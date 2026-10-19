from decimal import Decimal

from django import forms
from django.conf import settings
from django.contrib import messages
from django.db.models import F, Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect
from django.utils.dateparse import parse_date
from django.utils.functional import cached_property
from django.views.generic import CreateView, DetailView, FormView, ListView, UpdateView

from apps.accounts.forms import StyledFormMixin
from apps.accounts.mixins import GymPermissionMixin
from apps.suppliers.models import Supplier

from . import services
from .models import InventoryItem, ItemCategory, MovementKind, StockMovement
from .services import StockError


class InventoryEnabledMixin(GymPermissionMixin):
    """Inventory is optional: with INVENTORY_ENABLED off every inventory URL answers 404."""

    def dispatch(self, request, *args, **kwargs):
        if not settings.INVENTORY_ENABLED:
            raise Http404
        return super().dispatch(request, *args, **kwargs)


class ItemForm(StyledFormMixin, forms.ModelForm):
    opening_quantity = forms.IntegerField(min_value=0, max_value=1_000_000, required=False, initial=0, label="Opening stock",
                                          help_text="Units on the shelf right now. Recorded as the first stock movement.")

    class Meta:
        model = InventoryItem
        fields = ["name", "sku", "category", "unit", "min_stock", "supplier", "notes", "is_active"]
        help_texts = {"sku": "Leave blank to generate one automatically (SKU-00001 ...)."}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["sku"].required = False
        self.fields["supplier"].queryset = Supplier.objects.filter(is_active=True)
        if self.instance.pk:
            del self.fields["opening_quantity"]

    def clean_sku(self):
        sku = self.cleaned_data["sku"].strip().upper()
        if sku and InventoryItem.objects.filter(sku__iexact=sku).exclude(pk=self.instance.pk).exists():
            raise forms.ValidationError("This SKU is already in use.")
        return sku


class PurchaseForm(StyledFormMixin, forms.Form):
    quantity = forms.IntegerField(min_value=1, max_value=1_000_000)
    unit_cost = forms.DecimalField(label="Price per unit (LKR)", min_value=Decimal("0"), max_digits=12, decimal_places=2, required=False)
    supplier = forms.ModelChoiceField(queryset=Supplier.objects.none(), required=False)
    reference = forms.CharField(label="Invoice / reference", max_length=60, required=False)
    record_expense = forms.BooleanField(required=False, initial=True, label="Record the purchase as an expense",
                                        help_text="Creates one linked 'Inventory & supplies' expense (needs a price per unit).")

    def __init__(self, *args, can_expense, item, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["supplier"].queryset = Supplier.objects.filter(is_active=True)
        self.fields["supplier"].initial = item.supplier_id
        if item.last_unit_cost is not None:
            self.fields["unit_cost"].initial = item.last_unit_cost
        if not can_expense:
            del self.fields["record_expense"]

    def clean(self):
        c = super().clean()
        if c.get("record_expense") and not c.get("unit_cost"):
            self.add_error("unit_cost", "Enter the price per unit to record an expense.")
        return c


class UsageForm(StyledFormMixin, forms.Form):
    quantity = forms.IntegerField(min_value=1, max_value=1_000_000, label="Quantity used / sold")
    reason = forms.CharField(max_length=255, required=False, label="Note")


class AdjustForm(StyledFormMixin, forms.Form):
    counted = forms.IntegerField(min_value=0, max_value=1_000_000, label="Counted quantity",
                                 help_text="What is actually on the shelf. The difference is recorded as an adjustment.")
    reason = forms.CharField(max_length=255, label="Reason")


class ItemListView(InventoryEnabledMixin, ListView):
    required_permission = "inventory.view"
    template_name = "inventory/item_list.html"
    paginate_by = 25

    def get_queryset(self):
        g = self.request.GET
        qs = InventoryItem.objects.select_related("supplier")
        if g.get("show") != "all":
            qs = qs.filter(is_active=True)
        q = g.get("q", "").strip()
        if q:
            qs = qs.filter(Q(name__icontains=q) | Q(sku__icontains=q))
        if g.get("category") in ItemCategory.values:
            qs = qs.filter(category=g["category"])
        if g.get("stock") == "low":
            qs = qs.filter(quantity__lte=F("min_stock"))
        return qs

    def get_context_data(self, **kwargs):
        g = self.request.GET
        low = InventoryItem.objects.filter(is_active=True, quantity__lte=F("min_stock")).count()
        return super().get_context_data(q=g.get("q", ""), category=g.get("category", ""), stock=g.get("stock", ""), show=g.get("show", ""),
                                        categories=ItemCategory.choices, low_count=low, **kwargs)


class ItemCreateView(InventoryEnabledMixin, CreateView):
    required_permission = "inventory.manage"
    model = InventoryItem
    form_class = ItemForm
    template_name = "inventory/item_form.html"

    def get_context_data(self, **kwargs):
        return super().get_context_data(title="New inventory item", **kwargs)

    def form_valid(self, form):
        response = super().form_valid(form)
        opening = form.cleaned_data.get("opening_quantity") or 0
        if opening:
            services.move_stock(self.object, MovementKind.INITIAL, opening, self.request.user, reason="Opening stock")
        messages.success(self.request, f"{self.object.name} was added as {self.object.sku}.")
        return response


class ItemUpdateView(InventoryEnabledMixin, UpdateView):
    required_permission = "inventory.manage"
    model = InventoryItem
    form_class = ItemForm
    template_name = "inventory/item_form.html"

    def get_context_data(self, **kwargs):
        return super().get_context_data(title=f"Edit {self.object.name}", **kwargs)

    def form_valid(self, form):
        messages.success(self.request, "Item updated. Quantities change only through stock movements.")
        return super().form_valid(form)


class ItemDetailView(InventoryEnabledMixin, DetailView):
    required_permission = "inventory.view"
    model = InventoryItem
    template_name = "inventory/item_detail.html"

    def get_context_data(self, **kwargs):
        return super().get_context_data(movements=self.object.movements.select_related("user", "supplier")[:50],
                                        can_manage=self.request.user.has_gym_perm("inventory.manage"), **kwargs)


class _MoveView(InventoryEnabledMixin, FormView):
    required_permission = "inventory.manage"
    template_name = "inventory/move_form.html"
    title = ""

    @cached_property
    def item(self):
        return get_object_or_404(InventoryItem, pk=self.kwargs["pk"])

    def get_context_data(self, **kwargs):
        return super().get_context_data(item=self.item, title=self.title, **kwargs)

    def apply(self, form):
        raise NotImplementedError

    def form_valid(self, form):
        try:
            self.apply(form)
        except StockError as exc:
            form.add_error(None, str(exc))
            return self.form_invalid(form)
        messages.success(self.request, "Stock updated.")
        return redirect(self.item)


class PurchaseView(_MoveView):
    form_class = PurchaseForm
    title = "Record stock purchase"

    def get_form_kwargs(self):
        return {**super().get_form_kwargs(), "can_expense": self.request.user.has_gym_perm("expenses.manage"), "item": self.item}

    def apply(self, f):
        c = f.cleaned_data
        services.move_stock(self.item, MovementKind.PURCHASE, c["quantity"], self.request.user, unit_cost=c["unit_cost"], supplier=c["supplier"],
                            reference=c["reference"], record_expense=c.get("record_expense", False))


class UsageView(_MoveView):
    form_class = UsageForm
    title = "Record usage / sale"

    def apply(self, f):
        services.move_stock(self.item, MovementKind.USAGE, f.cleaned_data["quantity"], self.request.user, reason=f.cleaned_data["reason"])


class AdjustView(_MoveView):
    form_class = AdjustForm
    title = "Adjust stock count"

    def apply(self, f):
        delta = f.cleaned_data["counted"] - self.item.quantity
        if delta == 0:
            raise StockError("The counted quantity matches the system; nothing to adjust.")
        services.move_stock(self.item, MovementKind.ADJUSTMENT, delta, self.request.user, reason=f.cleaned_data["reason"])


class MovementListView(InventoryEnabledMixin, ListView):
    required_permission = "inventory.view"
    template_name = "inventory/movement_list.html"
    paginate_by = 30

    def get_queryset(self):
        g = self.request.GET
        qs = StockMovement.objects.select_related("item", "user", "supplier")
        if g.get("kind") in MovementKind.values:
            qs = qs.filter(kind=g["kind"])
        if g.get("q", "").strip():
            qs = qs.filter(Q(item__name__icontains=g["q"].strip()) | Q(item__sku__icontains=g["q"].strip()))
        d_from, d_to = parse_date(g.get("from", "")), parse_date(g.get("to", ""))
        if d_from:
            qs = qs.filter(created_at__date__gte=d_from)
        if d_to:
            qs = qs.filter(created_at__date__lte=d_to)
        return qs

    def get_context_data(self, **kwargs):
        g = self.request.GET
        return super().get_context_data(q=g.get("q", ""), kind=g.get("kind", ""), date_from=g.get("from", ""), date_to=g.get("to", ""), kinds=MovementKind.choices, **kwargs)

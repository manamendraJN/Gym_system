from datetime import timedelta
from decimal import Decimal

from django import forms
from django.contrib import messages
from django.db.models import Q, Sum
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse_lazy
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.utils.functional import cached_property
from django.views import View
from django.views.generic import CreateView, DetailView, FormView, ListView, UpdateView

from apps.accounts.forms import StyledFormMixin
from apps.accounts.mixins import GymPermissionMixin
from apps.core.listing import resolve_sort, sort_columns
from apps.core.models import ActivityLog
from apps.core.validators import validate_document_upload
from apps.memberships.forms import ReasonForm
from apps.suppliers.models import Supplier

from . import services
from .models import Expense, ExpenseCategory
from .services import ExpenseError


class ExpenseForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = Expense
        fields = ["category", "amount", "expense_date", "description", "supplier", "reference", "receipt"]
        widgets = {"expense_date": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
                   "amount": forms.NumberInput(attrs={"step": "0.01", "min": "0.01"}),
                   "receipt": forms.FileInput(attrs={"accept": ".pdf,.jpg,.jpeg,.png"})}
        help_texts = {"receipt": "Supporting receipt: PDF, JPEG or PNG up to 5 MB. Stored privately."}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        cats = ExpenseCategory.objects.filter(is_active=True)
        if self.instance.pk:
            cats = ExpenseCategory.objects.filter(Q(is_active=True) | Q(pk=self.instance.category_id))
        self.fields["category"].queryset = cats
        self.fields["supplier"].queryset = Supplier.objects.filter(is_active=True)
        self.fields["supplier"].required = False

    def clean_amount(self):
        amount = self.cleaned_data["amount"]
        if amount <= 0 or amount > Decimal("99999999.99"):
            raise forms.ValidationError("Enter an amount greater than zero.")
        return amount

    def clean_expense_date(self):
        day, today = self.cleaned_data["expense_date"], timezone.localdate()
        if day > today:
            raise forms.ValidationError("The expense date cannot be in the future.")
        if day < today.replace(year=today.year - 5):
            raise forms.ValidationError("The expense date is too far in the past.")
        return day

    def clean_receipt(self):
        return validate_document_upload(self.cleaned_data.get("receipt"))

    def clean_description(self):
        text = self.cleaned_data["description"].strip()
        if len(text) < 3:
            raise forms.ValidationError("Please describe the expense.")
        return text


class CategoryForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = ExpenseCategory
        fields = ["name", "is_active"]

    def clean_name(self):
        name = " ".join(self.cleaned_data["name"].split())
        if ExpenseCategory.objects.filter(name__iexact=name).exclude(pk=self.instance.pk).exists():
            raise forms.ValidationError("This category already exists.")
        return name


SORTS = {"date": "expense_date", "amount": "amount", "category": "category__name"}
COLUMNS = [("date", "Date"), ("category", "Category"), ("amount", "Amount")]


class ExpenseListView(GymPermissionMixin, ListView):
    required_permission = "expenses.view"
    template_name = "expenses/expense_list.html"
    paginate_by = 20

    def get_queryset(self):
        g = self.request.GET
        qs = Expense.objects.select_related("category", "supplier", "entered_by")
        q = g.get("q", "").strip()
        if q:
            qs = qs.filter(Q(description__icontains=q) | Q(reference__icontains=q) | Q(supplier__name__icontains=q) | Q(category__name__icontains=q))
        if g.get("category", "").isdigit():
            qs = qs.filter(category_id=g["category"])
        if g.get("supplier", "").isdigit():
            qs = qs.filter(supplier_id=g["supplier"])
        d_from, d_to = parse_date(g.get("from", "")), parse_date(g.get("to", ""))
        if d_from:
            qs = qs.filter(expense_date__gte=d_from)
        if d_to:
            qs = qs.filter(expense_date__lte=d_to)
        if g.get("show") == "void":
            qs = qs.filter(voided_at__isnull=False)
        elif g.get("show") != "all":
            qs = qs.filter(voided_at__isnull=True)
        self.filtered = qs
        self.sort, self.direction, order = resolve_sort(self.request, SORTS, "date")
        if "sort" not in g:
            self.direction, order = "desc", ["-expense_date", "-pk"]
        return qs.order_by(*order)

    def get_context_data(self, **kwargs):
        g = self.request.GET
        total = self.filtered.filter(voided_at__isnull=True).aggregate(t=Sum("amount"))["t"] or Decimal("0")
        return super().get_context_data(
            q=g.get("q", ""), category=g.get("category", ""), supplier=g.get("supplier", ""), date_from=g.get("from", ""),
            date_to=g.get("to", ""), show=g.get("show", ""), categories=ExpenseCategory.objects.all(), suppliers=Supplier.objects.all(),
            total=total, columns={c["key"]: c for c in sort_columns(self.request, COLUMNS, self.sort, self.direction)},
            filtered_on=any(g.get(k) for k in ("q", "category", "supplier", "from", "to", "show")), **kwargs)


class ExpenseCreateView(GymPermissionMixin, CreateView):
    required_permission = "expenses.manage"
    model = Expense
    form_class = ExpenseForm
    template_name = "expenses/expense_form.html"

    def get_context_data(self, **kwargs):
        return super().get_context_data(title="Record expense", multipart=True, **kwargs)

    def form_valid(self, form):
        f = form.cleaned_data
        self.object = services.record_expense(f["category"], f["amount"], f["expense_date"], f["description"], self.request.user,
                                              f["supplier"], f["reference"], f.get("receipt"))
        messages.success(self.request, "Expense recorded.")
        return redirect(self.object)


class ExpenseDetailView(GymPermissionMixin, DetailView):
    required_permission = "expenses.view"
    model = Expense
    template_name = "expenses/expense_detail.html"

    def get_queryset(self):
        return Expense.objects.select_related("category", "supplier", "entered_by", "voided_by")

    def get_context_data(self, **kwargs):
        u, e = self.request.user, self.object
        return super().get_context_data(
            can_edit=u.has_gym_perm("expenses.manage") and not e.is_void and not e.is_linked,
            can_void=u.has_gym_perm("expenses.void") and not e.is_void and not e.is_linked, **kwargs)


class ExpenseUpdateView(GymPermissionMixin, UpdateView):
    required_permission = "expenses.manage"
    model = Expense
    form_class = ExpenseForm
    template_name = "expenses/expense_form.html"

    def get_object(self, queryset=None):
        obj = super().get_object(queryset)
        if obj.is_void or obj.is_linked:
            messages.error(self.request, "Voided or system-generated expenses cannot be edited here.")
            raise Http404
        return obj

    def get_context_data(self, **kwargs):
        return super().get_context_data(title=f"Edit expense #{self.object.pk}", multipart=True, **kwargs)

    def form_valid(self, form):
        old = Expense.objects.values_list("amount", flat=True).get(pk=self.object.pk)
        response = super().form_valid(form)
        ActivityLog.record(self.request.user, "expense_updated", f"Updated expense #{self.object.pk} (LKR {old:,.2f} → {self.object.amount:,.2f})")
        messages.success(self.request, "Expense updated.")
        return response


class ExpenseVoidView(GymPermissionMixin, FormView):
    required_permission = "expenses.void"
    form_class = ReasonForm
    template_name = "expenses/void_form.html"

    @cached_property
    def expense(self):
        return get_object_or_404(Expense, pk=self.kwargs["pk"])

    def get_context_data(self, **kwargs):
        return super().get_context_data(expense=self.expense, **kwargs)

    def form_valid(self, form):
        try:
            services.void_expense(self.expense, self.request.user, form.cleaned_data["reason"])
        except ExpenseError as exc:
            form.add_error(None, str(exc))
            return self.form_invalid(form)
        messages.success(self.request, "The expense was voided. It no longer counts in any total.")
        return redirect(self.expense)


class ReceiptDownloadView(GymPermissionMixin, View):
    """Receipts are private files: only users with expenses.view can fetch them, and every download is logged."""

    required_permission = "expenses.view"

    def get(self, request, pk):
        expense = get_object_or_404(Expense, pk=pk)
        if not expense.receipt or not expense.receipt.storage.exists(expense.receipt.name):
            raise Http404
        ActivityLog.record(request.user, "expense_receipt_viewed", f"Opened receipt of expense #{expense.pk}")
        response = FileResponse(expense.receipt.open("rb"), as_attachment=True, filename=expense.receipt.name.rsplit("/", 1)[-1])
        response["Cache-Control"] = "private, no-store"
        response["X-Content-Type-Options"] = "nosniff"
        return response


class CategoryListView(GymPermissionMixin, ListView):
    required_permission = "expenses.view"
    model = ExpenseCategory
    template_name = "expenses/category_list.html"


class CategoryCreateView(GymPermissionMixin, CreateView):
    required_permission = "expenses.manage"
    model = ExpenseCategory
    form_class = CategoryForm
    template_name = "expenses/category_form.html"
    success_url = reverse_lazy("expenses:categories")

    def get_context_data(self, **kwargs):
        return super().get_context_data(title="New expense category", **kwargs)


class CategoryUpdateView(GymPermissionMixin, UpdateView):
    required_permission = "expenses.manage"
    model = ExpenseCategory
    form_class = CategoryForm
    template_name = "expenses/category_form.html"
    success_url = reverse_lazy("expenses:categories")

    def get_context_data(self, **kwargs):
        return super().get_context_data(title=f"Edit {self.object.name}", **kwargs)

from datetime import timedelta
from decimal import Decimal

from django import forms
from django.contrib import messages
from django.db.models import Count, Q, Sum
from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.utils.functional import cached_property
from django.views import View
from django.views.generic import CreateView, DetailView, FormView, ListView, UpdateView

from apps.accounts.forms import StyledFormMixin
from apps.accounts.mixins import GymPermissionMixin
from apps.core.models import ActivityLog
from apps.expenses import services as expense_services
from apps.suppliers.models import Supplier

from . import services
from .models import Equipment, EquipmentCategory, EquipmentStatus, FaultReport, FaultSeverity, MaintenanceKind, MaintenanceRecord, Condition
from .services import EquipmentError

DATE = forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")


class EquipmentForm(StyledFormMixin, forms.ModelForm):
    record_purchase_expense = forms.BooleanField(
        required=False, label="Also record the purchase as an expense",
        help_text="Adds one 'Equipment' expense for the purchase cost. Leave unticked if you already recorded it.")

    class Meta:
        model = Equipment
        fields = ["name", "code", "category", "serial_number", "location", "condition", "status", "purchase_date", "purchase_cost", "supplier",
                  "warranty_expires", "warranty_notes", "maintenance_interval_days", "next_maintenance_date", "notes"]
        widgets = {"purchase_date": DATE, "warranty_expires": DATE, "next_maintenance_date": DATE, "notes": forms.Textarea(attrs={"rows": 2}),
                   "purchase_cost": forms.NumberInput(attrs={"step": "0.01", "min": "0"})}
        help_texts = {"code": "Leave blank to generate one automatically (EQ-0001 ...)."}

    def __init__(self, *args, can_expense=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["code"].required = False
        self.fields["supplier"].queryset = Supplier.objects.filter(is_active=True)
        if not can_expense or self.instance.pk:
            del self.fields["record_purchase_expense"]

    def clean_code(self):
        code = self.cleaned_data["code"].strip().upper()
        if code and Equipment.objects.filter(code__iexact=code).exclude(pk=self.instance.pk).exists():
            raise forms.ValidationError("This equipment code is already in use.")
        return code

    def clean(self):
        c = super().clean()
        if c.get("record_purchase_expense") and not c.get("purchase_cost"):
            self.add_error("purchase_cost", "Enter the purchase cost to record it as an expense.")
        if c.get("purchase_date") and c["purchase_date"] > timezone.localdate():
            self.add_error("purchase_date", "The purchase date cannot be in the future.")
        if c.get("warranty_expires") and c.get("purchase_date") and c["warranty_expires"] < c["purchase_date"]:
            self.add_error("warranty_expires", "The warranty cannot expire before the purchase date.")
        return c


class FaultForm(StyledFormMixin, forms.Form):
    severity = forms.ChoiceField(choices=FaultSeverity.choices, initial=FaultSeverity.MINOR,
                                 help_text="A major fault takes the equipment out of service straight away.")
    description = forms.CharField(widget=forms.Textarea(attrs={"rows": 3}), max_length=1000)


class MaintenanceForm(StyledFormMixin, forms.Form):
    kind = forms.ChoiceField(choices=MaintenanceKind.choices)
    performed_on = forms.DateField(widget=DATE, label="Date performed")
    description = forms.CharField(max_length=255)
    cost = forms.DecimalField(label="Cost (LKR)", min_value=Decimal("0"), max_digits=12, decimal_places=2, initial=0,
                              widget=forms.NumberInput(attrs={"step": "0.01", "min": "0"}))
    technician = forms.CharField(label="Technician / company", max_length=150, required=False)
    supplier = forms.ModelChoiceField(queryset=Supplier.objects.none(), required=False)
    fault = forms.ModelChoiceField(queryset=FaultReport.objects.none(), required=False, label="Resolves fault")
    next_due = forms.DateField(widget=DATE, required=False, label="Next maintenance due",
                               help_text="Leave blank to use the equipment's service interval, if it has one.")
    condition_after = forms.ChoiceField(choices=[("", "Unchanged")] + Condition.choices, required=False)
    return_to_service = forms.BooleanField(required=False, label="Return the equipment to service (status: Available)")
    record_as_expense = forms.BooleanField(required=False, initial=True, label="Record the cost as a Maintenance expense",
                                           help_text="Creates one linked expense so the cost appears in expense reports exactly once.")

    def __init__(self, *args, equipment, can_expense, **kwargs):
        super().__init__(*args, **kwargs)
        self.equipment = equipment
        self.fields["performed_on"].initial = timezone.localdate()
        self.fields["supplier"].queryset = Supplier.objects.filter(is_active=True)
        self.fields["fault"].queryset = equipment.faults.filter(resolved_at__isnull=True)
        if not can_expense:
            del self.fields["record_as_expense"]


class EquipmentListView(GymPermissionMixin, ListView):
    required_permission = "equipment.view"
    template_name = "equipment/equipment_list.html"
    paginate_by = 20

    def get_queryset(self):
        g = self.request.GET
        qs = Equipment.objects.select_related("supplier")
        q = g.get("q", "").strip()
        if q:
            qs = qs.filter(Q(name__icontains=q) | Q(code__icontains=q) | Q(serial_number__icontains=q) | Q(location__icontains=q))
        if g.get("category") in EquipmentCategory.values:
            qs = qs.filter(category=g["category"])
        if g.get("status") in EquipmentStatus.values:
            qs = qs.filter(status=g["status"])
        today = timezone.localdate()
        if g.get("maintenance") == "overdue":
            qs = qs.filter(next_maintenance_date__lt=today)
        elif g.get("maintenance") == "soon":
            qs = qs.filter(next_maintenance_date__gte=today, next_maintenance_date__lte=today + timedelta(days=14))
        return qs

    def get_context_data(self, **kwargs):
        g = self.request.GET
        counts = {row["status"]: row["n"] for row in Equipment.objects.values("status").annotate(n=Count("pk"))}
        return super().get_context_data(
            q=g.get("q", ""), category=g.get("category", ""), status=g.get("status", ""), maintenance=g.get("maintenance", ""),
            categories=EquipmentCategory.choices, statuses=EquipmentStatus.choices, counts=counts,
            open_faults=FaultReport.objects.filter(resolved_at__isnull=True).count(), **kwargs)


class EquipmentCreateView(GymPermissionMixin, CreateView):
    required_permission = "equipment.manage"
    model = Equipment
    form_class = EquipmentForm
    template_name = "equipment/equipment_form.html"

    def get_form_kwargs(self):
        return {**super().get_form_kwargs(), "can_expense": self.request.user.has_gym_perm("expenses.manage")}

    def get_context_data(self, **kwargs):
        return super().get_context_data(title="Register equipment", **kwargs)

    def form_valid(self, form):
        response = super().form_valid(form)
        eq = self.object
        if form.cleaned_data.get("record_purchase_expense"):
            expense_services.record_expense(
                expense_services.category("Equipment"), eq.purchase_cost, eq.purchase_date or timezone.localdate(),
                f"Purchase of {eq.code} {eq.name}", self.request.user, supplier=eq.supplier, equipment=eq)
        ActivityLog.record(self.request.user, "equipment_registered", f"Registered equipment {eq.code} {eq.name}")
        messages.success(self.request, f"{eq.name} was registered as {eq.code}.")
        return response


class EquipmentUpdateView(GymPermissionMixin, UpdateView):
    required_permission = "equipment.manage"
    model = Equipment
    form_class = EquipmentForm
    template_name = "equipment/equipment_form.html"

    def get_form_kwargs(self):
        return {**super().get_form_kwargs(), "can_expense": False}

    def get_context_data(self, **kwargs):
        return super().get_context_data(title=f"Edit {self.object.name}", **kwargs)

    def form_valid(self, form):
        messages.success(self.request, "Equipment updated.")
        ActivityLog.record(self.request.user, "equipment_updated", f"Updated equipment {self.object.code}")
        return super().form_valid(form)


class EquipmentDetailView(GymPermissionMixin, DetailView):
    required_permission = "equipment.view"
    model = Equipment
    template_name = "equipment/equipment_detail.html"

    def get_context_data(self, **kwargs):
        eq, user = self.object, self.request.user
        history = eq.maintenance.select_related("recorded_by", "supplier")
        totals = history.aggregate(all=Sum("cost"), repairs=Sum("cost", filter=Q(kind=MaintenanceKind.REPAIR)))
        return super().get_context_data(
            history=history[:50], faults=eq.faults.select_related("reported_by")[:20], statuses=EquipmentStatus.choices,
            total_cost=totals["all"] or Decimal("0"), repair_cost=totals["repairs"] or Decimal("0"),
            can_manage=user.has_gym_perm("equipment.manage"), **kwargs)


class StatusView(GymPermissionMixin, View):
    required_permission = "equipment.manage"
    http_method_names = ["post"]

    def post(self, request, pk):
        eq = get_object_or_404(Equipment, pk=pk)
        try:
            services.set_status(eq, request.POST.get("status", ""), request.user, request.POST.get("note", "")[:100])
            messages.success(request, "Status updated.")
        except EquipmentError as exc:
            messages.error(request, str(exc))
        return redirect(eq)


class FaultCreateView(GymPermissionMixin, FormView):
    required_permission = "equipment.view"
    form_class = FaultForm
    template_name = "equipment/fault_form.html"

    @cached_property
    def equipment(self):
        return get_object_or_404(Equipment, pk=self.kwargs["pk"])

    def get_context_data(self, **kwargs):
        return super().get_context_data(equipment=self.equipment, **kwargs)

    def form_valid(self, form):
        try:
            fault = services.report_fault(self.equipment, form.cleaned_data["description"], form.cleaned_data["severity"], self.request.user)
        except EquipmentError as exc:
            form.add_error(None, str(exc))
            return self.form_invalid(form)
        msg = "Fault reported."
        if fault.severity == FaultSeverity.MAJOR:
            msg += " The equipment was marked out of service."
        messages.success(self.request, msg)
        return redirect(self.equipment)


class MaintenanceCreateView(GymPermissionMixin, FormView):
    required_permission = "equipment.manage"
    form_class = MaintenanceForm
    template_name = "equipment/maintenance_form.html"

    @cached_property
    def equipment(self):
        return get_object_or_404(Equipment, pk=self.kwargs["pk"])

    def get_form_kwargs(self):
        return {**super().get_form_kwargs(), "equipment": self.equipment, "can_expense": self.request.user.has_gym_perm("expenses.manage")}

    def get_initial(self):
        fault = self.request.GET.get("fault")
        return {"fault": fault if fault and fault.isdigit() else None}

    def get_context_data(self, **kwargs):
        return super().get_context_data(equipment=self.equipment, **kwargs)

    def form_valid(self, form):
        c = form.cleaned_data
        try:
            services.record_maintenance(
                self.equipment, self.request.user, kind=c["kind"], performed_on=c["performed_on"], description=c["description"], cost=c["cost"],
                technician=c["technician"], supplier=c["supplier"], fault=c["fault"], next_due=c["next_due"],
                condition_after=c["condition_after"], return_to_service=c["return_to_service"],
                record_as_expense=c.get("record_as_expense", False))
        except EquipmentError as exc:
            form.add_error(None, str(exc))
            return self.form_invalid(form)
        messages.success(self.request, "Maintenance recorded.")
        return redirect(self.equipment)


class MaintenanceListView(GymPermissionMixin, ListView):
    """All maintenance history plus the upcoming schedule."""

    required_permission = "equipment.view"
    template_name = "equipment/maintenance_list.html"
    paginate_by = 25

    def get_queryset(self):
        g = self.request.GET
        qs = MaintenanceRecord.objects.select_related("equipment", "recorded_by", "supplier")
        if g.get("kind") in MaintenanceKind.values:
            qs = qs.filter(kind=g["kind"])
        d_from, d_to = parse_date(g.get("from", "")), parse_date(g.get("to", ""))
        if d_from:
            qs = qs.filter(performed_on__gte=d_from)
        if d_to:
            qs = qs.filter(performed_on__lte=d_to)
        if g.get("q", "").strip():
            qs = qs.filter(Q(equipment__name__icontains=g["q"].strip()) | Q(equipment__code__icontains=g["q"].strip()) | Q(description__icontains=g["q"].strip()))
        self.filtered = qs
        return qs

    def get_context_data(self, **kwargs):
        g = self.request.GET
        today = timezone.localdate()
        due = Equipment.objects.filter(next_maintenance_date__isnull=False, next_maintenance_date__lte=today + timedelta(days=30)).order_by("next_maintenance_date")
        return super().get_context_data(
            q=g.get("q", ""), kind=g.get("kind", ""), date_from=g.get("from", ""), date_to=g.get("to", ""), kinds=MaintenanceKind.choices,
            total_cost=self.filtered.aggregate(t=Sum("cost"))["t"] or Decimal("0"), due=due,
            open_faults=FaultReport.objects.filter(resolved_at__isnull=True).select_related("equipment"), today=today, **kwargs)

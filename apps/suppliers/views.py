from django import forms
from django.contrib import messages
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse_lazy
from django.views import View
from django.views.generic import CreateView, ListView, UpdateView

from apps.accounts.forms import StyledFormMixin
from apps.accounts.mixins import GymPermissionMixin
from apps.core.models import ActivityLog

from .models import Supplier


class SupplierForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = Supplier
        fields = ["name", "contact_name", "phone", "email", "address", "notes", "is_active"]
        widgets = {"address": forms.Textarea(attrs={"rows": 2}), "notes": forms.Textarea(attrs={"rows": 2})}

    def clean_name(self):
        name = " ".join(self.cleaned_data["name"].split())
        if Supplier.objects.filter(name__iexact=name).exclude(pk=self.instance.pk).exists():
            raise forms.ValidationError("A supplier with this name already exists.")
        return name


class SupplierListView(GymPermissionMixin, ListView):
    required_permission = "suppliers.view"
    template_name = "suppliers/supplier_list.html"
    paginate_by = 20

    def get_queryset(self):
        qs = Supplier.objects.all()
        q = self.request.GET.get("q", "").strip()
        if q:
            qs = qs.filter(Q(name__icontains=q) | Q(contact_name__icontains=q) | Q(phone__contains=q) | Q(email__icontains=q))
        return qs

    def get_context_data(self, **kwargs):
        return super().get_context_data(q=self.request.GET.get("q", ""), **kwargs)


class SupplierCreateView(GymPermissionMixin, CreateView):
    required_permission = "suppliers.manage"
    model = Supplier
    form_class = SupplierForm
    template_name = "suppliers/supplier_form.html"
    success_url = reverse_lazy("suppliers:list")

    def get_context_data(self, **kwargs):
        return super().get_context_data(title="New supplier", **kwargs)

    def form_valid(self, form):
        messages.success(self.request, "Supplier saved.")
        ActivityLog.record(self.request.user, "supplier_created", f"Created supplier {form.instance.name}")
        return super().form_valid(form)


class SupplierUpdateView(GymPermissionMixin, UpdateView):
    required_permission = "suppliers.manage"
    model = Supplier
    form_class = SupplierForm
    template_name = "suppliers/supplier_form.html"
    success_url = reverse_lazy("suppliers:list")

    def get_context_data(self, **kwargs):
        return super().get_context_data(title=f"Edit {self.object.name}", **kwargs)

    def form_valid(self, form):
        messages.success(self.request, "Supplier updated.")
        return super().form_valid(form)

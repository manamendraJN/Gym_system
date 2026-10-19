from django.conf import settings
from django.contrib import messages
from django.db.models import Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect
from django.utils.functional import cached_property
from django.views import View
from django.views.generic import DetailView, FormView, ListView

from apps.accounts.mixins import GymPermissionMixin
from apps.core.models import ActivityLog, Notification
from apps.members.models import Member
from apps.trainers.access import can_access_member, scope_by_member
from apps.trainers.models import WEEKDAYS

from . import forms
from .models import DISCLAIMER, MEAL_ORDER, MealItem, MealPlan, NutritionProfile


class NutritionEnabledMixin(GymPermissionMixin):
    """The whole module is optional: when NUTRITION_ENABLED is off every page answers 404."""

    def dispatch(self, request, *args, **kwargs):
        if not settings.NUTRITION_ENABLED:
            raise Http404
        return super().dispatch(request, *args, **kwargs)


def group_by_day(items):
    days = []
    for n, label in WEEKDAYS:
        meals = sorted((i for i in items if i.weekday == n), key=lambda i: (MEAL_ORDER[i.meal_type], i.pk))
        if meals:
            days.append({"label": label, "meals": meals})
    return days


class PlanListView(NutritionEnabledMixin, ListView):
    required_permission = "nutrition.view"
    template_name = "nutrition/plan_list.html"
    paginate_by = 20

    def get_queryset(self):
        qs = scope_by_member(self.request.user, MealPlan.objects.select_related("member"))
        q = self.request.GET.get("q", "").strip()
        if q:
            qs = qs.filter(Q(name__icontains=q) | Q(member__full_name__icontains=q) | Q(member__member_id__icontains=q))
        return qs

    def get_context_data(self, **kwargs):
        return super().get_context_data(q=self.request.GET.get("q", ""), disclaimer=DISCLAIMER, **kwargs)


class PlanCreateView(NutritionEnabledMixin, FormView):
    required_permission = "nutrition.manage"
    form_class = forms.MealPlanForm
    template_name = "nutrition/plan_form.html"

    def get_initial(self):
        return {"member": self.request.GET.get("member", "")}

    def get_context_data(self, **kwargs):
        return super().get_context_data(title="New meal plan", disclaimer=DISCLAIMER, **kwargs)

    def form_valid(self, form):
        member = form.cleaned_data["member"]
        if not can_access_member(self.request.user, member):
            form.add_error("member", "You can only create plans for members assigned to you.")
            return self.form_invalid(form)
        plan = form.save(commit=False)
        plan.member, plan.created_by = member, self.request.user
        plan.save()
        ActivityLog.record(self.request.user, "meal_plan_created", f"Created meal plan “{plan.name}” for {member.full_name}")
        if member.user_id:
            Notification.send(member.user, "New meal plan", f"“{plan.name}” was assigned to you.", f"/me/nutrition/")
        messages.success(self.request, "Meal plan created. Add meals to it.")
        return redirect(plan)


class _PlanMixin(NutritionEnabledMixin):
    @cached_property
    def plan(self):
        return get_object_or_404(scope_by_member(self.request.user, MealPlan.objects.select_related("member")), pk=self.kwargs["pk"])


class PlanDetailView(_PlanMixin, DetailView):
    required_permission = "nutrition.view"
    template_name = "nutrition/plan_detail.html"
    context_object_name = "plan"

    def get_object(self, queryset=None):
        return self.plan

    def get_context_data(self, **kwargs):
        profile = NutritionProfile.objects.filter(member=self.plan.member).first()
        return super().get_context_data(days=group_by_day(list(self.plan.items.all())), profile=profile, disclaimer=DISCLAIMER,
                                        can_manage=self.request.user.has_gym_perm("nutrition.manage"), **kwargs)


class PlanUpdateView(_PlanMixin, FormView):
    required_permission = "nutrition.manage"
    form_class = forms.MealPlanForm
    template_name = "nutrition/plan_form.html"

    def get_form_kwargs(self):
        return {**super().get_form_kwargs(), "instance": self.plan, "fixed_member": self.plan.member}

    def get_context_data(self, **kwargs):
        return super().get_context_data(title=f"Edit {self.plan.name}", disclaimer=DISCLAIMER, **kwargs)

    def form_valid(self, form):
        form.save()
        messages.success(self.request, "The meal plan was updated.")
        return redirect(self.plan)


class ItemCreateView(_PlanMixin, FormView):
    required_permission = "nutrition.manage"
    form_class = forms.MealItemForm
    template_name = "nutrition/item_form.html"

    def get_context_data(self, **kwargs):
        return super().get_context_data(title="Add meal", plan=self.plan, **kwargs)

    def form_valid(self, form):
        item = form.save(commit=False)
        item.plan = self.plan
        item.save()
        messages.success(self.request, "Meal added.")
        return redirect(self.plan)


class ItemDeleteView(NutritionEnabledMixin, View):
    required_permission = "nutrition.manage"
    http_method_names = ["post"]

    def post(self, request, pk):
        item = get_object_or_404(MealItem.objects.select_related("plan__member"), pk=pk)
        if not can_access_member(request.user, item.plan.member):
            raise Http404
        plan = item.plan
        item.delete()
        messages.success(request, "Meal removed.")
        return redirect(plan)


class ProfileView(NutritionEnabledMixin, FormView):
    """Food preferences, allergies and goals of one member (sensitive)."""

    required_permission = "nutrition.view"
    form_class = forms.ProfileForm
    template_name = "nutrition/profile.html"

    @cached_property
    def member(self):
        member = get_object_or_404(Member, pk=self.kwargs["member_pk"])
        if not can_access_member(self.request.user, member):
            raise Http404
        return member

    def get_form_kwargs(self):
        return {**super().get_form_kwargs(), "instance": NutritionProfile.objects.filter(member=self.member).first()}

    def get_context_data(self, **kwargs):
        return super().get_context_data(member=self.member, can_edit=self.request.user.has_gym_perm("nutrition.manage"),
                                        disclaimer=DISCLAIMER, **kwargs)

    def post(self, request, *args, **kwargs):
        if not request.user.has_gym_perm("nutrition.manage"):
            from django.core.exceptions import PermissionDenied

            raise PermissionDenied
        return super().post(request, *args, **kwargs)

    def form_valid(self, form):
        profile = form.save(commit=False)
        profile.member, profile.updated_by = self.member, self.request.user
        profile.save()
        ActivityLog.record(self.request.user, "nutrition_profile_updated", f"Updated nutrition profile of {self.member.full_name}")
        messages.success(self.request, "Nutrition profile saved.")
        return redirect("nutrition:profile", member_pk=self.member.pk)

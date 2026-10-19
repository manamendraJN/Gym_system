from itertools import groupby

from django.contrib import messages
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone
from django.utils.functional import cached_property
from django.views import View
from django.views.generic import CreateView, DetailView, FormView, ListView, UpdateView

from apps.accounts.mixins import GymPermissionMixin
from apps.core.models import ActivityLog
from apps.trainers.access import can_access_member, scope_by_member, trainer_of
from apps.trainers.models import WEEKDAYS, Trainer

from . import forms, services
from .models import Category, Exercise, MuscleGroup, PlanExercise, PlanStatus, WorkoutPlan


# -------------------------------------------------------------------------------------------- exercises
class ExerciseListView(GymPermissionMixin, ListView):
    required_permission = "workouts.view"
    template_name = "workouts/exercise_list.html"
    paginate_by = 18

    def get_queryset(self):
        g = self.request.GET
        qs = Exercise.objects.all()
        if not self.request.user.has_gym_perm("workouts.manage") or g.get("show") != "all":
            qs = qs.filter(is_active=True)
        if g.get("q", "").strip():
            qs = qs.filter(Q(name__icontains=g["q"].strip()) | Q(description__icontains=g["q"].strip()))
        if g.get("muscle") in MuscleGroup.values:
            qs = qs.filter(muscle_group=g["muscle"])
        if g.get("category") in Category.values:
            qs = qs.filter(category=g["category"])
        return qs

    def get_context_data(self, **kwargs):
        g = self.request.GET
        return super().get_context_data(q=g.get("q", ""), muscle=g.get("muscle", ""), category=g.get("category", ""),
                                        show=g.get("show", ""), muscles=MuscleGroup.choices, categories=Category.choices, **kwargs)


class ExerciseCreateView(GymPermissionMixin, CreateView):
    required_permission = "workouts.manage"
    model = Exercise
    form_class = forms.ExerciseForm
    template_name = "workouts/exercise_form.html"

    def get_context_data(self, **kwargs):
        return super().get_context_data(title="New exercise", **kwargs)

    def form_valid(self, form):
        form.instance.created_by = self.request.user
        response = super().form_valid(form)
        messages.success(self.request, f"“{self.object.name}” was added to the library.")
        return response


class ExerciseUpdateView(GymPermissionMixin, UpdateView):
    required_permission = "workouts.manage"
    model = Exercise
    form_class = forms.ExerciseForm
    template_name = "workouts/exercise_form.html"

    def get_context_data(self, **kwargs):
        return super().get_context_data(title=f"Edit {self.object.name}", **kwargs)

    def form_valid(self, form):
        messages.success(self.request, "The exercise was updated.")
        return super().form_valid(form)


class ExerciseDetailView(GymPermissionMixin, DetailView):
    required_permission = "workouts.view"
    model = Exercise
    template_name = "workouts/exercise_detail.html"


# ----------------------------------------------------------------------------------------------- plans
class PlanListView(GymPermissionMixin, ListView):
    required_permission = "workouts.view"
    template_name = "workouts/plan_list.html"
    paginate_by = 20

    def get_queryset(self):
        g = self.request.GET
        qs = scope_by_member(self.request.user, WorkoutPlan.objects.select_related("member", "trainer"))
        if g.get("q", "").strip():
            q = g["q"].strip()
            qs = qs.filter(Q(name__icontains=q) | Q(member__full_name__icontains=q) | Q(member__member_id__icontains=q))
        if g.get("status") in PlanStatus.values:
            qs = qs.filter(status=g["status"])
        return qs

    def get_context_data(self, **kwargs):
        g = self.request.GET
        return super().get_context_data(q=g.get("q", ""), status=g.get("status", ""), statuses=PlanStatus.choices, **kwargs)


class PlanCreateView(GymPermissionMixin, FormView):
    required_permission = "workouts.manage"
    form_class = forms.PlanForm
    template_name = "workouts/plan_form.html"

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["trainer_choices"] = Trainer.objects.filter(user=self.request.user) if self.request.user.role == "trainer" else None
        return kwargs

    def get_initial(self):
        return {"member": self.request.GET.get("member", "")}

    def get_context_data(self, **kwargs):
        return super().get_context_data(title="New workout plan", **kwargs)

    def form_valid(self, form):
        member = form.cleaned_data["member"]
        if not can_access_member(self.request.user, member):
            form.add_error("member", "You can only create plans for members assigned to you.")
            return self.form_invalid(form)
        trainer = form.cleaned_data["trainer"] or trainer_of(self.request.user)
        plan = services.create_plan(member, form, self.request.user, trainer)
        messages.success(self.request, "The plan was created. Now add exercises to it.")
        return redirect(plan)


class _PlanMixin(GymPermissionMixin):
    @cached_property
    def plan(self):
        qs = scope_by_member(self.request.user, WorkoutPlan.objects.select_related("member", "trainer"))
        return get_object_or_404(qs, pk=self.kwargs["pk"])


class PlanDetailView(_PlanMixin, DetailView):
    required_permission = "workouts.view"
    template_name = "workouts/plan_detail.html"
    context_object_name = "plan"

    def get_object(self, queryset=None):
        return self.plan

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        items = list(self.plan.items.select_related("exercise"))
        days = [{"label": label, "items": [i for i in items if i.weekday == n and i.is_active]} for n, label in WEEKDAYS]
        ctx.update(
            days=[d for d in days if d["items"]], archived=[i for i in items if not i.is_active],
            changes=self.plan.changes.select_related("user")[:15],
            sessions=self.plan.sessions.prefetch_related("entries__exercise")[:10],
            can_manage=self.request.user.has_gym_perm("workouts.manage"), plan_statuses=PlanStatus.choices,
        )
        return ctx


class PlanUpdateView(_PlanMixin, FormView):
    required_permission = "workouts.manage"
    form_class = forms.PlanForm
    template_name = "workouts/plan_form.html"

    def get_form_kwargs(self):
        return {**super().get_form_kwargs(), "instance": self.plan, "fixed_member": self.plan.member}

    def get_context_data(self, **kwargs):
        return super().get_context_data(title=f"Edit {self.plan.name}", plan=self.plan, **kwargs)

    def form_valid(self, form):
        form.save()
        services.note_change(self.plan, self.request.user, "Plan details updated")
        messages.success(self.request, "The plan was updated.")
        return redirect(self.plan)


class PlanStatusView(_PlanMixin, View):
    required_permission = "workouts.manage"
    http_method_names = ["post"]

    def post(self, request, pk):
        status = request.POST.get("status")
        if status not in PlanStatus.values:
            messages.error(request, "Invalid status.")
        else:
            services.set_status(self.plan, status, request.user)
            messages.success(request, f"The plan is now {PlanStatus(status).label.lower()}.")
        return redirect(self.plan)


class ItemCreateView(_PlanMixin, FormView):
    required_permission = "workouts.manage"
    form_class = forms.PlanItemForm
    template_name = "workouts/item_form.html"

    def get_initial(self):
        return {"weekday": int(self.request.GET["day"]) if self.request.GET.get("day", "").isdigit() and int(self.request.GET["day"]) < 7 else 0}

    def get_context_data(self, **kwargs):
        return super().get_context_data(title="Add exercise", plan=self.plan, **kwargs)

    def form_valid(self, form):
        item = form.save(commit=False)
        item.plan = self.plan
        item.position = self.plan.items.filter(weekday=item.weekday).count()
        item.save()
        services.note_change(self.plan, self.request.user, f"Added “{item.exercise.name}” on {item.get_weekday_display()}")
        messages.success(self.request, "Exercise added.")
        return redirect(self.plan)


class _ItemMixin(GymPermissionMixin):
    @cached_property
    def item(self):
        qs = PlanExercise.objects.select_related("plan__member", "exercise")
        item = get_object_or_404(qs, pk=self.kwargs["pk"])
        if not can_access_member(self.request.user, item.plan.member):
            from django.http import Http404

            raise Http404
        return item


class ItemUpdateView(_ItemMixin, FormView):
    required_permission = "workouts.manage"
    form_class = forms.PlanItemForm
    template_name = "workouts/item_form.html"

    def get_form_kwargs(self):
        return {**super().get_form_kwargs(), "instance": self.item}

    def get_context_data(self, **kwargs):
        return super().get_context_data(title=f"Edit {self.item.exercise.name}", plan=self.item.plan, **kwargs)

    def form_valid(self, form):
        item = form.save()
        services.note_change(item.plan, self.request.user, f"Updated “{item.exercise.name}”: {item.prescription}")
        messages.success(self.request, "Exercise updated. Workouts already recorded keep the numbers that were actually done.")
        return redirect(item.plan)


class ItemRemoveView(_ItemMixin, View):
    required_permission = "workouts.manage"
    http_method_names = ["post"]

    def post(self, request, pk):
        plan = self.item.plan
        result = services.remove_item(self.item, request.user)
        messages.success(request, "Exercise removed." if result == "deleted" else "Exercise archived (it has recorded workouts, so its history is kept).")
        return redirect(plan)


class PlanCopyView(_PlanMixin, FormView):
    required_permission = "workouts.manage"
    form_class = forms.CopyPlanForm
    template_name = "workouts/copy_form.html"

    def get_context_data(self, **kwargs):
        return super().get_context_data(plan=self.plan, **kwargs)

    def form_valid(self, form):
        member = form.cleaned_data["member"]
        if not can_access_member(self.request.user, member):
            form.add_error("member", "You can only assign plans to members assigned to you.")
            return self.form_invalid(form)
        new = services.copy_plan(self.plan, member, form.cleaned_data["start_date"], self.request.user, trainer_of(self.request.user))
        messages.success(self.request, f"Plan copied to {member.full_name}.")
        return redirect(new)


class LogWorkoutView(_PlanMixin, FormView):
    """Record a completed workout on behalf of a member (members can also do this themselves in their portal)."""

    required_permission = "workouts.manage"
    form_class = forms.LogWorkoutForm
    template_name = "workouts/log_form.html"
    portal = False

    def get_form_kwargs(self):
        day = self.request.GET.get("day")
        items = self.plan.items.filter(is_active=True).select_related("exercise")
        if day and day.isdigit():
            items = items.filter(weekday=int(day))
        return {**super().get_form_kwargs(), "plan": self.plan, "items": items}

    def get_context_data(self, **kwargs):
        return super().get_context_data(plan=self.plan, portal=self.portal, **kwargs)

    def form_valid(self, form):
        services.log_session(self.plan.member, self.plan, form.cleaned_data["performed_on"], form.entries(), self.request.user, form.cleaned_data["notes"])
        messages.success(self.request, "Workout recorded.")
        return redirect(self.plan)

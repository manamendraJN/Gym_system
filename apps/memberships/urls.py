from django.urls import path

from . import views

app_name = "memberships"

urlpatterns = [
    path("plans/", views.PlanListView.as_view(), name="plan_list"),
    path("plans/new/", views.PlanCreateView.as_view(), name="plan_create"),
    path("plans/<int:pk>/", views.PlanDetailView.as_view(), name="plan_detail"),
    path("plans/<int:pk>/edit/", views.PlanUpdateView.as_view(), name="plan_edit"),
    path("plans/<int:pk>/toggle-active/", views.PlanToggleActiveView.as_view(), name="plan_toggle_active"),
    path("memberships/", views.MembershipListView.as_view(), name="list"),
    path("memberships/expired/", views.ExpiredListView.as_view(), name="expired"),
    path("memberships/expiring/", views.ExpiringSoonView.as_view(), name="expiring"),
    path("memberships/<int:pk>/", views.MembershipDetailView.as_view(), name="detail"),
    path("memberships/<int:pk>/renew/", views.RenewMembershipView.as_view(), name="renew"),
    path("memberships/<int:pk>/change/", views.ChangePlanView.as_view(), name="change"),
    path("memberships/<int:pk>/suspend/", views.SuspendMembershipView.as_view(), name="suspend"),
    path("memberships/<int:pk>/reactivate/", views.ReactivateMembershipView.as_view(), name="reactivate"),
    path("memberships/<int:pk>/cancel/", views.CancelMembershipView.as_view(), name="cancel"),
    path("members/<int:member_pk>/assign/", views.AssignMembershipView.as_view(), name="assign"),
]

from django.contrib import admin

from .models import Membership, MembershipPlan

admin.site.register(MembershipPlan)


@admin.register(Membership)
class MembershipAdmin(admin.ModelAdmin):
    list_display = ("member", "plan", "start_date", "end_date", "status")
    list_filter = ("status",)
    search_fields = ("member__full_name", "member__member_id")
    raw_id_fields = ("member", "previous")

    # Lifecycle changes must go through the services (they keep dates/history consistent).
    def has_add_permission(self, request):
        return False

    def get_readonly_fields(self, request, obj=None):
        return [f.name for f in Membership._meta.fields]

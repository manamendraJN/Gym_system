from django.contrib import admin

from .models import Attendance


@admin.register(Attendance)
class AttendanceAdmin(admin.ModelAdmin):
    list_display = ("member", "check_in", "check_out", "method")
    search_fields = ("member__full_name", "member__member_id")
    list_filter = ("method",)

    def has_add_permission(self, request):
        return False

    def get_readonly_fields(self, request, obj=None):
        return [f.name for f in Attendance._meta.fields]

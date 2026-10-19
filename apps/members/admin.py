from django.contrib import admin

from .models import Member


@admin.register(Member)
class MemberAdmin(admin.ModelAdmin):
    list_display = ("member_id", "full_name", "phone", "is_active")
    search_fields = ("member_id", "full_name", "phone", "email")
    readonly_fields = ("member_id",)

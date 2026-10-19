from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from .models import RolePermission, User


@admin.register(User)
class GymUserAdmin(UserAdmin):
    list_display = ("username", "email", "role", "is_active")
    list_filter = ("role", "is_active")
    fieldsets = UserAdmin.fieldsets + (("Gym", {"fields": ("role", "phone")}),)
    add_fieldsets = UserAdmin.add_fieldsets + (("Gym", {"fields": ("email", "role", "phone")}),)


admin.site.register(RolePermission)

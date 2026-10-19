from django.contrib import admin

from .models import Payment, PaymentAudit


class ReadOnlyAdmin(admin.ModelAdmin):
    """Financial records are changed only through the application's services (which audit every change)."""

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Payment)
class PaymentAdmin(ReadOnlyAdmin):
    list_display = ("receipt_number", "reference", "member", "kind", "amount", "status", "paid_on")
    search_fields = ("receipt_number", "reference", "member__full_name", "member__member_id")
    list_filter = ("kind", "status", "method")


@admin.register(PaymentAudit)
class PaymentAuditAdmin(ReadOnlyAdmin):
    list_display = ("created_at", "payment", "action", "user")

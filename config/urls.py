from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path

handler403 = "apps.core.views.permission_denied"
handler404 = "apps.core.views.page_not_found"
handler500 = "apps.core.views.server_error"

urlpatterns = [
    path("django-admin/", admin.site.urls),
    path("accounts/", include("apps.accounts.urls")),
    path("members/", include("apps.members.urls")),
    path("attendance/", include("apps.attendance.urls")),
    path("payments/", include("apps.payments.urls")),
    path("trainers/", include("apps.trainers.urls")),
    path("appointments/", include("apps.appointments.urls")),
    path("workouts/", include("apps.workouts.urls")),
    path("progress/", include("apps.progress.urls")),
    path("nutrition/", include("apps.nutrition.urls")),
    path("me/", include("apps.portal.urls")),
    path("suppliers/", include("apps.suppliers.urls")),
    path("expenses/", include("apps.expenses.urls")),
    path("equipment/", include("apps.equipment.urls")),
    path("inventory/", include("apps.inventory.urls")),
    path("announcements/", include("apps.announcements.urls")),
    path("notifications/", include("apps.notifications.urls")),
    path("reports/", include("apps.reports.urls")),
    path("", include("apps.memberships.urls")),
    path("", include("apps.core.urls")),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

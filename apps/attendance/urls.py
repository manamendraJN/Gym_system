from django.urls import path

from . import views

app_name = "attendance"

urlpatterns = [
    path("", views.DeskView.as_view(), name="desk"),
    path("scan/", views.ScanView.as_view(), name="scan"),
    path("history/", views.HistoryView.as_view(), name="history"),
    path("report/", views.DailyReportView.as_view(), name="report"),
    path("check-in/<int:member_pk>/", views.CheckInView.as_view(), name="check_in"),
    path("<int:pk>/check-out/", views.CheckOutView.as_view(), name="check_out"),
    path("members/<int:member_pk>/", views.MemberAttendanceView.as_view(), name="member_history"),
    path("members/<int:member_pk>/qr/", views.MemberQRView.as_view(), name="qr_card"),
    path("members/<int:member_pk>/qr.png", views.MemberQRImageView.as_view(), name="qr_image"),
    path("members/<int:member_pk>/qr/regenerate/", views.RegenerateQRView.as_view(), name="qr_regenerate"),
]

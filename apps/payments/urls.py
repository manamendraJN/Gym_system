from django.urls import path

from . import views

app_name = "payments"

urlpatterns = [
    path("", views.PaymentListView.as_view(), name="list"),
    path("outstanding/", views.OutstandingView.as_view(), name="outstanding"),
    path("membership/<int:pk>/pay/", views.RecordPaymentView.as_view(), name="record"),
    path("membership/<int:pk>/adjust/", views.AdjustChargeView.as_view(), name="adjust"),
    path("appointment/<int:pk>/pay/", views.RecordAppointmentPaymentView.as_view(), name="record_appointment"),
    path("appointment/<int:pk>/adjust/", views.AdjustAppointmentChargeView.as_view(), name="adjust_appointment"),
    path("<int:pk>/", views.PaymentDetailView.as_view(), name="detail"),
    path("<int:pk>/refund/", views.RefundView.as_view(), name="refund"),
    path("<int:pk>/confirm/", views.ConfirmPaymentView.as_view(), name="confirm"),
    path("<int:pk>/reject/", views.RejectPaymentView.as_view(), name="reject"),
    path("<int:pk>/void/", views.VoidPaymentView.as_view(), name="void"),
    path("<int:pk>/receipt/", views.ReceiptView.as_view(), name="receipt"),
    path("<int:pk>/receipt.pdf", views.ReceiptPDFView.as_view(), name="receipt_pdf"),
]

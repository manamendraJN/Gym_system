"""External copies of notifications (e-mail, SMS, WhatsApp).

Nothing here is required for the application to work: with no credentials configured no external delivery is
ever created and the in-app notification centre is the only channel. Statuses are reported honestly:

  queued     waiting for the next `deliver_queued` run
  sent       the provider ACCEPTED the message (SMTP server / Twilio API). This is not proof it arrived.
  delivered  only when the provider confirms it (Twilio status callback). E-mail never reaches this state.
  failed     the provider rejected it or all retries failed
  skipped    not attempted (e.g. no address on file); development e-mail backends never create deliveries
"""
import base64
import hashlib
import hmac
import json
import urllib.error
import urllib.parse
import urllib.request

from django.conf import settings
from django.core.mail import EmailMessage
from django.utils import timezone

from .models import Channel, DeliveryStatus, NotificationDelivery, NotificationPreference, NotificationSettings

DEV_EMAIL_BACKENDS = ("console", "locmem", "dummy", "filebased")
MAX_ATTEMPTS = 3


# ------------------------------------------------------------------------------------------ configuration
def email_configured():
    backend = settings.EMAIL_BACKEND.lower()
    return not any(name in backend for name in DEV_EMAIL_BACKENDS)


def _twilio_ready():
    return bool(settings.TWILIO_ACCOUNT_SID and settings.TWILIO_AUTH_TOKEN)


def sms_configured():
    return settings.SMS_PROVIDER == "twilio" and _twilio_ready() and bool(settings.TWILIO_SMS_FROM)


def whatsapp_configured():
    return settings.WHATSAPP_PROVIDER == "twilio" and _twilio_ready() and bool(settings.TWILIO_WHATSAPP_FROM)


def configured_channels():
    return {Channel.EMAIL: email_configured(), Channel.SMS: sms_configured(), Channel.WHATSAPP: whatsapp_configured()}


# ---------------------------------------------------------------------------------------------- addresses
def phone_for(user):
    """E.164 number for SMS/WhatsApp from the user's (or linked member's) phone, or ''."""
    raw = user.phone or ""
    if not raw:
        profile = getattr(user, "member_profile", None)
        raw = profile.phone if profile else ""
    digits = "".join(ch for ch in raw if ch.isdigit())
    if not digits:
        return ""
    if raw.strip().startswith("+"):
        return "+" + digits
    country = settings.DEFAULT_PHONE_COUNTRY_CODE
    return f"{country}{digits[1:]}" if digits.startswith("0") else f"{country}{digits}"


def preference_for(user):
    pref, _ = NotificationPreference.objects.get_or_create(user=user)
    return pref


def queue_deliveries(notification):
    """Create QUEUED deliveries for every channel that is (a) configured, (b) switched on by the business, (c) allowed
    by the user and (d) has an address. Returns the created deliveries."""
    user, conf, pref = notification.user, NotificationSettings.load(), None
    pref = preference_for(user)
    available = configured_channels()
    plan = [
        (Channel.EMAIL, available[Channel.EMAIL] and conf.email_enabled and pref.email_enabled, user.email),
        (Channel.SMS, available[Channel.SMS] and conf.sms_enabled and pref.sms_enabled, phone_for(user)),
        (Channel.WHATSAPP, available[Channel.WHATSAPP] and conf.whatsapp_enabled and pref.whatsapp_enabled, phone_for(user)),
    ]
    created = []
    for channel, allowed, address in plan:
        if not allowed:
            continue
        delivery, was_created = NotificationDelivery.objects.get_or_create(
            notification=notification, channel=channel,
            defaults={"address": address or "", "status": DeliveryStatus.QUEUED if address else DeliveryStatus.SKIPPED,
                      "detail": "" if address else "No address on file."})
        if was_created:
            created.append(delivery)
    return created


# ---------------------------------------------------------------------------------------------- sending
def _body(notification):
    return f"{notification.message}\n\n— {settings.GYM_NAME}"


def _send_email(delivery):
    EmailMessage(subject=delivery.notification.title, body=_body(delivery.notification), to=[delivery.address]).send(fail_silently=False)
    return DeliveryStatus.SENT, "", "Accepted by the mail server. Delivery to the inbox is not confirmed."


def _twilio_request(delivery, whatsapp):
    sid, token = settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN
    prefix = "whatsapp:" if whatsapp else ""
    data = {"To": prefix + delivery.address, "From": settings.TWILIO_WHATSAPP_FROM if whatsapp else settings.TWILIO_SMS_FROM,
            "Body": _body(delivery.notification)[:1500]}
    if whatsapp and not data["From"].startswith("whatsapp:"):
        data["From"] = "whatsapp:" + data["From"]
    if settings.TWILIO_WEBHOOK_URL:
        data["StatusCallback"] = settings.TWILIO_WEBHOOK_URL
    request = urllib.request.Request(
        f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json", data=urllib.parse.urlencode(data).encode(), method="POST")
    credentials = base64.b64encode(f"{sid}:{token}".encode()).decode()
    request.add_header("Authorization", f"Basic {credentials}")
    with urllib.request.urlopen(request, timeout=15) as response:  # noqa: S310 (fixed https URL)
        return json.loads(response.read().decode())


def _send_twilio(delivery, whatsapp):
    payload = _twilio_request(delivery, whatsapp)
    callback = " Delivery is confirmed by the provider's status callback." if settings.TWILIO_WEBHOOK_URL else " Delivery confirmation is not configured."
    return DeliveryStatus.SENT, payload.get("sid", ""), f"Accepted by Twilio (status: {payload.get('status', 'queued')})." + callback


def process_queue(limit=100):
    """Attempt queued deliveries. Returns counts by resulting status."""
    counts = {s: 0 for s in DeliveryStatus.values}
    for delivery in NotificationDelivery.objects.filter(status=DeliveryStatus.QUEUED).select_related("notification__user")[:limit]:
        delivery.attempts += 1
        try:
            if delivery.channel == Channel.EMAIL:
                status, message_id, detail = _send_email(delivery)
            elif delivery.channel == Channel.SMS:
                status, message_id, detail = _send_twilio(delivery, False)
            else:
                status, message_id, detail = _send_twilio(delivery, True)
            delivery.status, delivery.provider_message_id, delivery.detail, delivery.sent_at = status, message_id, detail, timezone.now()
        except urllib.error.HTTPError as exc:
            retry = exc.code >= 500 and delivery.attempts < MAX_ATTEMPTS
            delivery.status = DeliveryStatus.QUEUED if retry else DeliveryStatus.FAILED
            delivery.detail = f"Provider rejected the message (HTTP {exc.code})."[:255]
        except Exception as exc:  # network / SMTP errors
            retry = delivery.attempts < MAX_ATTEMPTS
            delivery.status = DeliveryStatus.QUEUED if retry else DeliveryStatus.FAILED
            delivery.detail = f"Could not send: {type(exc).__name__}"[:255]
        delivery.save()
        counts[delivery.status] += 1
    return counts


# --------------------------------------------------------------------------------- provider confirmation
def twilio_signature(url, params, token):
    payload = url + "".join(f"{key}{params[key]}" for key in sorted(params))
    digest = hmac.new(token.encode(), payload.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


def valid_twilio_signature(url, params, signature, token):
    return bool(signature) and hmac.compare_digest(twilio_signature(url, params, token), signature)


def apply_twilio_status(message_sid, status):
    """Update a delivery from a verified Twilio callback. Returns True if a delivery matched."""
    delivery = NotificationDelivery.objects.filter(provider_message_id=message_sid).first()
    if delivery is None:
        return False
    if status in ("delivered", "read"):
        delivery.status, delivery.delivered_at, delivery.detail = DeliveryStatus.DELIVERED, timezone.now(), "Delivery confirmed by Twilio."
    elif status in ("failed", "undelivered"):
        delivery.status, delivery.detail = DeliveryStatus.FAILED, f"Twilio reported: {status}."
    else:
        return True  # queued / sending / sent: still only "handed to provider"
    delivery.save()
    return True

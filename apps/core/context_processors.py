from django.conf import settings


def site(request):
    from .gym import gym

    current = gym()
    ctx = {"GYM_NAME": current.name, "GYM": current, "NUTRITION_ENABLED": settings.NUTRITION_ENABLED, "INVENTORY_ENABLED": settings.INVENTORY_ENABLED}
    user = getattr(request, "user", None)
    if user is not None and user.is_authenticated:
        from .models import Notification

        unread = Notification.objects.filter(user=user, read_at__isnull=True)
        ctx["unread_notifications"] = unread.count()
        ctx["recent_notifications"] = list(unread[:5])
    return ctx

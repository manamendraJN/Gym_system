"""Cache-based login throttling keyed on username + client IP."""
from django.conf import settings
from django.core.cache import cache


def _key(username, ip):
    return f"login-fail:{(username or '').lower()}:{ip}"


def client_ip(request):
    return request.META.get("REMOTE_ADDR", "")


def is_locked(username, ip):
    return cache.get(_key(username, ip), 0) >= settings.LOGIN_MAX_ATTEMPTS


def register_failure(username, ip):
    key = _key(username, ip)
    cache.add(key, 0, settings.LOGIN_LOCKOUT_SECONDS)
    try:
        cache.incr(key)
    except ValueError:
        cache.set(key, 1, settings.LOGIN_LOCKOUT_SECONDS)


def reset(username, ip):
    cache.delete(_key(username, ip))

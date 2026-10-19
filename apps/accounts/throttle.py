"""Cache-based login throttling keyed on username + client IP."""
from django.conf import settings
from django.core.cache import cache


def _key(username, ip):
    return f"login-fail:{(username or '').lower()}:{ip}"


def client_ip(request):
    """Client address for throttling and audit. Behind a reverse proxy set USE_X_FORWARDED_FOR=True so the real client
    (first X-Forwarded-For entry written by YOUR proxy) is used; leave it off when the app is exposed directly, because
    the header is then client-controlled."""
    if settings.USE_X_FORWARDED_FOR:
        forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "").split(",")[0].strip()
        if forwarded:
            return forwarded[:45]
    return request.META.get("REMOTE_ADDR", "")


def is_locked(username, ip):
    return cache.get(_key(username, ip), 0) >= settings.LOGIN_MAX_ATTEMPTS


def register_failure(username, ip):
    key = _key(username, ip)
    cache.add(key, 0, settings.LOGIN_LOCKOUT_SECONDS)
    try:
        return cache.incr(key)
    except ValueError:
        cache.set(key, 1, settings.LOGIN_LOCKOUT_SECONDS)
        return 1


def reset(username, ip):
    cache.delete(_key(username, ip))

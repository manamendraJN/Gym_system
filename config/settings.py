"""Django settings. Configuration comes from environment variables (see .env.example)."""
import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def env_bool(name, default=False):
    return os.environ.get(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def env_list(name, default=""):
    return [v.strip() for v in os.environ.get(name, default).split(",") if v.strip()]


DEBUG = env_bool("DEBUG", False)

SECRET_KEY = os.environ.get("SECRET_KEY", "")
if not SECRET_KEY:
    raise ImproperlyConfigured("The SECRET_KEY environment variable is required (see .env.example).")

ALLOWED_HOSTS = env_list("ALLOWED_HOSTS", "localhost,127.0.0.1")
CSRF_TRUSTED_ORIGINS = env_list("CSRF_TRUSTED_ORIGINS")

GYM_NAME = os.environ.get("GYM_NAME", "IronForge Gym")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "apps.accounts",
    "apps.core",
    "apps.members",
    "apps.memberships",
    "apps.attendance",
    "apps.payments",
    "apps.trainers",
    "apps.appointments",
    "apps.workouts",
    "apps.progress",
    "apps.nutrition",
    "apps.portal",
    "apps.suppliers",
    "apps.expenses",
    "apps.equipment",
    "apps.inventory",
    "apps.announcements",
    "apps.notifications",
    "apps.reports",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    # Every view requires login unless decorated with @login_not_required.
    "django.contrib.auth.middleware.LoginRequiredMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "apps.core.context_processors.site",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

# Behind a reverse proxy that overwrites X-Forwarded-For, set this so throttling / audit see the real client address.
USE_X_FORWARDED_FOR = env_bool("USE_X_FORWARDED_FOR", False)

# --- Database: SQLite for local development, PostgreSQL for production ---
if os.environ.get("DB_ENGINE", "sqlite").lower() in {"postgresql", "postgres"}:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.environ.get("DB_NAME", "gym_system"),
            "USER": os.environ.get("DB_USER", "postgres"),
            "PASSWORD": os.environ.get("DB_PASSWORD", ""),
            "HOST": os.environ.get("DB_HOST", "localhost"),
            "PORT": os.environ.get("DB_PORT", "5432"),
            "CONN_MAX_AGE": 60,
            "CONN_HEALTH_CHECKS": True,
            "OPTIONS": {"sslmode": os.environ["DB_SSLMODE"]} if os.environ.get("DB_SSLMODE") else {},
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": os.environ.get("SQLITE_PATH") or BASE_DIR / "db.sqlite3",
        }
    }

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --- Cache (login throttling, etc.). Use Redis in production so all worker processes share counters. ---
if os.environ.get("REDIS_URL"):
    CACHES = {"default": {"BACKEND": "django.core.cache.backends.redis.RedisCache", "LOCATION": os.environ["REDIS_URL"]}}
else:
    CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}

# --- Authentication ---
AUTH_USER_MODEL = "accounts.User"
LOGIN_URL = "accounts:login"
LOGIN_REDIRECT_URL = "core:home"
LOGOUT_REDIRECT_URL = "accounts:login"
PASSWORD_RESET_TIMEOUT = 60 * 60 * 3  # reset links valid for 3 hours

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 10}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# Brute-force protection (see apps/accounts/throttle.py)
LOGIN_MAX_ATTEMPTS = 5
LOGIN_LOCKOUT_SECONDS = 15 * 60

# --- Members & memberships (business rules, see README) ---
MEMBER_ID_PREFIX = "GM"
MEMBERSHIP_EXPIRING_SOON_DAYS = 14   # window for "expiring soon" and the dashboard
MEMBERSHIP_MAX_ADVANCE_START_DAYS = 60  # a new membership may start at most this far ahead
MEMBERSHIP_MAX_SUSPENSION_DAYS = 90  # suspended time credited back to the end date is capped at this

# --- Attendance & payments ---
ATTENDANCE_MAX_SESSION_HOURS = 12   # an open check-in older than this is auto-closed so a missed check-out cannot lock a member out
ATTENDANCE_BLOCK_UNPAID = False     # True: members whose membership has no payment recorded cannot check in
ATTENDANCE_SCAN_COOLDOWN_SECONDS = 60  # a second scan this soon after check-in is treated as accidental (no check-out)
QR_SCAN_MAX_FAILURES = 10           # failed QR lookups per staff user ...
QR_SCAN_WINDOW_SECONDS = 300        # ... within this window before scanning is paused
PAYMENT_MAX_BACKDATE_DAYS = 90

# --- Trainers, appointments, workouts, nutrition ---
PRIVATE_MEDIA_ROOT = BASE_DIR / "private_media"  # certificates etc.; never served directly, only through permission-checked views
NUTRITION_ENABLED = env_bool("NUTRITION_ENABLED", False)  # optional module; off by default
APPOINTMENT_DURATIONS = [30, 45, 60, 90]        # minutes a session may last
APPOINTMENT_SLOT_STEP_MINUTES = 15              # start times must fall on this grid
APPOINTMENT_MIN_LEAD_MINUTES = 60               # members must book at least this far ahead (staff may book sooner)
APPOINTMENT_MAX_ADVANCE_DAYS = 60
APPOINTMENT_CHANGE_NOTICE_HOURS = 12            # members may reschedule/cancel only this long before the start
APPOINTMENT_REMINDER_HOURS = 24                 # reminders go out this long before the start
APPOINTMENT_REQUIRE_MEMBERSHIP = True          # booking requires a membership covering the session date

# --- Equipment, inventory, expenses, notifications (Phase 5) ---
INVENTORY_ENABLED = env_bool("INVENTORY_ENABLED", True)  # optional: set False to hide inventory entirely

# External message providers. Everything defaults to "none" so the application works with no credentials; the
# in-app notification centre is always available. See README "Notifications and external messaging".
SMS_PROVIDER = os.environ.get("SMS_PROVIDER", "none").lower()            # none | twilio
WHATSAPP_PROVIDER = os.environ.get("WHATSAPP_PROVIDER", "none").lower()  # none | twilio
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_SMS_FROM = os.environ.get("TWILIO_SMS_FROM", "")
TWILIO_WHATSAPP_FROM = os.environ.get("TWILIO_WHATSAPP_FROM", "")  # e.g. whatsapp:+14155238886
DEFAULT_PHONE_COUNTRY_CODE = os.environ.get("DEFAULT_PHONE_COUNTRY_CODE", "+94")  # prefix for local numbers starting with 0
TWILIO_WEBHOOK_URL = os.environ.get("TWILIO_WEBHOOK_URL", "")      # public https URL of /notifications/webhooks/twilio/

# Optional Celery (scheduled tasks). Without a broker the same jobs run via `manage.py run_scheduled_notifications`.
CELERY_BROKER_URL = os.environ.get("CELERY_BROKER_URL", "")
CELERY_RESULT_BACKEND = os.environ.get("CELERY_RESULT_BACKEND", "")

# --- Internationalisation ---
LANGUAGE_CODE = "en-us"
TIME_ZONE = os.environ.get("TIME_ZONE", "Asia/Colombo")
USE_I18N = True
USE_TZ = True

# --- Static & media ---
STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"
MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"
        if DEBUG
        else "whitenoise.storage.CompressedManifestStaticFilesStorage"
    },
}

# --- Email ---
EMAIL_BACKEND = os.environ.get("EMAIL_BACKEND", "django.core.mail.backends.console.EmailBackend")
EMAIL_HOST = os.environ.get("EMAIL_HOST", "")
EMAIL_PORT = int(os.environ.get("EMAIL_PORT", "587"))
EMAIL_HOST_USER = os.environ.get("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = os.environ.get("EMAIL_HOST_PASSWORD", "")
EMAIL_USE_TLS = env_bool("EMAIL_USE_TLS", True)
DEFAULT_FROM_EMAIL = os.environ.get("DEFAULT_FROM_EMAIL", "Gym System <no-reply@example.com>")

# --- Security ---
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_AGE = 60 * 60 * 12
X_FRAME_OPTIONS = "DENY"
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"

if not DEBUG:
    SECURE_SSL_REDIRECT = env_bool("SECURE_SSL_REDIRECT", True)
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = 60 * 60 * 24 * 30
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = env_bool("SECURE_HSTS_PRELOAD", False)
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

# Message tags map onto Bootstrap alert classes.
from django.contrib.messages import constants as message_constants  # noqa: E402

MESSAGE_TAGS = {message_constants.ERROR: "danger"}

# Fast password hashing keeps the test suite quick; never used outside `manage.py test`.
import sys  # noqa: E402

if "test" in sys.argv[1:2]:
    PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

# --- Logging: to the console (captured by systemd / Docker); never includes request bodies or passwords ---
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"plain": {"format": "%(asctime)s %(levelname)s %(name)s: %(message)s"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "plain"}},
    "root": {"handlers": ["console"], "level": os.environ.get("LOG_LEVEL", "INFO")},
    "loggers": {"django.security": {"handlers": ["console"], "level": "WARNING", "propagate": False}},
}

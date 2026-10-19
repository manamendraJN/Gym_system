try:  # Celery is optional; the project runs without it.
    from .celery import app as celery_app

    __all__ = ("celery_app",)
except ImportError:  # pragma: no cover - celery not installed
    pass

"""Env-driven Django settings."""

from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parent.parent

env = environ.Env()
# load local .env when running outside docker; compose injects vars in-container
environ.Env.read_env(BASE_DIR.parent / ".env")

SECRET_KEY = env("DJANGO_SECRET_KEY", default="dev-insecure-change-me")
DEBUG = env.bool("DJANGO_DEBUG", default=False)
ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS", default=["localhost", "127.0.0.1", "backend"])
CSRF_TRUSTED_ORIGINS = env.list(
    "DJANGO_CSRF_TRUSTED_ORIGINS",
    default=["http://localhost", "http://localhost:5173"],
)

INSTALLED_APPS = [
    # must be first so it wraps the db and cache backends
    "django_prometheus",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "accounts",
    "menu",
    "orders",
    "ops",
]

MIDDLEWARE = [
    "django_prometheus.middleware.PrometheusBeforeMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "config.middleware.ApiWriteGuard",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "django_prometheus.middleware.PrometheusAfterMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

DATABASES = {
    "default": {
        **env.db(
            "DATABASE_URL",
            default="postgres://asian:asian@db:5432/asian_restaurant",
        ),
        # ASGI runs every request in its own thread, and a persistent connection stays with the thread that opened it
        "CONN_MAX_AGE": 0,
    }
}
DATABASES["default"]["ENGINE"] = "django_prometheus.db.backends.postgresql"
# per process. the sum over every storefront process is budgeted in DEPLOY.md and checked by the gate
DATABASES["default"].setdefault("OPTIONS", {})["pool"] = {
    "min_size": 1,
    "max_size": env.int("DJANGO_DB_POOL_MAX_SIZE", default=2),
    "timeout": 5,
}

# every redis client of the process: a stalled redis costs a request at most a second of its thread, its concurrency slot and the pooled database connection it may hold
REDIS_CLIENT_OPTIONS = {
    "socket_connect_timeout": 1.0,
    "socket_timeout": 1.0,
    "health_check_interval": 30,
}

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": env("REDIS_URL", default="redis://redis:6379/1"),
        "OPTIONS": REDIS_CLIENT_OPTIONS,
    }
}

CART_REDIS_URL = env("CART_REDIS_URL", default="redis://redis:6379/0")

# only trust X-Forwarded-For for client-ip rate limiting behind a proxy that
# strips and re-sets it; otherwise fall back to REMOTE_ADDR
RATELIMIT_TRUST_XFF = env.bool("RATELIMIT_TRUST_XFF", default=False)

# server-side delivery address verification (Nominatim by default)
GEOCODER_ENABLED = env.bool("GEOCODER_ENABLED", default=True)
GEOCODER_URL = env("GEOCODER_URL", default="https://nominatim.openstreetmap.org/search")
GEOCODER_USER_AGENT = env("GEOCODER_USER_AGENT", default="AsianRestaurant/1.0 (+delivery verification)")
GEOCODER_TIMEOUT = env.int("GEOCODER_TIMEOUT", default=5)

RABBITMQ_URL = env("RABBITMQ_URL", default="amqp://guest:guest@rabbitmq:5672/")

# Identity signs short-lived staff JWTs; the private key stays here, only the JWKS is public
IDENTITY_JWT_PRIVATE_KEY = env("IDENTITY_JWT_PRIVATE_KEY", default="")
IDENTITY_JWT_PRIVATE_KEY_FILE = env("IDENTITY_JWT_PRIVATE_KEY_FILE", default="")
IDENTITY_JWT_KID = env("IDENTITY_JWT_KID", default="dev-1")
IDENTITY_JWT_TTL = env.int("IDENTITY_JWT_TTL", default=600)
# previous public key kept in the JWKS during rotation so in-flight tokens still verify
IDENTITY_JWT_PREVIOUS_PUBLIC_KEY = env("IDENTITY_JWT_PREVIOUS_PUBLIC_KEY", default="")
IDENTITY_JWT_PREVIOUS_PUBLIC_KEY_FILE = env("IDENTITY_JWT_PREVIOUS_PUBLIC_KEY_FILE", default="")
IDENTITY_JWT_PREVIOUS_KID = env("IDENTITY_JWT_PREVIOUS_KID", default="")

def _is_placeholder(value: str) -> bool:
    """
    a value the repository could have shipped as an example, by shape rather than by list

    naming the two historical defaults let every placeholder added afterwards boot production
    """
    lowered = str(value).strip().lower()
    return "change-me" in lowered or lowered.startswith(("dev-", "ops-dev"))

DJANGO_PRODUCTION = env.bool("DJANGO_PRODUCTION", default=False)
if DJANGO_PRODUCTION:
    import os

    from django.core.exceptions import ImproperlyConfigured

    if _is_placeholder(SECRET_KEY):
        raise ImproperlyConfigured("DJANGO_SECRET_KEY must be set in production")
    if DEBUG:
        raise ImproperlyConfigured("DJANGO_DEBUG must be off in production")
    if "*" in ALLOWED_HOSTS:
        raise ImproperlyConfigured("DJANGO_ALLOWED_HOSTS must be restricted in production")
    if _is_placeholder(IDENTITY_JWT_KID):
        raise ImproperlyConfigured("production must not use the development signing kid")
    # the signing key comes from a readable secret file (Docker/K8s secret), not an env var
    if IDENTITY_JWT_PRIVATE_KEY:
        raise ImproperlyConfigured("do not pass IDENTITY_JWT_PRIVATE_KEY inline; mount a secret file")
    if not (IDENTITY_JWT_PRIVATE_KEY_FILE and os.access(IDENTITY_JWT_PRIVATE_KEY_FILE, os.R_OK)):
        raise ImproperlyConfigured("IDENTITY_JWT_PRIVATE_KEY_FILE must point to a readable secret")

AUTH_USER_MODEL = "accounts.User"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 8},
    },
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "uk"
TIME_ZONE = "Europe/Kyiv"
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_SECURE = env.bool("DJANGO_COOKIE_SECURE", default=not DEBUG)
CSRF_COOKIE_SECURE = env.bool("DJANGO_COOKIE_SECURE", default=not DEBUG)
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
# public keys only: operations fetches them from backend:8000 inside the network, where there is no tls to redirect to
SECURE_REDIRECT_EXEMPT = [r"^api/auth/jwks$", r"^metrics$"]

if not DEBUG:
    # on unless deliberately disabled; SECURE_PROXY_SSL_HEADER above is what makes it correct behind the TLS ingress, and a stack reachable over plain HTTP is the failure this prevents
    SECURE_SSL_REDIRECT = env.bool("DJANGO_SSL_REDIRECT", default=True)
    SECURE_HSTS_SECONDS = env.int("DJANGO_HSTS_SECONDS", default=3600)
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_CONTENT_TYPE_NOSNIFF = True

PROMETHEUS_EXPORT_MIGRATIONS = False

LOG_LEVEL = env("DJANGO_LOG_LEVEL", default="INFO")

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "json": {"()": "config.jsonlog.JsonFormatter"},
    },
    "filters": {
        "redact_bodies": {"()": "config.jsonlog.RedactBodies"},
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "filters": ["redact_bodies"],
            "formatter": "json",
        },
    },
    "root": {"handlers": ["console"], "level": LOG_LEVEL},
    "loggers": {
        "django": {"handlers": ["console"], "level": LOG_LEVEL, "propagate": False},
        "django.request": {"handlers": ["console"], "level": "ERROR", "propagate": False},
        "asian_restaurant": {"handlers": ["console"], "level": LOG_LEVEL, "propagate": False},
        # one INFO block per connection and channel, which buries the records that matter
        "pika": {"handlers": ["console"], "level": "WARNING", "propagate": False},
    },
}

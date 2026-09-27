from pathlib import Path

import environ
from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent

env = environ.Env()
environ.Env.read_env(BASE_DIR / ".env")

SECRET_KEY = env("OPERATIONS_SECRET_KEY", default="ops-dev-insecure-change-me")
DEBUG = env.bool("OPERATIONS_DEBUG", default=False)
ALLOWED_HOSTS = env.list("OPERATIONS_ALLOWED_HOSTS", default=["*"])
def _is_placeholder(value: str) -> bool:
    """
    a value the repository could have shipped as an example, by shape rather than by list

    naming the two historical defaults let every placeholder added afterwards boot production
    """
    lowered = str(value).strip().lower()
    return "change-me" in lowered or lowered.startswith(("dev-", "ops-dev"))

PRODUCTION = env.bool("OPERATIONS_PRODUCTION", default=False)

if PRODUCTION:
    # refuse to boot production with dev fallbacks that would weaken auth or host checks
    if _is_placeholder(SECRET_KEY):
        raise ImproperlyConfigured("OPERATIONS_SECRET_KEY must be set (no dev fallback) in production")
    if "*" in ALLOWED_HOSTS:
        raise ImproperlyConfigured("OPERATIONS_ALLOWED_HOSTS must be restricted in production")
    if DEBUG:
        raise ImproperlyConfigured("OPERATIONS_DEBUG must be off in production")

INSTALLED_APPS = ["operations"]

MIDDLEWARE = ["django.middleware.common.CommonMiddleware"]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {},
    }
]

DATABASES = {"default": env.db("OPERATIONS_DATABASE_URL")}
# ASGI runs every request in its own thread: connections come from a bounded pool per process and go back when the request ends
DATABASES["default"]["CONN_MAX_AGE"] = 0
DATABASES["default"].setdefault("OPTIONS", {})["pool"] = {
    "min_size": 1,
    "max_size": env.int("OPERATIONS_DB_POOL_MAX_SIZE", default=2),
    "timeout": 5,
}

RABBITMQ_URL = env("OPERATIONS_RABBITMQ_URL", default="amqp://guest:guest@rabbitmq:5672/")

# bridge spans vhosts: consumes storefront, publishes operations — distinct creds each side
BRIDGE_CONSUME_URL = env("OPERATIONS_BRIDGE_CONSUME_URL", default=RABBITMQ_URL)
BRIDGE_PUBLISH_URL = env("OPERATIONS_BRIDGE_PUBLISH_URL", default=RABBITMQ_URL)

# command publisher connects with its own write-only credential, never the consumer one
COMMANDS_RABBITMQ_URL = env("OPERATIONS_COMMANDS_RABBITMQ_URL", default="")

IDENTITY_JWKS_URL = env("IDENTITY_JWKS_URL", default="")

# counters of new commands per employee and per employee and order, used by the api alone; without a store it creates no command
REDIS_URL = env("OPERATIONS_REDIS_URL", default="")
COMMAND_LIMIT_WINDOW_SECONDS = env.int("OPERATIONS_COMMAND_LIMIT_WINDOW_SECONDS", default=60)
COMMAND_LIMIT_PER_ACTOR = env.int("OPERATIONS_COMMAND_LIMIT_PER_ACTOR", default=30)
COMMAND_LIMIT_PER_ORDER = env.int("OPERATIONS_COMMAND_LIMIT_PER_ORDER", default=5)

# every process exports its own counters on this port, since none can see another's
METRICS_PORT = env.int("OPERATIONS_METRICS_PORT", default=9101)

LOG_LEVEL = env("OPERATIONS_LOG_LEVEL", default="INFO")

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"json": {"()": "operations.jsonlog.JsonFormatter"}},
    "filters": {"redact_bodies": {"()": "operations.jsonlog.RedactBodies"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "json",
                             "filters": ["redact_bodies"]}},
    "root": {"handlers": ["console"], "level": LOG_LEVEL},
    "loggers": {
        "django": {"handlers": ["console"], "level": LOG_LEVEL, "propagate": False},
        "django.request": {"handlers": ["console"], "level": "ERROR", "propagate": False},
        "operations": {"handlers": ["console"], "level": LOG_LEVEL, "propagate": False},
        # pika reports every connection and channel at INFO, which buries the service log
        "pika": {"handlers": ["console"], "level": "WARNING", "propagate": False},
    },
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
USE_TZ = True
TIME_ZONE = "UTC"
LANGUAGE_CODE = "en-us"
STATIC_URL = "static/"

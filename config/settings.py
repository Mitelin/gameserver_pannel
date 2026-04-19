"""
config/settings.py

Základní nastavení pro gameserver panel.
Citlivé hodnoty načti z .env nebo os.environ.
"""
import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

load_dotenv(BASE_DIR / ".env")

SECRET_KEY = os.environ.get("SECRET_KEY", "change-me-in-production")
DEBUG      = os.environ.get("DEBUG", "false").lower() == "true"
ALLOWED_HOSTS = os.environ.get("ALLOWED_HOSTS", "localhost 127.0.0.1").split()
USE_SQLITE = os.environ.get("USE_SQLITE", "false").lower() == "true"
# InMemory channel layer se použije pokud není REDIS_URL nastavená nebo USE_INMEMORY_CHANNEL_LAYER=true
# InMemory channel layer se použije pokud není REDIS_URL nastavená nebo USE_INMEMORY_CHANNEL_LAYER=true
_redis_url = os.environ.get("REDIS_URL", "")
USE_INMEMORY_CHANNEL_LAYER = (
    os.environ.get("USE_INMEMORY_CHANNEL_LAYER", "false").lower() == "true"
    or not _redis_url
)

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",

    # Third party
    "channels",

    # Project apps
    "apps.setup",
    "apps.users",
    "apps.servers",
    "apps.console",
    "apps.control",
    "apps.audit",
    "apps.metrics",
    "apps.alerts",
    "apps.dashboard",
    "apps.common",
    "apps.filemanager",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "apps.setup.middleware.BootstrapMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF   = "config.urls"
ASGI_APPLICATION = "config.asgi.application"
WSGI_APPLICATION = "config.wsgi.application"

TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "DIRS":    [BASE_DIR / "templates"],
    "APP_DIRS": True,
    "OPTIONS": {
        "context_processors": [
            "django.template.context_processors.debug",
            "django.template.context_processors.request",
            "django.contrib.auth.context_processors.auth",
            "django.contrib.messages.context_processors.messages",
        ],
    },
}]

# ── Databáze ──────────────────────────────────────────────────
# SQLite se použije pokud: USE_SQLITE=true NEBO není nastavené DB_PASSWORD.
# PostgreSQL se použije pouze pokud je DB_PASSWORD explicitně nastavené (produkce).
_db_password = os.environ.get("DB_PASSWORD", "")
if USE_SQLITE or not _db_password:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE":   "django.db.backends.postgresql",
            "NAME":     os.environ.get("DB_NAME",     "gameserver_panel"),
            "USER":     os.environ.get("DB_USER",     "mcpanel"),
            "PASSWORD": _db_password,
            "HOST":     os.environ.get("DB_HOST",     "127.0.0.1"),
            "PORT":     os.environ.get("DB_PORT",     "5432"),
        }
    }

# ── Cache + Redis ─────────────────────────────────────────────
REDIS_URL = _redis_url or "redis://127.0.0.1:6379/0"

if USE_INMEMORY_CHANNEL_LAYER:
    # Dev mode – žádný Redis, cache v paměti procesu
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        }
    }
else:
    CACHES = {
        "default": {
            "BACKEND":  "django.core.cache.backends.redis.RedisCache",
            "LOCATION": REDIS_URL,
        }
    }

if USE_INMEMORY_CHANNEL_LAYER:
    CHANNEL_LAYERS = {
        "default": {
            "BACKEND": "channels.layers.InMemoryChannelLayer",
        }
    }
else:
    CHANNEL_LAYERS = {
        "default": {
            "BACKEND": "channels_redis.core.RedisChannelLayer",
            "CONFIG":  {"hosts": [REDIS_URL]},
        }
    }

# ── Auth ──────────────────────────────────────────────────────
LOGIN_URL          = "/accounts/login/"
LOGIN_REDIRECT_URL = "/"
LOGOUT_REDIRECT_URL = "/accounts/login/"

# ── Static ────────────────────────────────────────────────────
STATIC_URL  = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]

# ── Misc ──────────────────────────────────────────────────────
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
LANGUAGE_CODE      = "cs"
TIME_ZONE          = "Europe/Prague"
USE_TZ             = True

# ── Logging ───────────────────────────────────────────────────
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "{levelname} {asctime} {module} {message}",
            "style":  "{",
        },
    },
    "handlers": {
        "console": {
            "class":     "logging.StreamHandler",
            "formatter": "verbose",
        },
    },
    "root": {
        "handlers": ["console"],
        "level":    "INFO",
    },
    "loggers": {
        "django": {"handlers": ["console"], "level": "WARNING", "propagate": False},
        "apps":   {"handlers": ["console"], "level": "DEBUG",   "propagate": False},
    },
}

# Error page templates
# Django uses these automatically when DEBUG=False
# handler404, handler500 etc. are set in urls.py

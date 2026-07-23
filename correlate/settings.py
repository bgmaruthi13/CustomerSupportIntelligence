"""
Django settings for correlate project.

All environment-specific values (secret key, debug flag, allowed hosts, database)
are read from environment variables / a local .env file (see .env.example) so the
same codebase runs unmodified in dev and production — only the .env differs.
"""

import os
from pathlib import Path

import dj_database_url
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def env_bool(name, default=False):
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


def env_list(name, default=""):
    raw = os.environ.get(name, default)
    return [item.strip() for item in raw.split(",") if item.strip()]


# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "django-insecure-dev-only-do-not-use-in-production")

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = env_bool("DJANGO_DEBUG", default=True)

if not DEBUG and SECRET_KEY == "django-insecure-dev-only-do-not-use-in-production":
    raise RuntimeError(
        "DJANGO_DEBUG=False but DJANGO_SECRET_KEY is unset — set a real secret key in .env "
        "before running in production (see .env.example)."
    )

ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1")
CSRF_TRUSTED_ORIGINS = env_list("DJANGO_CSRF_TRUSTED_ORIGINS")


# Application definition

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'core',
    'tickets',
    'clustering',
    'logscan',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'correlate.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'core.context_processors.site_settings_context',
                'core.context_processors.tenant_context',
            ],
        },
    },
]

WSGI_APPLICATION = 'correlate.wsgi.application'


# Database
# https://docs.djangoproject.com/en/6.0/ref/settings/#databases
# Zero-config default: SQLite (fine for a single-machine / pilot deployment).
# Set DATABASE_URL (e.g. postgres://user:pass@host:5432/dbname) to use Postgres instead.

DATABASES = {
    'default': dj_database_url.config(
        env='DATABASE_URL',
        default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}",
        conn_max_age=600,
    )
}


# Password validation
# https://docs.djangoproject.com/en/6.0/ref/settings/#auth-password-validators

AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
        'OPTIONS': {'min_length': 10},
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]


# Internationalization
# https://docs.djangoproject.com/en/6.0/topics/i18n/

LANGUAGE_CODE = 'en-us'

TIME_ZONE = 'UTC'

USE_I18N = True

USE_TZ = True


# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/6.0/howto/static-files/
# WhiteNoise serves compressed, hashed static files directly from the Django process —
# no separate nginx/IIS step needed for a single-machine Windows deployment.

STATIC_URL = 'static/'
STATICFILES_DIRS = [BASE_DIR / 'static']
STATIC_ROOT = BASE_DIR / 'staticfiles'
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}

# Media (uploaded CSV/Excel files). Intentionally NEVER served over a URL — uploaded
# ticket data is only ever read server-side (see tickets.ingestion.read_uploaded_file).
# This avoids exposing potentially sensitive source files (e.g. real ITSM exports)
# through a predictable /media/ path, in prod or dev.
MEDIA_URL = 'media/'
MEDIA_ROOT = BASE_DIR / 'media'

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

LOGIN_URL = 'login'
LOGIN_REDIRECT_URL = 'dashboard'
LOGOUT_REDIRECT_URL = 'login'

# DATA_UPLOAD_MAX_MEMORY_SIZE is a hard request-size cap (RequestDataTooBig
# above it) — it's global, not per-view, so it has to be sized for the
# largest legitimate upload anywhere in the app. That's now logscan's log
# file uploads (see LogSource, source_type="upload"), not ticket exports —
# ticket uploads keep their own 25MB guard as an explicit check in
# tickets.views.upload instead of relying on this global setting.
# FILE_UPLOAD_MAX_MEMORY_SIZE stays small on purpose: it's the memory/disk
# streaming crossover for Django's upload handler chain, not a size limit —
# a low value means large files start streaming to a temp file sooner rather
# than buffering in memory, which is what a multi-GB upload wants regardless.
LOGSCAN_UPLOAD_MAX_MEMORY_SIZE = int(os.environ.get("LOGSCAN_UPLOAD_MAX_MB", "2048")) * 1024 * 1024
DATA_UPLOAD_MAX_MEMORY_SIZE = LOGSCAN_UPLOAD_MAX_MEMORY_SIZE
FILE_UPLOAD_MAX_MEMORY_SIZE = 25 * 1024 * 1024


# Email — used only by logscan's alert digests (logscan.alerts.send_scan_digest),
# sent when a scan finds anything on a Log Source with alert_emails configured.
# Nothing else in this app sends email. Falls back to Django's console backend
# (prints to the server log) when DJANGO_EMAIL_HOST is unset, so the digest
# content can be exercised/tested without real SMTP configured — alerts.py
# already treats send failures as non-fatal (logged, not raised) regardless.
if os.environ.get("DJANGO_EMAIL_HOST"):
    EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
    EMAIL_HOST = os.environ["DJANGO_EMAIL_HOST"]
    EMAIL_PORT = int(os.environ.get("DJANGO_EMAIL_PORT", "587"))
    EMAIL_HOST_USER = os.environ.get("DJANGO_EMAIL_HOST_USER", "")
    EMAIL_HOST_PASSWORD = os.environ.get("DJANGO_EMAIL_HOST_PASSWORD", "")
    EMAIL_USE_TLS = env_bool("DJANGO_EMAIL_USE_TLS", default=True)
else:
    EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
DEFAULT_FROM_EMAIL = os.environ.get("DJANGO_ALERT_FROM_EMAIL", "correlate-alerts@localhost")


# Security hardening — applied whenever DEBUG=False. Cookie/HSTS settings assume the
# app sits behind HTTPS (either directly or via a reverse proxy); set DJANGO_BEHIND_TLS=False
# if this instance is intentionally plain-HTTP on a trusted internal network only.
BEHIND_TLS = env_bool("DJANGO_BEHIND_TLS", default=True)

if not DEBUG:
    SECURE_CONTENT_TYPE_NOSNIFF = True
    SECURE_REFERRER_POLICY = "same-origin"
    X_FRAME_OPTIONS = "DENY"
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    CSRF_COOKIE_SAMESITE = "Lax"

    if BEHIND_TLS:
        SECURE_SSL_REDIRECT = True
        SESSION_COOKIE_SECURE = True
        CSRF_COOKIE_SECURE = True
        SECURE_HSTS_SECONDS = 31536000
        SECURE_HSTS_INCLUDE_SUBDOMAINS = True
        SECURE_HSTS_PRELOAD = True
        SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')


# Logging — console always; rotating file handler under logs/ so errors survive a
# service restart. Django's own request logger is wired to both so 500s are captured.
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'verbose': {
            'format': '{asctime} {levelname} {name} {message}',
            'style': '{',
        },
    },
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
            'formatter': 'verbose',
        },
        'file': {
            'class': 'logging.handlers.RotatingFileHandler',
            'filename': LOG_DIR / 'correlate.log',
            'maxBytes': 5 * 1024 * 1024,
            'backupCount': 5,
            'formatter': 'verbose',
        },
    },
    'root': {
        'handlers': ['console', 'file'],
        'level': 'INFO',
    },
    'loggers': {
        'django.request': {
            'handlers': ['console', 'file'],
            'level': 'ERROR',
            'propagate': False,
        },
        'django.security': {
            'handlers': ['console', 'file'],
            'level': 'WARNING',
            'propagate': False,
        },
    },
}

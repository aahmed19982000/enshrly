import environ
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

env = environ.Env()
# Loads BASE_DIR/.env into os.environ if the file exists — it's gitignored and
# holds real local values; nothing here should ever hardcode a real secret again.
environ.Env.read_env(str(BASE_DIR / '.env'))

# SECURITY WARNING: keep the secret key used in production secret!
# No real key ships in source anymore — set DJANGO_SECRET_KEY in `.env` (see
# .env.example). The fallback below is an obviously-fake dev-only placeholder.
SECRET_KEY = env('DJANGO_SECRET_KEY', default='django-insecure-CHANGE-ME-in-your-local-.env-file')

# SECURITY WARNING: don't run with DEBUG turned on in production!
# Defaults to True so local development keeps working out of the box — set
# DJANGO_DEBUG=False in `.env` for any real deployment (DEBUG=True lets any
# unhandled error page dump this whole settings module, secrets included).
DEBUG = env.bool('DJANGO_DEBUG', default=True)

# Defaults to '*' for local dev convenience; set DJANGO_ALLOWED_HOSTS to a
# comma-separated list of real hostnames for any deployment.
ALLOWED_HOSTS = env.list('DJANGO_ALLOWED_HOSTS', default=['*'])

# Fernet key encrypting WordPressSite.application_password in the DB. Must be
# set in `.env` — there is no safe hardcoded fallback for this one. Do NOT
# rotate an existing value without re-encrypting already-stored rows first.
FIELD_ENCRYPTION_KEY = env('FIELD_ENCRYPTION_KEY', default='')

# Nginx terminates TLS and proxies to Gunicorn over plain HTTP, setting
# X-Forwarded-Proto — without this, Django never sees a request as secure and
# SECURE_SSL_REDIRECT below would redirect forever.
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')

# Production hardening — all default to safe values for real HTTPS deployments,
# but stay off while DEBUG is on so plain http://localhost development keeps working.
# Override via `.env` for a deployment that's DEBUG=False but not yet on HTTPS.
SECURE_SSL_REDIRECT = env.bool('DJANGO_SECURE_SSL_REDIRECT', default=not DEBUG)
SESSION_COOKIE_SECURE = env.bool('DJANGO_SESSION_COOKIE_SECURE', default=not DEBUG)
CSRF_COOKIE_SECURE = env.bool('DJANGO_CSRF_COOKIE_SECURE', default=not DEBUG)
SECURE_HSTS_SECONDS = env.int('DJANGO_SECURE_HSTS_SECONDS', default=0 if DEBUG else 31536000)
SECURE_HSTS_INCLUDE_SUBDOMAINS = not DEBUG
SECURE_HSTS_PRELOAD = not DEBUG

INSTALLED_APPS = [
    'modeltranslation',  # Must be before admin
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'django.contrib.sitemaps',
    # Third-Party
    'mptt',
    'rest_framework',
    'taggit',
    'sorl.thumbnail',
    'django_ckeditor_5',
    'guardian',
    'compressor',
    'django_celery_beat',
    # App
    'syndicator',
    'accounts',
    'payments',
    'landing',
    'pages',
    'blog',
    'faq',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.locale.LocaleMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'enshrly.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'pages.context_processors.support_whatsapp',
            ],
        },
    },
]

WSGI_APPLICATION = 'enshrly.wsgi.application'

# Defaults to local SQLite for zero-config local dev; set DATABASE_URL in
# `.env` (e.g. postgres://user:pass@localhost:5432/dbname) for any real deployment.
DATABASES = {
    'default': env.db('DATABASE_URL', default=f'sqlite:///{BASE_DIR / "db.sqlite3"}')
}

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

LANGUAGE_CODE = 'ar'
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True

LANGUAGES = [
    ('ar', '????'),
    ('en', 'English'),
]

MODELTRANSLATION_DEFAULT_LANGUAGE = 'ar'
MODELTRANSLATION_LANGUAGES = ('ar', 'en')

STATIC_URL = 'static/'
STATICFILES_DIRS = [
    BASE_DIR / 'static',
]
STATIC_ROOT = BASE_DIR / 'staticfiles'

# Content-hashed static filenames (style.a1b2c3.css) so a deploy that changes
# a CSS/JS file is picked up immediately instead of waiting out whatever
# cache lifetime the front-end web server sets for /static/ - the mobile nav
# fix silently failing to show up after a full deploy (server confirmed
# up to date) was exactly this: browsers kept serving their previously
# cached copy of the old enshrly.css because the URL never changed.
STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
    "staticfiles": {
        "BACKEND": "django.contrib.staticfiles.storage.ManifestStaticFilesStorage",
    },
}

MEDIA_URL = 'media/'
MEDIA_ROOT = BASE_DIR / 'media'

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

AUTHENTICATION_BACKENDS = (
    'django.contrib.auth.backends.ModelBackend',
    'guardian.backends.ObjectPermissionBackend',
)

STATICFILES_FINDERS = [
    'django.contrib.staticfiles.finders.FileSystemFinder',
    'django.contrib.staticfiles.finders.AppDirectoriesFinder',
    'compressor.finders.CompressorFinder',
]

COMPRESS_ENABLED = True

CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
        'LOCATION': 'unique-snowflake',
    }
}

REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': (
        'syndicator.auth.APITokenAuthentication',
        'rest_framework.authentication.SessionAuthentication',
        'rest_framework.authentication.BasicAuthentication',
    ),
}

CKEDITOR_5_CONFIGS = {
    'default': {
        'language': 'ar',
        'toolbar': ['heading', '|', 'bold', 'italic', 'link', 'bulletedList', 'numberedList', 'blockQuote', 'imageUpload', 'alignment'],
    }
}
CKEDITOR_5_FILE_UPLOAD_PERMISSION = "staff"

# ALWAYS_EAGER defaults to True so local dev runs tasks inline without needing
# Redis/a worker; set CELERY_TASK_ALWAYS_EAGER=False in `.env` once a real
# Celery worker + beat are running (see systemd services in production).
CELERY_BROKER_URL = env('CELERY_BROKER_URL', default='redis://localhost:6379/0')
CELERY_RESULT_BACKEND = env('CELERY_RESULT_BACKEND', default='redis://localhost:6379/0')
CELERY_TASK_ALWAYS_EAGER = env.bool('CELERY_TASK_ALWAYS_EAGER', default=True)

# Without this, Django's default logging config only sends WARNING+ to the
# console when DEBUG=True (the built-in 'console' handler has a
# require_debug_true filter) - with DEBUG=False in production, every
# logger.info()/logger.warning()/logger.error() call anywhere in the app
# (views, webhooks, background pipelines) vanishes silently instead of
# reaching `journalctl -u gunicorn`. Celery's own logs were never affected -
# it configures its own root logger independently via --loglevel on the
# systemd command, which is why celery-worker logs already showed up while
# gunicorn's never did.
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
        },
    },
    'root': {
        'handlers': ['console'],
        'level': 'INFO',
    },
    'loggers': {
        'django': {
            'handlers': ['console'],
            'level': 'WARNING',
            'propagate': False,
        },
    },
}

LOGIN_URL = '/auth/login/'
LOGIN_REDIRECT_URL = '/ai-dashboard/'
LOGOUT_REDIRECT_URL = '/auth/login/'

# رابط بوت واتساب المحلي (Baileys) لإشعارات الدفع/التجديد — فاضي = محاكاة فقط (يطبع في اللوج)
WHATSAPP_BOT_URL = env('WHATSAPP_BOT_URL', default='')

# إرسال كود التحقق (OTP) بالإيميل. بدون EMAIL_HOST يطبع الكود في الـ console فقط (تطوير محلي).
EMAIL_HOST = env('EMAIL_HOST', default='')
if EMAIL_HOST:
    EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'
    EMAIL_PORT = env.int('EMAIL_PORT', default=587)
    EMAIL_USE_TLS = env.bool('EMAIL_USE_TLS', default=True)
    EMAIL_HOST_USER = env('EMAIL_HOST_USER', default='')
    EMAIL_HOST_PASSWORD = env('EMAIL_HOST_PASSWORD', default='')
    DEFAULT_FROM_EMAIL = env('DEFAULT_FROM_EMAIL', default=EMAIL_HOST_USER)
else:
    EMAIL_BACKEND = 'django.core.mail.backends.console.EmailBackend'
    DEFAULT_FROM_EMAIL = 'no-reply@sahafihub.com'

# إعدادات استقبال وتأكيد مدفوعات المحافظ الإلكترونية
WALLET_API_KEY = env('WALLET_API_KEY', default='')
WALLET_NUMBER = env('WALLET_NUMBER', default='')  # رقم فودافون كاش الخاص بالإدارة لاستلام الأموال

# رمز الإقران المُضمَّن في كود QR — يجب تقديمه في confirm_pairing قبل تسليم
# WALLET_API_KEY لتطبيق الموبايل (بدلاً من تسليمه لأي طلب بلا تحقق)
PAIRING_TOKEN = env('PAIRING_TOKEN', default='')

# إعدادات بوابة الدفع PayPal
PAYPAL_CLIENT_ID = env('PAYPAL_CLIENT_ID', default='')
PAYPAL_CLIENT_SECRET = env('PAYPAL_CLIENT_SECRET', default='')
PAYPAL_MODE = env('PAYPAL_MODE', default='live')  # live أو sandbox

# إعدادات بوابة الدفع Paymob (البطاقات البنكية المصرية)
PAYMOB_API_KEY = env('PAYMOB_API_KEY', default='')
PAYMOB_HMAC_KEY = env('PAYMOB_HMAC_KEY', default='')
PAYMOB_CARD_INTEGRATION_ID = env('PAYMOB_CARD_INTEGRATION_ID', default='5792603')
PAYMOB_IFRAME_ID = env('PAYMOB_IFRAME_ID', default='1063704')  # معرف إطار الدفع الافتراضي، يمكن تعديله لاحقاً

# الرابط الأساسي العلني للخادم — يُستخدم لبناء redirect_uri الخاص بربط فيسبوك
# OAuth ولبناء روابط صور مشاركة السوشال ميديا المُرسَلة لـ Facebook Graph API.
SITE_BASE_URL = env('SITE_BASE_URL', default='http://localhost:8000')

# ربط صفحات فيسبوك الذاتي (OAuth) لخدمة النشر التلقائي على فيسبوك — يُسجَّل
# التطبيق على developers.facebook.com، ويجب ضبط "Valid OAuth Redirect URI"
# هناك على {SITE_BASE_URL}/facebook/connect/callback/
FACEBOOK_APP_ID = env('FACEBOOK_APP_ID', default='')
FACEBOOK_APP_SECRET = env('FACEBOOK_APP_SECRET', default='')

# HTTP(S) proxy used only for AISource rows with use_proxy=True - for feeds
# that block this server's own IP (403 / redirect loops) but work fine from
# anywhere else. Standard requests-style proxy URL, e.g.
# http://username:password@proxy-host:port - works with most proxy
# providers (Webshare, Smartproxy, Bright Data, ...) unchanged. Left empty,
# use_proxy is silently ignored and sources are fetched directly as before.
SCRAPING_PROXY_URL = env('SCRAPING_PROXY_URL', default='')







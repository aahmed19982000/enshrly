import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = 'django-insecure-@jpjci660fly+yozge(d1&m8c#i2kdze&*h^_3l%'
DEBUG = True
ALLOWED_HOSTS = ['*']

# Encryption Key for Fernet
FIELD_ENCRYPTION_KEY = '5q3zduDPj233xFGBU_U5zY41OsqhA-kGOEgnb3PAwTg='

INSTALLED_APPS = [
    'modeltranslation',  # Must be before admin
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
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
            ],
        },
    },
]

WSGI_APPLICATION = 'enshrly.wsgi.application'

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': BASE_DIR / 'db.sqlite3',
    }
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

CELERY_BROKER_URL = 'redis://localhost:6379/0'
CELERY_RESULT_BACKEND = 'redis://localhost:6379/0'
CELERY_TASK_ALWAYS_EAGER = True

LOGIN_REDIRECT_URL = '/ai-dashboard/'
LOGOUT_REDIRECT_URL = '/accounts/login/'

# إعدادات إرسال رسائل الواتساب عبر Infobip
INFOBIP_API_KEY = "342685fbcc1443ad48030552d1bc55a5-9c424797-939d-4193-a33d-0ebe0bcb4649"
INFOBIP_BASE_URL = "https://l2wvnw.api.infobip.com"
INFOBIP_SENDER = "201099437596"  # رقم الواتساب المعتمد المرسل لديهم

# إعدادات استقبال وتأكيد مدفوعات المحافظ الإلكترونية
WALLET_API_KEY = "enshrly_wallet_secret_token_2026"
WALLET_NUMBER = "201099437596"  # رقم فودافون كاش الخاص بالإدارة لاستلام الأموال

# إعدادات بوابة الدفع PayPal
PAYPAL_CLIENT_ID = os.environ.get("PAYPAL_CLIENT_ID", "BAAXNuYKO3tXHVroFwYNOs9qhkQ6vzvCRq4fWJMcV4DQRH-dokEht49LqsdZfu2_-_BiJ_NBw0aekSbE3k")
PAYPAL_CLIENT_SECRET = os.environ.get("PAYPAL_CLIENT_SECRET", "EIm18HKYeMy2Zj9AG-KAEUk3wso3DB9Y2mi6_EH_Sh8uKAekQXE6_CUYIujVJkaKaHrc66WB3tuxV-h0")
PAYPAL_MODE = os.environ.get("PAYPAL_MODE", "live")  # live أو sandbox

# إعدادات بوابة الدفع Paymob (البطاقات البنكية المصرية)
PAYMOB_API_KEY = os.environ.get("PAYMOB_API_KEY", "ZXlKaGJHY2lPaUpJVXpVeE1pSXNJblI1Y0NJNklrcFhWQ0o5LmV5SmpiR0Z6Y3lJNklrMWxjbU5vWVc1MElpd2ljSEp2Wm1sc1pWOXdheUk2TVRJd016VTJPQ3dpYm1GdFpTSTZJbWx1YVhScFlXd2lmUS5GQjh0Q09MYzZiWXN1U24tTHE1REpUUUFNZTNfbEFQbklsZTh5SXNZOERRQ3hQYm9PeHFzc3BUYmlWcGJOeDFFRlRQc1ZBQ3VOcGI1a1ZtdkJ1U0U5UQ==")
PAYMOB_HMAC_KEY = os.environ.get("PAYMOB_HMAC_KEY", "B7CA9CE2BA9F576F12EE90EA2A9BDCCA")
PAYMOB_CARD_INTEGRATION_ID = os.environ.get("PAYMOB_CARD_INTEGRATION_ID", "5792603")
PAYMOB_IFRAME_ID = os.environ.get("PAYMOB_IFRAME_ID", "1063704")  # معرف إطار الدفع الافتراضي، يمكن تعديله لاحقاً







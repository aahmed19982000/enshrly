import requests
from django.conf import settings
from django.core.cache import cache
import logging

logger = logging.getLogger(__name__)


def get_client_ip(request):
    """Best-effort client IP, honoring a reverse proxy's X-Forwarded-For if present."""
    forwarded = request.META.get('HTTP_X_FORWARDED_FOR')
    if forwarded:
        return forwarded.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR', 'unknown')


def check_rate_limit(key, limit, window_seconds):
    """
    Fixed-window rate limiter backed by Django's cache (no extra dependency).
    Returns True if `key` has already hit `limit` attempts within the current
    window and this call should be blocked; otherwise counts this call and
    returns False. Not perfectly atomic under heavy concurrency, which is fine
    for abuse throttling — it doesn't need to be a hard security boundary.
    """
    cache_key = f'ratelimit:{key}'
    count = cache.get(cache_key, 0)
    if count >= limit:
        return True
    cache.set(cache_key, count + 1, timeout=window_seconds)
    return False

def send_whatsapp_otp(phone_number, otp_code):
    """
    Sends an OTP code via WhatsApp using Infobip API.
    """
    api_key = getattr(settings, 'INFOBIP_API_KEY', '')
    base_url = getattr(settings, 'INFOBIP_BASE_URL', '')
    sender_number = getattr(settings, 'INFOBIP_SENDER', '')

    if not api_key or not base_url:
        logger.warning(f"Infobip credentials missing. Simulated OTP {otp_code} for {phone_number}")
        # Return True for local development
        return True

    url = f"{base_url}/whatsapp/1/message/text"

    headers = {
        "Authorization": f"App {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json"
    }

    # Ensure phone number is in international format without '+'
    formatted_number = phone_number.replace('+', '').replace(' ', '')
    if formatted_number.startswith('01') and len(formatted_number) == 11:
        formatted_number = '2' + formatted_number

    payload = {
        "from": sender_number,
        "to": formatted_number,
        "content": {
            "text": f"مرحباً بك في خدمة النشر الآلي! كود التفعيل الخاص بك هو: {otp_code}"
        }
    }

    try:
        response = requests.post(url, json=payload, headers=headers)
        response.raise_for_status()
        return True
    except requests.exceptions.RequestException as e:
        logger.error(f"Infobip Error: {str(e)} - {response.text if hasattr(response, 'text') else ''}")
        return False

def send_whatsapp_payment_success(phone_number, client_name, package_name, token_code, days=30):
    """
    Sends a payment confirmation WhatsApp message containing the new connection token.
    """
    api_key = getattr(settings, 'INFOBIP_API_KEY', '')
    base_url = getattr(settings, 'INFOBIP_BASE_URL', '')
    sender_number = getattr(settings, 'INFOBIP_SENDER', '')

    if not api_key or not base_url:
        logger.warning(f"Infobip credentials missing. Simulated payment success WhatsApp message to {phone_number}")
        return True

    url = f"{base_url}/whatsapp/1/message/text"

    headers = {
        "Authorization": f"App {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json"
    }

    formatted_number = phone_number.replace('+', '').replace(' ', '')
    if formatted_number.startswith('01') and len(formatted_number) == 11:
        formatted_number = '2' + formatted_number

    message_text = (
        f"عزيزي {client_name}، تم تأكيد اشتراكك بنجاح! 🎉\n\n"
        f"📦 الباقة: {package_name}\n"
        f"🔑 كود الربط الخاص بك (Token):\n`{token_code}`\n\n"
        f"📅 صلاحية الكود: {days} يوماً من تاريخ اليوم.\n\n"
        f"يرجى نسخ هذا الكود ووضعه في لوحة تحكم موقعك الووردبريس (إضافة Enshrly Connector) لبدء النشر التلقائي فوراً."
    )

    payload = {
        "from": sender_number,
        "to": formatted_number,
        "content": {
            "text": message_text
        }
    }

    try:
        response = requests.post(url, json=payload, headers=headers)
        response.raise_for_status()
        return True
    except requests.exceptions.RequestException as e:
        logger.error(f"Infobip Payment Success WhatsApp Error: {str(e)} - {response.text if hasattr(response, 'text') else ''}")
        return False

def send_whatsapp_renewal_reminder(phone_number, client_name, site_name, days_left, is_trial=False):
    """
    Sends a renewal or free trial expiry reminder message via WhatsApp using Infobip API.
    """
    api_key = getattr(settings, 'INFOBIP_API_KEY', '')
    base_url = getattr(settings, 'INFOBIP_BASE_URL', '')
    sender_number = getattr(settings, 'INFOBIP_SENDER', '')

    if not api_key or not base_url:
        logger.warning(f"Infobip credentials missing. Simulated renewal/trial reminder WhatsApp message to {phone_number}")
        return True

    url = f"{base_url}/whatsapp/1/message/text"

    headers = {
        "Authorization": f"App {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json"
    }

    formatted_number = phone_number.replace('+', '').replace(' ', '')
    if formatted_number.startswith('01') and len(formatted_number) == 11:
        formatted_number = '2' + formatted_number

    if is_trial:
        if days_left > 0:
            message_text = (
                f"عزيزي {client_name}، نود تذكيرك بأن الفترة التجريبية المجانية لموقعك ({site_name}) تنتهي بعد {days_left} أيام. ⚠️\n\n"
                f"لضمان استمرار النشر التلقائي دون توقف، يرجى الترقية والاشتراك في إحدى الباقات المدفوعة عبر لوحة التحكم الخاصة بك.\n\n"
                f"رابط لوحة التحكم للترقية:\nhttps://enshrly.com/accounts/dashboard/"
            )
        else:
            message_text = (
                f"عزيزي {client_name}، لقد انتهت الفترة التجريبية المجانية لموقعك ({site_name}) اليوم! ❌\n\n"
                f"تم إيقاف النشر التلقائي مؤقتاً. يرجى الاشتراك في إحدى باقاتنا للاستمرار في النشر.\n\n"
                f"اشترك الآن بخطوة واحدة من هنا:\nhttps://enshrly.com/accounts/dashboard/"
            )
    else:
        if days_left > 0:
            message_text = (
                f"عزيزي {client_name}، نود تذكيرك بأن اشتراك موقعك ({site_name}) ينتهي بعد {days_left} أيام. ⚠️\n\n"
                f"لتجنب توقف النشر التلقائي للخبر والمقالات، يرجى تجديد اشتراكك في أقرب وقت عبر لوحة التحكم الخاصة بك.\n\n"
                f"رابط لوحة التحكم للتجديد:\nhttps://enshrly.com/accounts/dashboard/"
            )
        else:
            message_text = (
                f"عزيزي {client_name}، نود إعلامك بأن اشتراك موقعك ({site_name}) قد انتهى اليوم! ❌\n\n"
                f"لقد تم إيقاف عمليات النشر التلقائي مؤقتاً لحين تجديد الاشتراك.\n\n"
                f"يمكنك التجديد الآن بخطوة واحدة عبر لوحة التحكم الخاصة بك:\nhttps://enshrly.com/accounts/dashboard/"
            )

    payload = {
        "from": sender_number,
        "to": formatted_number,
        "content": {
            "text": message_text
        }
    }

    try:
        response = requests.post(url, json=payload, headers=headers)
        response.raise_for_status()
        return True
    except requests.exceptions.RequestException as e:
        logger.error(f"Infobip Renewal WhatsApp Error: {str(e)} - {response.text if hasattr(response, 'text') else ''}")
        return False



import requests
from django.conf import settings
from django.core.cache import cache
from django.core.mail import send_mail
from decimal import Decimal
import logging

logger = logging.getLogger(__name__)


def send_otp_email(email, otp_code):
    """
    Sends an OTP code by email. Egyptian carrier/WhatsApp regulations made
    SMS and WhatsApp OTP delivery unreliable without a paid sender
    registration — email needs none of that.
    """
    if not email:
        logger.warning(f"No email on file. Simulated OTP {otp_code}")
        return True

    try:
        send_mail(
            subject="كود التحقق - Sahafi Hub",
            message=f"كود التحقق الخاص بك هو: {otp_code}\n\nصالح لمدة 10 دقائق. لا تشارك هذا الكود مع أي شخص.",
            from_email=None,
            recipient_list=[email],
            fail_silently=False,
        )
        return True
    except Exception as e:
        logger.error(f"Email OTP Error: {str(e)}")
        return False


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

def _send_via_whatsapp_bot(phone_number, text):
    """
    Sends free-text WhatsApp via a local Baileys-based bot (unofficial
    WhatsApp Web client) instead of Infobip — these are business-initiated
    notifications rather than OTP, so the account-ban risk inherent to
    unofficial WhatsApp Web clients is acceptable here, and it needs no
    template approval or sender registration.
    """
    bot_url = getattr(settings, 'WHATSAPP_BOT_URL', '')
    if not bot_url:
        logger.warning(f"WHATSAPP_BOT_URL not set. Simulated WhatsApp message to {phone_number}: {text}")
        return True

    formatted_number = phone_number.replace('+', '').replace(' ', '')
    if formatted_number.startswith('01') and len(formatted_number) == 11:
        formatted_number = '2' + formatted_number

    try:
        response = requests.post(f"{bot_url}/send", json={"to": formatted_number, "text": text}, timeout=10)
        response.raise_for_status()
        return True
    except requests.exceptions.RequestException as e:
        resp_text = response.text if 'response' in locals() and hasattr(response, 'text') else ''
        logger.error(f"WhatsApp bot error for {formatted_number}: {str(e)} - {resp_text}")
        return False

def send_whatsapp_welcome(phone_number, client_name):
    """
    Sent right after a new account is created, before WhatsApp/OTP verification even completes.
    """
    message_text = (
        f"أهلاً بيك يا {client_name} في Sahafi Hub! 🎉\n\n"
        f"دلوقتي تقدر تستعرض باقاتنا وتبدأ النشر التلقائي لموقعك على الووردبريس في دقايق.\n\n"
        f"لوحة التحكم: https://sahafihub.com/auth/dashboard/"
    )
    return _send_via_whatsapp_bot(phone_number, message_text)

def send_whatsapp_payment_failed(phone_number, client_name, package_name):
    """
    Sent when a payment attempt is declined/fails at the gateway (Paymob decline,
    PayPal non-COMPLETED status) — previously these failures were silent to the customer.
    """
    message_text = (
        f"عزيزي {client_name}، للأسف لم تكتمل عملية الدفع الخاصة بباقة \"{package_name}\". ❌\n\n"
        f"يرجى المحاولة مرة أخرى أو استخدام وسيلة دفع مختلفة من لوحة التحكم.\n\n"
        f"لوحة التحكم: https://sahafihub.com/auth/dashboard/"
    )
    return _send_via_whatsapp_bot(phone_number, message_text)

def send_whatsapp_payment_success(phone_number, client_name, package_name, token_code, days=30):
    """
    Sends a payment confirmation WhatsApp message containing the new connection token.
    """
    message_text = (
        f"عزيزي {client_name}، تم تأكيد اشتراكك بنجاح! 🎉\n\n"
        f"📦 الباقة: {package_name}\n"
        f"🔑 كود الربط الخاص بك (Token):\n`{token_code}`\n\n"
        f"📅 صلاحية الكود: {days} يوماً من تاريخ اليوم.\n\n"
        f"يرجى نسخ هذا الكود ووضعه في لوحة تحكم موقعك الووردبريس (إضافة Sahafi Hub Connector) لبدء النشر التلقائي فوراً."
    )
    return _send_via_whatsapp_bot(phone_number, message_text)

def send_whatsapp_underpayment_notice(phone_number, client_name, package_name, required_amount, received_amount, currency):
    """
    Sent when an incoming 'local' wallet transfer is matched to a pending
    transaction by sender phone but falls short of the required amount —
    without this, an underpaid transfer just sits pending with no
    explanation of why it was never auto-confirmed.
    """
    shortfall = Decimal(str(required_amount)) - Decimal(str(received_amount))
    message_text = (
        f"عزيزي {client_name}، استلمنا تحويلك بمبلغ {received_amount} {currency}، "
        f"لكن المبلغ المطلوب لتفعيل باقة \"{package_name}\" هو {required_amount} {currency}. ⚠️\n\n"
        f"يرجى تحويل المبلغ المتبقي وقدره {shortfall} {currency} على نفس رقم المحفظة لإتمام تفعيل اشتراكك."
    )
    return _send_via_whatsapp_bot(phone_number, message_text)

def send_whatsapp_page_closed_notice(phone_number, client_name, amount, currency, wallet_number):
    """
    Sent when a customer navigates away from / closes the local-wallet pending
    checkout page before their transfer was confirmed — reassures them the
    order is still open and confirmation will happen automatically once the
    transfer arrives, instead of leaving them thinking they need to restart.
    """
    message_text = (
        f"عزيزي {client_name}، لاحظنا إنك غادرت صفحة الدفع قبل تأكيد التحويل. 📌\n\n"
        f"يرجى تحويل مبلغ {amount} {currency} إلى رقم المحفظة {wallet_number} إن لم تكن حوّلته بعد، "
        f"وسيتم تأكيد طلبك تلقائياً فور استلام التحويل — هتلاقي التحديث هنا في الواتساب."
    )
    return _send_via_whatsapp_bot(phone_number, message_text)

def send_whatsapp_manual_confirmation_request(phone_number, client_name):
    """
    Sent when a 'local' wallet-transfer transaction hasn't been auto-confirmed
    by the mobile monitor app within a few minutes (likely offline/disconnected).
    Asks the customer to reply with a transfer screenshot for manual confirmation.
    """
    message_text = (
        f"عزيزي {client_name}، شكراً لاستخدامك خدمتنا! 🙏\n\n"
        f"لاحظنا إن التحويل بتاعك لسه محتاج تأكيد. يرجى الرد على هذه الرسالة بصورة سكرين شوت "
        f"لعملية التحويل، وسيتم تأكيد الدفع وتفعيل اشتراكك يدوياً في أقرب وقت. ⚡"
    )
    return _send_via_whatsapp_bot(phone_number, message_text)

def send_whatsapp_renewal_reminder(phone_number, client_name, site_name, days_left, is_trial=False):
    """
    Sends a renewal or free trial expiry reminder message via WhatsApp.
    """
    if is_trial:
        if days_left > 0:
            message_text = (
                f"عزيزي {client_name}، نود تذكيرك بأن الفترة التجريبية المجانية لموقعك ({site_name}) تنتهي بعد {days_left} أيام. ⚠️\n\n"
                f"لضمان استمرار النشر التلقائي دون توقف، يرجى الترقية والاشتراك في إحدى الباقات المدفوعة عبر لوحة التحكم الخاصة بك.\n\n"
                f"رابط لوحة التحكم للترقية:\nhttps://sahafihub.com/auth/dashboard/"
            )
        else:
            message_text = (
                f"عزيزي {client_name}، لقد انتهت الفترة التجريبية المجانية لموقعك ({site_name}) اليوم! ❌\n\n"
                f"تم إيقاف النشر التلقائي مؤقتاً. يرجى الاشتراك في إحدى باقاتنا للاستمرار في النشر.\n\n"
                f"اشترك الآن بخطوة واحدة من هنا:\nhttps://sahafihub.com/auth/dashboard/"
            )
    else:
        if days_left > 0:
            message_text = (
                f"عزيزي {client_name}، نود تذكيرك بأن اشتراك موقعك ({site_name}) ينتهي بعد {days_left} أيام. ⚠️\n\n"
                f"لتجنب توقف النشر التلقائي للخبر والمقالات، يرجى تجديد اشتراكك في أقرب وقت عبر لوحة التحكم الخاصة بك.\n\n"
                f"رابط لوحة التحكم للتجديد:\nhttps://sahafihub.com/auth/dashboard/"
            )
        else:
            message_text = (
                f"عزيزي {client_name}، نود إعلامك بأن اشتراك موقعك ({site_name}) قد انتهى اليوم! ❌\n\n"
                f"لقد تم إيقاف عمليات النشر التلقائي مؤقتاً لحين تجديد الاشتراك.\n\n"
                f"يمكنك التجديد الآن بخطوة واحدة عبر لوحة التحكم الخاصة بك:\nhttps://sahafihub.com/auth/dashboard/"
            )

    return _send_via_whatsapp_bot(phone_number, message_text)



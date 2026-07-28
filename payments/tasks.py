import logging

from celery import shared_task
from django.utils import timezone
from datetime import timedelta

logger = logging.getLogger(__name__)


def _alert_whatsapp_send_exhausted(context, phone_number, client_name):
    """Best-effort Telegram alert to admins once a WhatsApp notification has exhausted all retries and will not be delivered."""
    from syndicator.ai_utils import send_telegram_alert
    send_telegram_alert(
        f"⚠️ فشل إرسال إشعار واتساب ({context}) للعميل {client_name} ({phone_number}) بعد استنفاد كل المحاولات."
    )

# Any pending 'local' wallet transaction older than this with no automatic
# confirmation is assumed to mean the mobile monitor app is offline/disconnected.
PENDING_CONFIRMATION_GRACE_MINUTES = 5


@shared_task
def notify_stale_pending_local_transactions():
    """
    Periodic task: finds 'local' wallet-transfer transactions still 'pending'
    PENDING_CONFIRMATION_GRACE_MINUTES after creation (the mobile monitor app
    never auto-confirmed them) and asks the customer via WhatsApp to send a
    transfer screenshot for manual confirmation instead of leaving them waiting
    silently on the pending-checkout page.
    """
    from .models import Transaction
    from accounts.utils import send_whatsapp_manual_confirmation_request

    cutoff = timezone.now() - timedelta(minutes=PENDING_CONFIRMATION_GRACE_MINUTES)
    stale_transactions = Transaction.objects.filter(
        status='pending',
        gateway='local',
        fallback_notified=False,
        created_at__lte=cutoff,
    ).select_related('customer__user')

    notified = 0
    for tx in stale_transactions:
        if not tx.customer or not tx.customer.whatsapp_number:
            continue
        client_name = tx.customer.user.first_name or tx.customer.user.username
        sent = send_whatsapp_manual_confirmation_request(
            phone_number=tx.customer.whatsapp_number,
            client_name=client_name,
        )
        if sent:
            tx.fallback_notified = True
            tx.save(update_fields=['fallback_notified', 'updated_at'])
            notified += 1

    if notified:
        logger.info(f"Sent manual-confirmation-request WhatsApp message for {notified} stale pending transaction(s)")
    return f"Notified {notified} stale pending transaction(s)"


@shared_task(bind=True, max_retries=3, default_retry_delay=30)
def send_payment_success_whatsapp(self, phone_number, client_name, package_name, token_code, days):
    """
    Sends the payment-success WhatsApp message off the request/webhook thread.
    The WhatsApp bot has been observed to hang on individual /send calls —
    running this inline inside a payment webhook risks delaying the HTTP
    response long enough for the gateway to retry the whole webhook, which is
    exactly what previously caused duplicate WPConnectionToken issuance.
    Retries a few times since a bot hang/timeout is typically transient.
    """
    from accounts.utils import send_whatsapp_payment_success
    sent = send_whatsapp_payment_success(phone_number, client_name, package_name, token_code, days)
    if not sent:
        try:
            raise self.retry()
        except self.MaxRetriesExceededError:
            _alert_whatsapp_send_exhausted("تأكيد الدفع", phone_number, client_name)
            raise


@shared_task(bind=True, max_retries=3, default_retry_delay=30)
def send_payment_failed_whatsapp(self, phone_number, client_name, package_name):
    """Same rationale as send_payment_success_whatsapp — never block a payment webhook on the WhatsApp bot."""
    from accounts.utils import send_whatsapp_payment_failed
    sent = send_whatsapp_payment_failed(phone_number, client_name, package_name)
    if not sent:
        try:
            raise self.retry()
        except self.MaxRetriesExceededError:
            _alert_whatsapp_send_exhausted("فشل الدفع", phone_number, client_name)
            raise


@shared_task(bind=True, max_retries=3, default_retry_delay=30)
def send_underpayment_whatsapp(self, phone_number, client_name, package_name, required_amount, received_amount, currency):
    from accounts.utils import send_whatsapp_underpayment_notice
    sent = send_whatsapp_underpayment_notice(phone_number, client_name, package_name, required_amount, received_amount, currency)
    if not sent:
        try:
            raise self.retry()
        except self.MaxRetriesExceededError:
            _alert_whatsapp_send_exhausted("نقص في المبلغ المحول", phone_number, client_name)
            raise


@shared_task(bind=True, max_retries=3, default_retry_delay=30)
def send_page_closed_whatsapp(self, phone_number, client_name, amount, currency, wallet_number):
    from accounts.utils import send_whatsapp_page_closed_notice
    sent = send_whatsapp_page_closed_notice(phone_number, client_name, amount, currency, wallet_number)
    if not sent:
        try:
            raise self.retry()
        except self.MaxRetriesExceededError:
            _alert_whatsapp_send_exhausted("إغلاق صفحة الدفع", phone_number, client_name)
            raise

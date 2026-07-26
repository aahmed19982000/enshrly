import logging

from celery import shared_task
from django.utils import timezone
from datetime import timedelta

logger = logging.getLogger(__name__)

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

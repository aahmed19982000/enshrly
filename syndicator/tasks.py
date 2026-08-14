import logging
from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=3, default_retry_delay=10)
def increment_views_count(self, article_id):
    """
    Atomically increment the views_count for an article.
    Uses F() expression to avoid race conditions on concurrent reads.
    """
    try:
        from .models import Article
        from django.db.models import F

        updated = Article.objects.filter(pk=article_id).update(
            views_count=F('views_count') + 1
        )
        if updated:
            logger.info(f"Views count incremented for article {article_id}")
        else:
            logger.warning(f"Article {article_id} not found for views increment")
    except Exception as exc:
        logger.error(f"Failed to increment views for article {article_id}: {exc}")
        raise self.retry(exc=exc)


@shared_task(bind=True, max_retries=3, default_retry_delay=30)
def compress_article_image_task(self, article_id):
    """
    Asynchronously compress and convert an article's cover_image to WebP.
    This offloads the Pillow processing from the request/response cycle.
    """
    try:
        from .models import Article
        from core.utils import optimize_image_field

        article = Article.all_objects.get(pk=article_id)
        if article.cover_image and not article.cover_image.name.endswith('.webp'):
            optimize_image_field(article, 'cover_image', max_size=(1200, 1200), quality=85)
            # Save only the cover_image field to avoid triggering full save logic
            Article.all_objects.filter(pk=article_id).update(
                cover_image=article.cover_image.name
            )
            logger.info(f"Cover image compressed for article {article_id}: {article.cover_image.name}")
        else:
            logger.info(f"Article {article_id} cover image already optimized or absent")
    except Exception as exc:
        logger.error(f"Failed to compress image for article {article_id}: {exc}")
        raise self.retry(exc=exc)


@shared_task(bind=True, max_retries=2, default_retry_delay=30)
def generate_and_publish_social_share_task(self, wp_site_id, payload):
    """
    Async wrapper for generate_and_publish_social_share_from_wp_payload -
    moved off wp_post_published_api_view's synchronous request path because
    it can involve a slow source-image download plus a Facebook Graph API
    call, which together can exceed gunicorn's worker timeout (30s default,
    unconfigured on this deployment). A killed worker mid-request never gets
    a chance to log anything or save a SocialSharePost row - the request
    just vanishes with zero trace on either side, which is exactly what was
    observed: the WP plugin's hook fires (logged), the is_active/
    facebook_addon_is_active gate passes (logged), yet no SocialSharePost
    row and no error ever appear. Running this in Celery removes the
    request-time pressure entirely; the underlying pipeline already never
    raises for normal failures (see _build_and_maybe_post, which always
    records failures on the SocialSharePost row itself) - retry here is
    only for truly unexpected crashes (e.g. a DB hiccup), not the composite/
    Facebook-API failures that pipeline already handles internally.
    """
    try:
        from .models import WordPressSite
        from .social_image_utils import generate_and_publish_social_share_from_wp_payload
        wp_site = WordPressSite.objects.get(pk=wp_site_id)
        generate_and_publish_social_share_from_wp_payload(wp_site, payload)
    except WordPressSite.DoesNotExist:
        logger.error(f"generate_and_publish_social_share_task: WordPressSite {wp_site_id} no longer exists")
    except Exception as exc:
        logger.error(f"Facebook social share task failed for wp_site_id={wp_site_id}: {exc}")
        raise self.retry(exc=exc)


@shared_task(bind=True, max_retries=1, default_retry_delay=60)
def scrape_and_generate_news_task(self, target_site_id=None):
    """
    Periodic task to scrape news sources and write unique articles using Gemini AI.

    `target_site_id`: optional. When set, this is a manual "generate now"
    trigger for one specific WordPressSite (see TriggerSiteScraperView)
    instead of the normal all-sites scheduled cycle.
    """
    try:
        from .ai_utils import run_ai_generation_cycle
        count = run_ai_generation_cycle(target_site_id=target_site_id)
        logger.info(f"AI news generation cycle completed. Generated {count} articles.")
        return f"Success: {count} articles generated"
    except Exception as exc:
        logger.error(f"AI news generation cycle failed: {exc}")
        raise self.retry(exc=exc)


@shared_task(bind=True, max_retries=1, default_retry_delay=30)
def redistribute_and_republish_logs_task(self, log_ids, site_counts):
    """
    Runs the bulk log-redistribution in the background instead of inline in
    the request/response cycle - each article involves a full WordPress
    round-trip (image upload, category lookup, draft-then-publish with a
    5s wait for the featured image), so redistributing more than a handful
    at once easily exceeds Cloudflare's gateway timeout if done synchronously
    (see BulkRedistributeLogsView, which now just queues this task).

    `site_counts`: {site_id: count} - how many of the selected failed
    articles each site should get, per admin's explicit manual allocation.
    """
    try:
        from .ai_utils import redistribute_and_republish_logs
        results = redistribute_and_republish_logs(log_ids, site_counts)
        logger.info(f"Bulk redistribute finished: {results}")
        return results
    except Exception as exc:
        logger.error(f"Bulk redistribute task failed: {exc}")
        raise self.retry(exc=exc)

@shared_task
def check_expiring_subscriptions_daily():
    """
    Daily periodic task to find tokens expiring in 3 days, or expired today,
    and send them WhatsApp renewal notifications.
    """
    from django.utils import timezone
    from datetime import timedelta
    from .models import WPConnectionToken
    from django.contrib.auth.models import User
    from accounts.utils import send_whatsapp_renewal_reminder

    today = timezone.now().date()
    
    # helper to find user
    def get_user_for_token(client_name):
        clean_name = client_name.replace("(Free Trial)", "").replace("(Trial)", "").strip()
        user = User.objects.filter(first_name=clean_name).first()
        if not user:
            user = User.objects.filter(username=clean_name).first()
        return user

    # 1. Check for tokens expiring in exactly 3 days
    three_days_later = today + timedelta(days=3)
    expiring_tokens = WPConnectionToken.objects.filter(
        is_used=True,
        expires_at__date=three_days_later
    )
    
    for token in expiring_tokens:
        user = get_user_for_token(token.client_name)
        if user and hasattr(user, 'customer_profile'):
            profile = user.customer_profile
            is_trial = "trial" in token.client_name.lower()
            site_name = token.wp_site.name if token.wp_site else "موقعك"
            send_whatsapp_renewal_reminder(
                phone_number=profile.whatsapp_number,
                client_name=user.first_name or user.username,
                site_name=site_name,
                days_left=3,
                is_trial=is_trial
            )

    # 2. Check for tokens expired today
    expired_tokens = WPConnectionToken.objects.filter(
        is_used=True,
        expires_at__date=today
    )
    
    for token in expired_tokens:
        user = get_user_for_token(token.client_name)
        if user and hasattr(user, 'customer_profile'):
            profile = user.customer_profile
            is_trial = "trial" in token.client_name.lower()
            site_name = token.wp_site.name if token.wp_site else "موقعك"
            send_whatsapp_renewal_reminder(
                phone_number=profile.whatsapp_number,
                client_name=user.first_name or user.username,
                site_name=site_name,
                days_left=0,
                is_trial=is_trial
            )

    return "Renewal checks completed."


@shared_task
def check_source_failure_spikes_hourly():
    """
    Early-warning check that didn't exist during the SCRAPING_PROXY_URL 402
    outage: that ran for weeks, silently failing thousands of fetches,
    because nothing surfaced it besides someone manually exporting and
    reading the operations log. Runs hourly; any AISource with 3+ 'failed'
    AIImportLog entries in the last hour gets emailed to settings.OPS_ALERT_EMAIL
    (if configured) so a new outage - proxy quota, a newly-blocked source,
    anything - gets noticed within the hour instead of weeks later.
    """
    from django.conf import settings
    from django.core.mail import send_mail
    from django.db.models import Count
    from django.utils import timezone
    from datetime import timedelta
    from .models import AIImportLog

    since = timezone.now() - timedelta(hours=1)
    spikes = (
        AIImportLog.objects
        .filter(status='failed', created_at__gte=since, source__isnull=False)
        .values('source__id', 'source__name')
        .annotate(fail_count=Count('id'))
        .filter(fail_count__gte=3)
        .order_by('-fail_count')
    )
    if not spikes:
        return "No source failure spikes in the last hour."

    lines = []
    for row in spikes:
        latest = (
            AIImportLog.objects
            .filter(source_id=row['source__id'], status='failed', created_at__gte=since)
            .order_by('-created_at')
            .values_list('error_message', flat=True)
            .first()
        )
        lines.append(f"- {row['source__name']}: {row['fail_count']} فشل في آخر ساعة - {latest or ''}")
        logger.warning(f"Source failure spike: {row['source__name']} failed {row['fail_count']}x in the last hour")

    if settings.OPS_ALERT_EMAIL:
        send_mail(
            subject=f"تنبيه: {len(lines)} مصدر بيفشل بشكل متكرر",
            message="المصادر دي فشلت 3 مرات أو أكتر في آخر ساعة:\n\n" + "\n".join(lines),
            from_email=None,
            recipient_list=[settings.OPS_ALERT_EMAIL],
            fail_silently=True,
        )

    return f"{len(lines)} source(s) flagged."



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


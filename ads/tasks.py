import logging
from datetime import datetime, timedelta

from celery import shared_task
from django.utils import timezone

from . import marketing_api_client

logger = logging.getLogger(__name__)

# Objective -> (optimization_goal, billing_event) mapping for the 3
# simplified goals the wizard exposes (see ads/models.py::Campaign.OBJECTIVE_CHOICES).
OBJECTIVE_DELIVERY_SETTINGS = {
    'OUTCOME_TRAFFIC': ('LINK_CLICKS', 'LINK_CLICKS'),
    'OUTCOME_ENGAGEMENT': ('POST_ENGAGEMENT', 'IMPRESSIONS'),
    'OUTCOME_AWARENESS': ('REACH', 'IMPRESSIONS'),
}

# MVP targeting default: Egypt (matches the rest of the product - EGP
# pricing, Egyptian wallets/WhatsApp numbers) + Advantage+ automatic
# audience, so a non-expert customer never has to configure targeting
# manually. A country picker is a natural Phase 2 addition to the wizard.
DEFAULT_TARGETING_SPEC = {
    'age_min': 18,
    'age_max': 65,
    'genders': [1, 2],
    'geo_locations': {'countries': ['EG']},
    'targeting_automation': {'advantage_audience': 1},
}


def _to_fb_datetime(date_obj):
    return datetime.combine(date_obj, datetime.min.time()).strftime('%Y-%m-%dT%H:%M:%S+0000')


def _fail_campaign(campaign, message):
    campaign.status = 'error'
    campaign.error_message = message
    campaign.save(update_fields=['status', 'error_message', 'updated_at'])
    logger.error(f"Campaign {campaign.pk} creation failed: {message}")


@shared_task(bind=True, max_retries=2, retry_backoff=True)
def create_campaign_task(self, campaign_id):
    """
    Sequential Graph calls: campaign -> ad set -> creative -> ad. Each
    facebook_*_id is persisted immediately after its own step succeeds, so a
    mid-sequence failure leaves an inspectable partial state instead of
    losing it. Expected Graph API failures (bad params, disapproved
    creative, insufficient permissions) set Campaign.status='error' and
    return without raising - mirroring social_image_utils.py's
    "never raises for normal failures" convention. Only unexpected
    exceptions (DB hiccups, a connection dropped mid-task) bubble up to
    Celery's own retry.
    """
    from .models import Campaign, AdSet, AdCreative, Ad

    try:
        campaign = Campaign.objects.select_related('ad_account', 'ad_account__wp_site', 'article', 'social_share_post').get(pk=campaign_id)
    except Campaign.DoesNotExist:
        logger.error(f"create_campaign_task: Campaign {campaign_id} no longer exists")
        return

    ad_account = campaign.ad_account
    if not ad_account.is_connected:
        _fail_campaign(campaign, 'حساب الإعلانات غير متصل.')
        return

    ad_account_id = ad_account.facebook_ad_account_id
    access_token = ad_account.access_token

    try:
        # Step 1: Campaign
        fb_campaign_id, error = marketing_api_client.create_campaign(
            ad_account_id, access_token, name=campaign.name, objective=campaign.objective,
        )
        if error:
            _fail_campaign(campaign, f'تعذر إنشاء الحملة: {error}')
            return
        campaign.facebook_campaign_id = fb_campaign_id
        campaign.save(update_fields=['facebook_campaign_id', 'updated_at'])

        # Step 2: Ad Set
        optimization_goal, billing_event = OBJECTIVE_DELIVERY_SETTINGS[campaign.objective]
        ad_set = AdSet.objects.create(
            campaign=campaign, optimization_goal=optimization_goal, billing_event=billing_event,
            targeting_spec=DEFAULT_TARGETING_SPEC,
        )
        end_time = _to_fb_datetime(campaign.end_date) if campaign.end_date else None
        fb_adset_id, error = marketing_api_client.create_ad_set(
            ad_account_id, access_token, campaign_id=fb_campaign_id, name=f'{campaign.name} - مجموعة إعلانية',
            daily_budget_cents=int(campaign.daily_budget * 100), optimization_goal=optimization_goal,
            billing_event=billing_event, targeting_spec=DEFAULT_TARGETING_SPEC,
            start_time=_to_fb_datetime(campaign.start_date), end_time=end_time,
        )
        if error:
            ad_set.status = 'error'
            ad_set.save(update_fields=['status'])
            _fail_campaign(campaign, f'تعذر إنشاء المجموعة الإعلانية: {error}')
            return
        ad_set.facebook_adset_id = fb_adset_id
        ad_set.status = 'active'
        ad_set.save(update_fields=['facebook_adset_id', 'status'])

        # Step 3: Creative
        page_id = ad_account.wp_site.facebook_page_id
        social_post = campaign.social_share_post
        if social_post and social_post.facebook_post_id and page_id:
            fb_creative_id, error = marketing_api_client.create_ad_creative_from_existing_post(
                ad_account_id, access_token, page_id, social_post.facebook_post_id, name=campaign.name,
            )
            creative_kwargs = dict(source_type='existing_post', headline=social_post.article_title, destination_url=campaign.destination_url)
        elif campaign.facebook_post_id and page_id:
            fb_creative_id, error = marketing_api_client.create_ad_creative_from_existing_post(
                ad_account_id, access_token, page_id, campaign.facebook_post_id, name=campaign.name,
            )
            creative_kwargs = dict(source_type='existing_post', headline=campaign.name, destination_url=campaign.destination_url)
        elif campaign.article and campaign.article.cover_image and page_id:
            article = campaign.article
            try:
                article.cover_image.open('rb')
                image_bytes = article.cover_image.read()
            finally:
                article.cover_image.close()
            image_hash, error = marketing_api_client.upload_ad_image(ad_account_id, access_token, image_bytes)
            if error:
                _fail_campaign(campaign, f'تعذر رفع صورة الإعلان: {error}')
                return
            fb_creative_id, error = marketing_api_client.create_ad_creative_from_link(
                ad_account_id, access_token, page_id, image_hash, link_url=campaign.destination_url,
                message=article.excerpt or article.title, headline=article.title, name=campaign.name,
            )
            creative_kwargs = dict(source_type='article_cover', headline=article.title, destination_url=campaign.destination_url)
        else:
            _fail_campaign(campaign, 'لا يوجد محتوى صالح للترويج (منشور فيسبوك أو صورة خبر) أو صفحة فيسبوك غير مربوطة.')
            return

        if error:
            _fail_campaign(campaign, f'تعذر إنشاء المحتوى الإعلاني: {error}')
            return
        creative = AdCreative.objects.create(ad_set=ad_set, facebook_creative_id=fb_creative_id, **creative_kwargs)

        # Step 4: Ad
        fb_ad_id, error = marketing_api_client.create_ad(
            ad_account_id, access_token, ad_set_id=fb_adset_id, creative_id=fb_creative_id, name=f'{campaign.name} - إعلان',
        )
        if error:
            _fail_campaign(campaign, f'تعذر إنشاء الإعلان: {error}')
            return
        Ad.objects.create(ad_set=ad_set, creative=creative, facebook_ad_id=fb_ad_id, name=campaign.name, status='active')

        campaign.status = 'active'
        campaign.error_message = ''
        campaign.save(update_fields=['status', 'error_message', 'updated_at'])
        logger.info(f"Campaign {campaign.pk} created successfully on Facebook (fb_campaign_id={fb_campaign_id})")

    except Exception as exc:
        logger.error(f"create_campaign_task crashed unexpectedly for campaign {campaign_id}: {exc}")
        raise self.retry(exc=exc)


@shared_task(bind=True, max_retries=2, retry_backoff=True)
def set_campaign_status_task(self, campaign_id, new_status):
    """
    Cascades pause/resume to the campaign's ad set(s) and ad(s) too - Meta
    does not guarantee that pausing a campaign automatically pauses its
    children the way a customer would expect from the dashboard toggle.
    """
    from .models import Campaign

    try:
        campaign = Campaign.objects.select_related('ad_account').prefetch_related('ad_sets__ads').get(pk=campaign_id)
    except Campaign.DoesNotExist:
        logger.error(f"set_campaign_status_task: Campaign {campaign_id} no longer exists")
        return

    access_token = campaign.ad_account.access_token
    errors = []

    if campaign.facebook_campaign_id:
        success, error = marketing_api_client.set_status(campaign.facebook_campaign_id, access_token, new_status)
        if not success:
            errors.append(error)

    for ad_set in campaign.ad_sets.all():
        if ad_set.facebook_adset_id:
            success, error = marketing_api_client.set_status(ad_set.facebook_adset_id, access_token, new_status)
            if success:
                ad_set.status = 'active' if new_status == 'ACTIVE' else 'paused'
                ad_set.save(update_fields=['status'])
            else:
                errors.append(error)
        for ad in ad_set.ads.all():
            if ad.facebook_ad_id:
                success, error = marketing_api_client.set_status(ad.facebook_ad_id, access_token, new_status)
                if success:
                    ad.status = 'active' if new_status == 'ACTIVE' else 'paused'
                    ad.save(update_fields=['status'])
                else:
                    errors.append(error)

    if errors:
        campaign.error_message = '؛ '.join(errors)
        campaign.save(update_fields=['error_message', 'updated_at'])
    else:
        campaign.status = 'active' if new_status == 'ACTIVE' else 'paused'
        campaign.error_message = ''
        campaign.save(update_fields=['status', 'error_message', 'updated_at'])


@shared_task(bind=True, max_retries=2, retry_backoff=True)
def sync_campaign_insights_task(self, campaign_id=None):
    """
    Periodic (hourly, see the seed migration) daily-performance sync.
    campaign_id=None syncs every currently-active campaign; a specific id is
    used for an on-demand "refresh now" from the campaign detail page.
    """
    from .models import Campaign, AdInsightSnapshot

    campaigns = Campaign.objects.filter(pk=campaign_id) if campaign_id else Campaign.objects.filter(status='active')
    today = timezone.now().date()

    for campaign in campaigns.select_related('ad_account'):
        if not campaign.facebook_campaign_id:
            continue
        rows, error = marketing_api_client.fetch_insights(
            campaign.facebook_campaign_id, campaign.ad_account.access_token,
            since=campaign.start_date, until=today,
        )
        if error:
            logger.warning(f"sync_campaign_insights_task: campaign {campaign.pk} insights fetch failed: {error}")
            continue

        for row in rows or []:
            try:
                date_value = datetime.strptime(row['date_start'], '%Y-%m-%d').date()
            except (KeyError, ValueError):
                continue
            AdInsightSnapshot.objects.update_or_create(
                campaign=campaign, date=date_value,
                defaults={
                    'impressions': int(row.get('impressions', 0) or 0),
                    'reach': int(row.get('reach', 0) or 0),
                    'clicks': int(row.get('clicks', 0) or 0),
                    'spend': row.get('spend', 0) or 0,
                    'ctr': row.get('ctr', 0) or 0,
                    'cpc': row.get('cpc', 0) or 0,
                    'cpm': row.get('cpm', 0) or 0,
                    'raw_response': row,
                },
            )


@shared_task
def poll_campaign_health_task():
    """
    Periodic (~every 15 min): flags campaigns stuck in pending_creation past
    a reasonable timeout as errored (their create_campaign_task presumably
    crashed without a retry left), and re-verifies each connected ad
    account's token so a revoked/expired token surfaces on the dashboard as
    needs_reauth instead of silently failing the next campaign creation.
    """
    from .models import Campaign, AdAccountConnection

    stuck_cutoff = timezone.now() - timedelta(minutes=30)
    stuck = Campaign.objects.filter(status='pending_creation', created_at__lt=stuck_cutoff)
    for campaign in stuck:
        campaign.status = 'error'
        campaign.error_message = 'انتهت مهلة إنشاء الحملة على فيسبوك.'
        campaign.save(update_fields=['status', 'error_message', 'updated_at'])

    for connection in AdAccountConnection.objects.filter(status='connected'):
        valid, error = marketing_api_client.verify_access_token(connection.access_token)
        if valid:
            connection.last_verified_at = timezone.now()
            connection.save(update_fields=['last_verified_at'])
        else:
            connection.status = 'needs_reauth'
            connection.error_message = error or ''
            connection.save(update_fields=['status', 'error_message'])

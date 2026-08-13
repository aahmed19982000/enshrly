import logging
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Sum
from django.http import Http404
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST
from datetime import timedelta

from accounts.views import _customer_site_ids
from syndicator.models import WordPressSite, SocialSharePost, AIImportLog

from . import marketing_api_client
from .models import AdAccountConnection, Campaign

logger = logging.getLogger(__name__)

DURATION_PRESETS = [3, 7, 14, 30]


def _get_profile(request):
    return getattr(request.user, 'customer_profile', None)


def _get_owned_site(request, wp_site_id):
    profile = _get_profile(request)
    if wp_site_id not in _customer_site_ids(profile):
        raise Http404
    return get_object_or_404(WordPressSite, pk=wp_site_id), profile


@login_required
def ads_dashboard_view(request):
    profile = _get_profile(request)
    sites = WordPressSite.objects.filter(id__in=_customer_site_ids(profile))

    sites_data = []
    for site in sites:
        connection = getattr(site, 'ad_account_connection', None)
        sites_data.append({
            'site': site,
            'connection': connection,
            'addon_active': connection.ads_addon_is_active if connection else False,
            'is_connected': connection.is_connected if connection else False,
            'campaign_count': connection.campaigns.count() if connection else 0,
        })

    return render(request, 'ads/dashboard.html', {'sites_data': sites_data})


@login_required
def ad_account_connect_redirect_view(request, wp_site_id):
    site, profile = _get_owned_site(request, wp_site_id)
    connection = getattr(site, 'ad_account_connection', None)

    if not connection or not connection.ads_addon_is_active:
        messages.error(request, "خدمة إدارة الإعلانات غير مفعّلة لهذا الموقع.")
        return redirect('ads:dashboard')

    from .views_ads_connect import make_ads_connect_token
    token = make_ads_connect_token(site.pk)
    return redirect(reverse('ads:ads_connect_start', kwargs={'token': token}))


@login_required
def campaign_list_view(request, wp_site_id):
    site, profile = _get_owned_site(request, wp_site_id)
    connection = getattr(site, 'ad_account_connection', None)
    campaigns = connection.campaigns.all() if connection else Campaign.objects.none()

    return render(request, 'ads/campaign_list.html', {
        'site': site,
        'connection': connection,
        'campaigns': campaigns,
    })


@login_required
def campaign_wizard_view(request, wp_site_id):
    site, profile = _get_owned_site(request, wp_site_id)
    connection, _created = AdAccountConnection.objects.get_or_create(wp_site=site)

    if not connection.ads_addon_is_active:
        messages.error(request, "خدمة إدارة الإعلانات غير مفعّلة لهذا الموقع.")
        return redirect('ads:dashboard')
    if not connection.is_connected:
        messages.error(request, "يرجى ربط حساب إعلانات فيسبوك أولاً.")
        return redirect('ads:dashboard')

    plan = connection.ads_addon_plan
    active_count = connection.campaigns.filter(status__in=['pending_creation', 'active']).count()
    if plan and plan.max_concurrent_active_campaigns and active_count >= plan.max_concurrent_active_campaigns:
        messages.error(request, f"وصلت للحد الأقصى لعدد الحملات النشطة في باقتك ({plan.max_concurrent_active_campaigns}). أوقف حملة قائمة لإنشاء حملة جديدة.")
        return redirect('ads:campaign_list', wp_site_id=site.pk)

    promotable_posts = SocialSharePost.objects.filter(
        wp_site=site, status='posted'
    ).exclude(facebook_post_id='').select_related('article').order_by('-created_at')[:15]

    promotable_articles = AIImportLog.objects.filter(
        wp_site=site, status='success'
    ).exclude(published_url='').select_related('article').order_by('-created_at')[:15]

    promotable_fb_posts = []
    if site.facebook_page_id and site.facebook_access_token:
        fb_posts, error = marketing_api_client.get_page_posts(site.facebook_page_id, site.facebook_access_token)
        if error:
            logger.warning(f"get_page_posts failed for site={site.pk}: {error}")
        else:
            already_tracked = set(promotable_posts.values_list('facebook_post_id', flat=True))
            promotable_fb_posts = [p for p in fb_posts if p['id'] not in already_tracked]

    max_daily_budget = plan.max_daily_budget_usd if plan else None

    if request.method == 'POST':
        promote_choice = request.POST.get('promote', '')
        objective = request.POST.get('objective', '')
        duration_days = request.POST.get('duration_days', '7')
        budget_raw = request.POST.get('daily_budget', '')

        redirect_back = redirect('ads:campaign_wizard', wp_site_id=site.pk)

        if objective not in dict(Campaign.OBJECTIVE_CHOICES):
            messages.error(request, "يرجى اختيار هدف صحيح للحملة.")
            return redirect_back

        try:
            duration_days = int(duration_days)
            if duration_days not in DURATION_PRESETS:
                raise ValueError
        except ValueError:
            messages.error(request, "مدة الحملة غير صحيحة.")
            return redirect_back

        try:
            daily_budget = Decimal(budget_raw)
            if daily_budget <= 0:
                raise InvalidOperation
        except (InvalidOperation, ValueError):
            messages.error(request, "يرجى إدخال ميزانية يومية صحيحة.")
            return redirect_back

        if max_daily_budget and daily_budget > max_daily_budget:
            messages.error(request, f"أقصى ميزانية يومية مسموحة في باقتك هي ${max_daily_budget}.")
            return redirect_back

        kind, _, raw_id = promote_choice.partition(':')
        social_post = None
        article = None
        destination_url = ''
        name_source = ''

        if kind == 'post':
            social_post = get_object_or_404(SocialSharePost, pk=raw_id, wp_site=site, status='posted')
            article = social_post.article
            destination_url = article.get_absolute_url() if article else ''
            name_source = social_post.article_title
        elif kind == 'article':
            log = get_object_or_404(AIImportLog, pk=raw_id, wp_site=site, status='success')
            article = log.article
            destination_url = log.published_url or ''
            name_source = log.title or (article.title if article else '')
        elif kind == 'fbpost':
            fb_post, error = marketing_api_client.get_page_post(site.facebook_page_id, raw_id, site.facebook_access_token)
            if error or not fb_post:
                messages.error(request, "تعذر التحقق من منشور فيسبوك المختار.")
                return redirect_back
            destination_url = fb_post.get('permalink_url', '')
            name_source = (fb_post.get('message') or '')[:80] or 'منشور من صفحة فيسبوك'
        else:
            messages.error(request, "يرجى اختيار محتوى للترويج له.")
            return redirect_back

        is_real_post = (social_post and social_post.facebook_post_id) or kind == 'fbpost'
        if objective == 'OUTCOME_ENGAGEMENT' and not is_real_post:
            messages.error(request, "هدف \"تفاعل أكبر\" متاح فقط عند اختيار منشور فيسبوك منشور بالفعل.")
            return redirect_back

        if not destination_url:
            messages.error(request, "تعذر تحديد رابط الوجهة لهذا المحتوى.")
            return redirect_back

        campaign = Campaign.objects.create(
            ad_account=connection,
            article=article,
            social_share_post=social_post,
            facebook_post_id=raw_id if kind == 'fbpost' else '',
            name=(name_source or 'حملة إعلانية')[:80],
            objective=objective,
            status='pending_creation',
            daily_budget=daily_budget,
            start_date=timezone.now().date(),
            end_date=timezone.now().date() + timedelta(days=duration_days),
            destination_url=destination_url,
            created_by=profile,
        )

        from .tasks import create_campaign_task
        create_campaign_task.delay(campaign.pk)

        messages.success(request, "تم إرسال حملتك للإنشاء على فيسبوك، ستظهر النتيجة خلال دقائق.")
        return redirect('ads:campaign_list', wp_site_id=site.pk)

    return render(request, 'ads/campaign_wizard.html', {
        'site': site,
        'connection': connection,
        'plan': plan,
        'promotable_posts': promotable_posts,
        'promotable_articles': promotable_articles,
        'promotable_fb_posts': promotable_fb_posts,
        'objective_choices': Campaign.OBJECTIVE_CHOICES,
        'duration_presets': DURATION_PRESETS,
        'max_daily_budget': max_daily_budget,
    })


@login_required
def campaign_detail_view(request, wp_site_id, campaign_id):
    site, profile = _get_owned_site(request, wp_site_id)
    campaign = get_object_or_404(Campaign, pk=campaign_id, ad_account__wp_site=site)
    insights = campaign.insights.order_by('-date')[:30]
    totals = campaign.insights.aggregate(
        spend=Sum('spend'), impressions=Sum('impressions'), clicks=Sum('clicks'), reach=Sum('reach'),
    )

    return render(request, 'ads/campaign_detail.html', {
        'site': site,
        'campaign': campaign,
        'insights': insights,
        'totals': totals,
    })


@login_required
@require_POST
def campaign_toggle_status_view(request, wp_site_id, campaign_id):
    site, profile = _get_owned_site(request, wp_site_id)
    campaign = get_object_or_404(Campaign, pk=campaign_id, ad_account__wp_site=site)

    if campaign.status not in ('active', 'paused'):
        messages.error(request, "لا يمكن تغيير حالة هذه الحملة في وضعها الحالي.")
        return redirect('ads:campaign_detail', wp_site_id=site.pk, campaign_id=campaign.pk)

    new_fb_status = 'PAUSED' if campaign.status == 'active' else 'ACTIVE'
    from .tasks import set_campaign_status_task
    set_campaign_status_task.delay(campaign.pk, new_fb_status)

    messages.success(request, "جاري تحديث حالة الحملة...")
    return redirect('ads:campaign_detail', wp_site_id=site.pk, campaign_id=campaign.pk)

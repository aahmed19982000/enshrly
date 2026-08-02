from datetime import datetime
from django.shortcuts import render, redirect
from django.views.generic import ListView, CreateView, UpdateView, DeleteView, View, TemplateView
from django.contrib.auth.mixins import UserPassesTestMixin
from django.contrib.auth import authenticate, login as auth_login
from django.contrib.auth.models import User
from django.urls import reverse_lazy, reverse
from django.contrib import messages
from django.shortcuts import get_object_or_404
from django.utils.http import url_has_allowed_host_and_scheme
from django.core.exceptions import ValidationError
from django.db.models import Sum, Q
from django.http import JsonResponse, HttpResponse, HttpResponseForbidden
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
import json
from django.conf import settings
from .models import AISettings, AISource, AISourceGroup, AIImportLog, Category, Article, WordPressSite, WordPressScheduleSlot, WordPressSiteGroup, SocialSharePost, WPConnectionToken, SourceGroupWPCategoryMap
from .tasks import scrape_and_generate_news_task
from accounts.utils import check_rate_limit, get_client_ip

class StaffRequiredMixin(UserPassesTestMixin):
    """
    Mixin that ensures the user is logged in and is a staff member. Uses the
    dashboard's own dedicated staff login page (news_ai:staff_login) instead
    of the global LOGIN_URL, which is the customer-facing WhatsApp login -
    staff accounts aren't expected to have a WhatsApp/customer profile.
    """
    login_url = reverse_lazy('news_ai:staff_login')

    def test_func(self):
        return self.request.user.is_authenticated and self.request.user.is_staff


def staff_login_view(request):
    """
    Dedicated username/password login for the /ai-dashboard/ staff area,
    kept separate from accounts.login_view's WhatsApp-based customer login -
    staff accounts don't necessarily have a CustomerProfile/WhatsApp number.
    """
    if request.user.is_authenticated and request.user.is_staff:
        return redirect('news_ai:index')

    next_url = request.GET.get('next') or request.POST.get('next') or ''

    if request.method == 'POST':
        username = request.POST.get('username', '').strip()
        password = request.POST.get('password', '')
        client_ip = get_client_ip(request)

        if check_rate_limit(f'stafflogin:{client_ip}:{username}', limit=5, window_seconds=600):
            messages.error(request, "عدد محاولات كبير جداً، يرجى الانتظار قليلاً ثم إعادة المحاولة.")
        else:
            user = authenticate(request, username=username, password=password)
            if user is not None and user.is_staff:
                auth_login(request, user)
                if next_url and url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
                    return redirect(next_url)
                return redirect('news_ai:index')
            messages.error(request, "اسم المستخدم أو كلمة المرور غير صحيحة، أو لا تملك صلاحية الوصول للوحة الإدارة.")

    return render(request, 'ai_dashboard/staff_login.html', {'next': next_url})


class DashboardIndexView(StaffRequiredMixin, TemplateView):
    template_name = 'ai_dashboard/index.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        
        # Stats
        context['settings'] = AISettings.get_settings()
        context['sources_count'] = AISource.objects.count()
        context['active_sources_count'] = AISource.objects.filter(is_active=True).count()
        context['total_articles_generated'] = AIImportLog.objects.filter(status='success').count()
        context['total_failures'] = AIImportLog.objects.filter(status='failed').count()
        
        # Calculate total estimated cost (cached per-row at creation time, summed in the DB)
        from django.db.models import Sum
        context['total_cost'] = AIImportLog.objects.aggregate(total=Sum('estimated_cost'))['total'] or 0
        
        # Lists
        context['recent_articles'] = Article.objects.filter(ai_logs__isnull=False).distinct().order_by('-published_at')[:8]
        context['recent_logs'] = AIImportLog.objects.all().order_by('-created_at')[:10]
        
        return context


class SettingsUpdateView(StaffRequiredMixin, UpdateView):
    model = AISettings
    fields = ['gemini_api_key', 'telegram_bot_token', 'telegram_allowed_chats', 'wallet_number', 'support_whatsapp_number', 'enable_paypal_gateway', 'enable_paymob_gateway', 'enable_local_wallet_gateway', 'articles_per_day', 'max_words', 'is_active', 'publish_to_main_site', 'daily_cost_limit_usd']
    template_name = 'ai_dashboard/settings.html'
    success_url = reverse_lazy('news_ai:index')

    def get_object(self, queryset=None):
        return AISettings.get_settings()

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        from django.contrib.auth.models import User
        from .models import WordPressSite
        context['wp_sites'] = WordPressSite.objects.filter(is_active=True)
        context['all_sources'] = AISource.objects.filter(is_active=True)
        context['local_source_ids'] = list(self.get_object().local_sources.values_list('id', flat=True))
        context['staff_authors'] = User.objects.filter(is_staff=True)
        context['default_author_ids'] = list(self.get_object().default_authors.values_list('id', flat=True))
        from .ai_utils import get_today_total_cost
        context['today_total_cost'] = get_today_total_cost()
        return context

    def form_valid(self, form):
        from .models import WordPressSite
        response = super().form_valid(form)
        
        # Save local_sources M2M selection
        settings_obj = self.get_object()
        selected_local_sources = self.request.POST.getlist('local_sources')
        settings_obj.local_sources.set(
            AISource.objects.filter(id__in=[int(x) for x in selected_local_sources if x.isdigit()])
        )

        # Save default_authors M2M selection
        from django.contrib.auth.models import User
        selected_authors = self.request.POST.getlist('default_authors')
        settings_obj.default_authors.set(
            User.objects.filter(id__in=[int(x) for x in selected_authors if x.isdigit()])
        )

        # Process and save WordPress site limits from POST
        active_sites = WordPressSite.objects.filter(is_active=True)
        for site in active_sites:
            limit_key = f"site_limit_{site.id}"
            if limit_key in self.request.POST:
                try:
                    val = int(self.request.POST[limit_key])
                    if val >= 0:
                        site.daily_limit = val
                        site.save()
                except (ValueError, TypeError):
                    pass
                    
        messages.success(self.request, "تم حفظ الإعدادات بنجاح.")
        return response


class SourceListView(StaffRequiredMixin, ListView):
    model = AISource
    template_name = 'ai_dashboard/sources_list.html'
    context_object_name = 'sources'


class SourceCreateView(StaffRequiredMixin, CreateView):
    model = AISource
    fields = ['name', 'url', 'language', 'group', 'is_active']
    template_name = 'ai_dashboard/source_form.html'
    success_url = reverse_lazy('news_ai:sources')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['grouped_source_groups'] = _source_group_parents_with_cat_values()
        return context

    def form_valid(self, form):
        messages.success(self.request, f"تمت إضافة المصدر '{form.instance.name}' بنجاح.")
        return super().form_valid(form)


class SourceUpdateView(StaffRequiredMixin, UpdateView):
    model = AISource
    fields = ['name', 'url', 'language', 'group', 'is_active']
    template_name = 'ai_dashboard/source_form.html'
    success_url = reverse_lazy('news_ai:sources')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['grouped_source_groups'] = _source_group_parents_with_cat_values()
        return context

    def form_valid(self, form):
        messages.success(self.request, f"تم تحديث المصدر '{form.instance.name}' بنجاح.")
        return super().form_valid(form)


class SourceDeleteView(StaffRequiredMixin, DeleteView):
    model = AISource
    template_name = 'ai_dashboard/source_confirm_delete.html'
    success_url = reverse_lazy('news_ai:sources')

    def delete(self, request, *args, **kwargs):
        obj = self.get_object()
        messages.success(self.request, f"تم حذف المصدر '{obj.name}' بنجاح.")
        return super().delete(request, *args, **kwargs)


class ImportLogListView(StaffRequiredMixin, ListView):
    model = AIImportLog
    template_name = 'ai_dashboard/logs_list.html'
    context_object_name = 'logs'
    paginate_by = 25

    def get_queryset(self):
        qs = super().get_queryset()
        status = self.request.GET.get('status')
        if status in ('success', 'failed'):
            qs = qs.filter(status=status)
        return qs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['all_sites'] = WordPressSite.objects.filter(is_active=True).order_by('name')
        context['status_filter'] = self.request.GET.get('status', '')
        return context


class RepublishLogView(StaffRequiredMixin, View):
    """
    Re-attempts the WordPress push for a single failed AIImportLog entry
    using the article/tags/category already saved from the original
    generation - see republish_ai_log() for why this costs nothing extra.
    """
    def post(self, request, pk, *args, **kwargs):
        from .ai_utils import republish_ai_log
        log = get_object_or_404(AIImportLog, pk=pk)
        if log.status != 'failed':
            messages.info(request, "هذا السجل ليس فاشلاً بالفعل.")
        elif republish_ai_log(log):
            messages.success(request, f"تم نشر المقال بنجاح على '{log.wp_site.name if log.wp_site else ''}'.")
        else:
            messages.error(request, f"فشلت إعادة النشر مرة أخرى: {log.error_message}")

        referer = request.META.get('HTTP_REFERER')
        if referer and url_has_allowed_host_and_scheme(referer, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
            return redirect(referer)
        return redirect('news_ai:logs')

    def get(self, request, pk, *args, **kwargs):
        return redirect('news_ai:logs')


class BulkRedistributeLogsView(StaffRequiredMixin, View):
    """
    Bulk-republishes a staff-picked set of failed AIImportLog entries across
    an explicit staff-specified count per WordPress site (e.g. "Site A gets
    5, Site B gets 3") in one action, instead of clicking "republish" on
    each row individually - see redistribute_and_republish_logs() for the
    distribution mechanics. This manual allocation deliberately ignores
    each site's daily_limit entirely, per request.

    Dispatched to Celery rather than run inline: each article is a full
    WordPress round-trip, so redistributing more than a few at once in the
    request/response cycle risks a Cloudflare gateway timeout well before
    Django itself would give up.
    """
    def post(self, request, *args, **kwargs):
        from .tasks import redistribute_and_republish_logs_task
        log_ids = request.POST.getlist('log_ids')

        site_counts = {}
        for site in WordPressSite.objects.filter(is_active=True):
            raw = request.POST.get(f'site_count_{site.id}', '').strip()
            if raw.isdigit() and int(raw) > 0:
                site_counts[site.id] = int(raw)

        if not log_ids:
            messages.error(request, "لم تحدد أي مقالات فاشلة لإعادة نشرها.")
        elif not site_counts:
            messages.error(request, "لم تحدد عدد الأخبار لأي موقع ووردبريس.")
        else:
            redistribute_and_republish_logs_task.delay(log_ids, site_counts)
            total_allocated = sum(site_counts.values())
            messages.success(
                request,
                f"تم إرسال طلب توزيع {min(total_allocated, len(log_ids))} من أصل {len(log_ids)} مقالاً محدداً "
                f"إلى الخلفية، وسيتم تنفيذه خلال دقائق قليلة. تحقّق من سجل العمليات بعد قليل لمتابعة النتيجة."
            )

        return redirect('news_ai:logs')

    def get(self, request, *args, **kwargs):
        return redirect('news_ai:logs')


class TriggerScraperView(StaffRequiredMixin, View):
    def post(self, request, *args, **kwargs):
        try:
            scrape_and_generate_news_task.delay()
            messages.success(request, "تم إرسال طلب التوليد إلى الخلفية، وسيتم تنفيذه خلال دقائق قليلة. تحقّق من سجلات الاستيراد بعد قليل لمتابعة النتيجة.")
        except Exception as e:
            messages.error(request, f"فشل إرسال طلب التوليد الآلي: {str(e)}")

        return redirect('news_ai:index')
    
    def get(self, request, *args, **kwargs):
        return redirect('news_ai:index')


class TriggerSiteScraperView(StaffRequiredMixin, View):
    """
    Manual "generate now" trigger scoped to a single WordPress site, so staff
    don't have to wait for that site's next scheduled slot (or fire the
    global trigger, which touches every active site). See
    run_ai_generation_cycle(target_site_id=...) for how the scheduling
    gates are bypassed for just this one site without disturbing anything else.
    """
    def post(self, request, wp_site_id, *args, **kwargs):
        wp_site = get_object_or_404(WordPressSite, pk=wp_site_id)
        try:
            scrape_and_generate_news_task.delay(target_site_id=wp_site.id)
            # info, not success: this only confirms the request reached the queue,
            # not that any article actually got published - the run can complete
            # "successfully" with zero articles (inactive sources, dead/blocked RSS
            # feeds, missing Gemini key...). Check "المقالات المنشورة" for this site
            # a few minutes later for the real outcome; nothing new there means this
            # run produced nothing, most likely because every source it tried failed.
            messages.info(request, f"تم إرسال طلب التوليد الفوري لموقع '{wp_site.name}' لقائمة الانتظار، وسيُنفَّذ خلال دقائق قليلة. هذا لا يعني نجاح النشر فعلياً — تحقّق من صفحة \"المقالات المنشورة\" لهذا الموقع بعد قليل: لو محصلش مقال جديد يبقى الأرجح إن كل مصادر الأخبار المرتبطة بالموقع فشلت في هذه المحاولة (روابط معطوبة أو محظورة)، مش عطل في الطلب نفسه.")
        except Exception as e:
            messages.error(request, f"فشل إرسال طلب التوليد الآلي: {str(e)}")

        fallback_url = reverse('news_ai:wp_site_edit', kwargs={'pk': wp_site_id})
        referer = request.META.get('HTTP_REFERER')
        if referer and url_has_allowed_host_and_scheme(referer, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
            return redirect(referer)
        return redirect(fallback_url)

    def get(self, request, wp_site_id, *args, **kwargs):
        return redirect('news_ai:wp_site_edit', pk=wp_site_id)


class WordPressSiteListView(StaffRequiredMixin, ListView):
    model = WordPressSite
    template_name = 'ai_dashboard/wp_sites_list.html'
    context_object_name = 'sites'

    def get_queryset(self):
        return WordPressSite.objects.select_related('merge_group').annotate(
            real_cost=Sum('import_logs__estimated_cost', filter=Q(import_logs__status='success')),
        )


WP_SITE_FORM_FIELDS = ['name', 'url', 'username', 'application_password', 'wp_author_ids', 'daily_limit', 'articles_per_run', 'is_active', 'sources', 'source_groups', 'category_mapping', 'use_rich_formatting', 'heading_color', 'use_internal_links', 'generate_gold_price_articles', 'generate_silver_price_articles', 'generate_dollar_price_articles', 'generate_iron_price_articles', 'generate_cement_price_articles', 'generate_poultry_price_articles', 'generate_fish_price_articles', 'generate_vegetable_price_articles', 'generate_arab_currencies_articles', 'site_tags', 'use_explainer_style', 'social_image_enabled', 'facebook_addon_plan', 'social_template', 'social_badge_text', 'social_logo', 'social_logo_position', 'social_primary_color', 'social_secondary_color', 'facebook_page_id', 'facebook_access_token', 'facebook_addon_trial_ends_at']


def _source_group_parents_with_cat_values(wp_site=None):
    """
    Every top-level AISourceGroup with its children (sub-categories)
    prefetched, each child annotated with a `.wp_cat_value` attribute
    holding this specific wp_site's existing comma-separated WP category id
    mapping (or '' if unmapped / wp_site is None for the add form).
    """
    cat_map = {}
    if wp_site is not None:
        cat_map = {m.source_group_id: m.wp_category_ids for m in wp_site.source_group_category_maps.all()}
    parents = list(AISourceGroup.objects.filter(parent__isnull=True).prefetch_related('children').order_by('name'))
    for parent in parents:
        for child in parent.children.all():
            child.wp_cat_value = cat_map.get(child.id, '')
    return parents


def _save_source_group_category_maps(wp_site, request):
    for group in AISourceGroup.objects.filter(parent__isnull=False):
        raw = request.POST.get(f'source_group_cat_{group.id}', '').strip()
        if raw:
            SourceGroupWPCategoryMap.objects.update_or_create(
                wp_site=wp_site, source_group=group, defaults={'wp_category_ids': raw},
            )
        else:
            SourceGroupWPCategoryMap.objects.filter(wp_site=wp_site, source_group=group).delete()


class WordPressSiteCreateView(StaffRequiredMixin, CreateView):
    model = WordPressSite
    fields = WP_SITE_FORM_FIELDS
    template_name = 'ai_dashboard/wp_site_form.html'
    success_url = reverse_lazy('news_ai:wp_sites')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['source_group_parents'] = _source_group_parents_with_cat_values()
        return context

    def form_valid(self, form):
        response = super().form_valid(form)
        _save_source_group_category_maps(self.object, self.request)
        messages.success(self.request, f"تمت إضافة الموقع '{form.instance.name}' بنجاح.")
        return response


class WordPressSiteUpdateView(StaffRequiredMixin, UpdateView):
    model = WordPressSite
    fields = WP_SITE_FORM_FIELDS
    template_name = 'ai_dashboard/wp_site_form.html'
    success_url = reverse_lazy('news_ai:wp_sites')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['has_schedule_slots'] = self.object.schedule_slots.filter(is_active=True).exists()
        context['source_group_parents'] = _source_group_parents_with_cat_values(self.object)
        try:
            from .views_facebook_connect import make_facebook_connect_token
            if getattr(settings, 'FACEBOOK_APP_ID', None):
                token = make_facebook_connect_token(self.object.pk)
                context['facebook_connect_link'] = self.request.build_absolute_uri(
                    reverse('news_ai:facebook_connect_start', kwargs={'token': token})
                )
        except ImportError:
            pass
        return context

    def form_valid(self, form):
        response = super().form_valid(form)
        _save_source_group_category_maps(self.object, self.request)
        messages.success(self.request, f"تم تحديث الموقع '{form.instance.name}' بنجاح.")
        return response


class WordPressSiteDeleteView(StaffRequiredMixin, DeleteView):
    model = WordPressSite
    template_name = 'ai_dashboard/wp_site_confirm_delete.html'
    success_url = reverse_lazy('news_ai:wp_sites')

    def delete(self, request, *args, **kwargs):
        obj = self.get_object()
        messages.success(self.request, f"تم حذف الموقع '{obj.name}' بنجاح.")
        return super().delete(request, *args, **kwargs)


class WordPressSitePublishedArticlesView(StaffRequiredMixin, ListView):
    model = AIImportLog
    template_name = 'ai_dashboard/wp_site_articles_list.html'
    context_object_name = 'logs'
    paginate_by = 30

    def get_queryset(self):
        self.wp_site = get_object_or_404(WordPressSite, pk=self.kwargs['wp_site_id'])
        return AIImportLog.objects.filter(
            wp_site=self.wp_site, status='success'
        ).exclude(published_url='').select_related('article').order_by('-created_at')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['wp_site'] = self.wp_site

        logs = context['logs']
        article_ids = [log.article_id for log in logs if log.article_id]
        latest_social_posts = {}
        if article_ids:
            posts = SocialSharePost.objects.filter(
                wp_site=self.wp_site, article_id__in=article_ids
            ).order_by('article_id', '-created_at')
            for post in posts:
                latest_social_posts.setdefault(post.article_id, post)
        # Attached directly on each row object (rather than exposed as a
        # separate dict) since Django templates can't do dynamic dict
        # lookups by variable key without a custom filter.
        for log in logs:
            log.social_post = latest_social_posts.get(log.article_id)
        return context


@require_POST
def wp_site_social_preview_view(request):
    """
    Live preview for the social-card settings on the wp-site add/edit form
    (staff, ai-dashboard) and the customer self-serve design form
    (accounts:social_design_edit): renders a sample card with whatever
    template/colors/badge/logo/position is currently selected in the
    (unsaved) form, using a stock sample photo and headline - so the caller
    can see the effect before hitting save.
    """
    if not request.user.is_authenticated:
        return HttpResponseForbidden()

    site_id = request.POST.get('site_id')
    if not request.user.is_staff:
        if not site_id:
            return HttpResponseForbidden()
        profile = getattr(request.user, 'customer_profile', None)
        owns_site = WPConnectionToken.objects.filter(
            customer=profile, is_used=True, wp_site_id=site_id
        ).exists()
        if not owns_site:
            return HttpResponseForbidden()

    import io as _io

    class _PreviewSite:
        pass

    site = _PreviewSite()
    site.name = request.POST.get('name') or 'اسم الموقع'
    site.social_template = request.POST.get('social_template') or 'news_ribbon'
    site.social_badge_text = request.POST.get('social_badge_text', '')
    site.social_primary_color = request.POST.get('social_primary_color') or '#0d9488'
    site.social_secondary_color = request.POST.get('social_secondary_color') or '#0f172a'
    site.social_logo_position = request.POST.get('social_logo_position') or 'top_left'

    logo_file = request.FILES.get('social_logo')
    if logo_file:
        site.social_logo = logo_file
    elif site_id:
        existing = get_object_or_404(WordPressSite, pk=site_id)
        site.social_logo = existing.social_logo
    else:
        site.social_logo = None

    from .social_image_utils import generate_social_card_image

    sample_title = 'هذا نص تجريبي لعنوان الخبر يوضح شكل التصميم النهائي على فيسبوك'
    sample_photo_path = settings.BASE_DIR / 'static' / 'images' / 'price_covers' / 'general_news.jpg'
    with open(sample_photo_path, 'rb') as f:
        photo_bytes = f.read()

    try:
        card = generate_social_card_image(site, sample_title, photo_bytes)
    except Exception as e:
        return JsonResponse({'status': 'error', 'message': str(e)}, status=400)

    buffer = _io.BytesIO()
    card.save(buffer, format='JPEG', quality=85)
    return HttpResponse(buffer.getvalue(), content_type='image/jpeg')


class RegenerateSocialImageView(StaffRequiredMixin, View):
    """
    Manual "regenerate social image" action - triggered by a button on the
    site's published-articles list, regardless of the site's automatic
    social_image_enabled toggle (an explicit click always regenerates).
    """
    def post(self, request, log_id):
        from .social_image_utils import generate_and_publish_social_share

        log = get_object_or_404(AIImportLog, pk=log_id)
        if not log.article or not log.wp_site:
            messages.error(request, "لا يمكن إعادة توليد الصورة: الخبر أو الموقع غير متاح.")
        else:
            social_post = generate_and_publish_social_share(log.article, log.wp_site, force=True)
            if social_post and social_post.status != 'failed':
                messages.success(request, "تم توليد صورة السوشال ميديا بنجاح.")
            else:
                error = social_post.error_message if social_post else "تعذر توليد الصورة."
                messages.error(request, f"فشل توليد صورة السوشال ميديا: {error}")

        return redirect('news_ai:wp_site_articles', wp_site_id=log.wp_site_id)

    def get(self, request, log_id):
        log = get_object_or_404(AIImportLog, pk=log_id)
        return redirect('news_ai:wp_site_articles', wp_site_id=log.wp_site_id)


def _parse_slot_form(request, wp_site):
    """
    Shared parsing/validation for the schedule-slot create/update forms: reads
    time_of_day, content_types (checkbox group) and regular_news_count from
    POST, validated against WordPressScheduleSlot.CONTENT_TYPE_CHOICES plus
    one 'group_<source_group_id>' token per AISourceGroup this site is
    actually subscribed to (wp_site.source_groups) - these represent regular
    (RSS) news scoped to a specific مجموعة مصادر مفضلة, the same tokens the
    WordPress plugin's own schedule screen writes (see
    get_due_slot_for_regular_news in ai_utils.py). Without accepting them
    here, staff could never view or re-save a slot the customer configured
    via the plugin without silently stripping those selections back out.
    Returns (cleaned_dict, error_message_or_None).
    """
    time_str = request.POST.get('time_of_day', '').strip()
    try:
        time_of_day = datetime.strptime(time_str, '%H:%M').time()
    except ValueError:
        return None, "صيغة الوقت غير صحيحة."

    valid_keys = {c[0] for c in WordPressScheduleSlot.CONTENT_TYPE_CHOICES}
    valid_keys |= {f'group_{gid}' for gid in wp_site.source_groups.values_list('id', flat=True)}
    content_types = [c for c in request.POST.getlist('content_types') if c in valid_keys]
    if not content_types:
        return None, "يجب اختيار نوع محتوى واحد على الأقل لهذه الفترة."

    try:
        regular_news_count = max(1, int(request.POST.get('regular_news_count', '1')))
    except ValueError:
        regular_news_count = 1

    return {
        'time_of_day': time_of_day,
        'content_types': ','.join(content_types),
        'regular_news_count': regular_news_count,
        'is_active': 'is_active' in request.POST,
    }, None


class ScheduleSlotListView(StaffRequiredMixin, ListView):
    model = WordPressScheduleSlot
    template_name = 'ai_dashboard/schedule_slots_list.html'
    context_object_name = 'slots'

    def get_queryset(self):
        self.wp_site = get_object_or_404(WordPressSite, pk=self.kwargs['wp_site_id'])
        return WordPressScheduleSlot.objects.filter(wp_site=self.wp_site)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['wp_site'] = self.wp_site
        context['content_type_choices'] = WordPressScheduleSlot.CONTENT_TYPE_CHOICES
        # Regular (RSS) news, scoped per slot to one or more of the source
        # groups this site is actually subscribed to (see
        # wp_site_edit's "مجموعات المصادر المفضلة" section) - rendered as
        # 'group_<id>' checkboxes alongside the fixed price types, matching
        # the tokens the WordPress plugin's own schedule screen writes.
        source_groups = list(self.wp_site.source_groups.all())
        for group in source_groups:
            group.token = f'group_{group.id}'
        context['source_groups'] = source_groups
        return context


class ScheduleSlotCreateView(StaffRequiredMixin, View):
    def post(self, request, wp_site_id):
        wp_site = get_object_or_404(WordPressSite, pk=wp_site_id)
        cleaned, error = _parse_slot_form(request, wp_site)
        if error:
            messages.error(request, error)
        else:
            WordPressScheduleSlot.objects.create(wp_site=wp_site, **cleaned)
            messages.success(request, "تمت إضافة الفترة الزمنية بنجاح.")
        return redirect('news_ai:schedule_slots', wp_site_id=wp_site_id)

    def get(self, request, wp_site_id):
        return redirect('news_ai:schedule_slots', wp_site_id=wp_site_id)


class ScheduleSlotUpdateView(StaffRequiredMixin, View):
    def post(self, request, wp_site_id, pk):
        slot = get_object_or_404(WordPressScheduleSlot, pk=pk, wp_site_id=wp_site_id)
        wp_site = get_object_or_404(WordPressSite, pk=wp_site_id)
        cleaned, error = _parse_slot_form(request, wp_site)
        if error:
            messages.error(request, error)
        else:
            for field, value in cleaned.items():
                setattr(slot, field, value)
            # Changing the schedule means the old run log no longer reflects
            # this (possibly new) configuration - let every type fire again.
            slot.last_run_log = '{}'
            slot.save()
            messages.success(request, "تم تحديث الفترة الزمنية بنجاح.")
        return redirect('news_ai:schedule_slots', wp_site_id=wp_site_id)

    def get(self, request, wp_site_id, pk):
        return redirect('news_ai:schedule_slots', wp_site_id=wp_site_id)


class ScheduleSlotDeleteView(StaffRequiredMixin, View):
    def post(self, request, wp_site_id, pk):
        slot = get_object_or_404(WordPressScheduleSlot, pk=pk, wp_site_id=wp_site_id)
        slot.delete()
        messages.success(request, "تم حذف الفترة الزمنية.")
        return redirect('news_ai:schedule_slots', wp_site_id=wp_site_id)

    def get(self, request, wp_site_id, pk):
        return redirect('news_ai:schedule_slots', wp_site_id=wp_site_id)


class WordPressSiteGroupListView(StaffRequiredMixin, ListView):
    model = WordPressSiteGroup
    template_name = 'ai_dashboard/wp_site_groups_list.html'
    context_object_name = 'groups'

    def get_queryset(self):
        return WordPressSiteGroup.objects.prefetch_related('sites').all()


class WordPressSiteGroupCreateView(StaffRequiredMixin, CreateView):
    model = WordPressSiteGroup
    fields = ['name', 'is_active']
    template_name = 'ai_dashboard/wp_site_group_form.html'
    success_url = reverse_lazy('news_ai:wp_site_groups')

    def form_valid(self, form):
        messages.success(self.request, f"تمت إضافة مجموعة الدمج '{form.instance.name}' بنجاح.")
        return super().form_valid(form)


class WordPressSiteGroupUpdateView(StaffRequiredMixin, UpdateView):
    model = WordPressSiteGroup
    fields = ['name', 'is_active']
    template_name = 'ai_dashboard/wp_site_group_form.html'
    success_url = reverse_lazy('news_ai:wp_site_groups')

    def form_valid(self, form):
        messages.success(self.request, f"تم تحديث مجموعة الدمج '{form.instance.name}' بنجاح.")
        return super().form_valid(form)


class WordPressSiteGroupDeleteView(StaffRequiredMixin, DeleteView):
    model = WordPressSiteGroup
    template_name = 'ai_dashboard/wp_site_group_confirm_delete.html'
    success_url = reverse_lazy('news_ai:wp_site_groups')

    def delete(self, request, *args, **kwargs):
        obj = self.get_object()
        messages.success(self.request, f"تم حذف مجموعة الدمج '{obj.name}' بنجاح.")
        return super().delete(request, *args, **kwargs)


from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status, permissions
from .auth import APITokenAuthentication

class AISettingsAPIView(APIView):
    authentication_classes = [APITokenAuthentication]
    permission_classes = [permissions.IsAdminUser]

    def get(self, request, *args, **kwargs):
        settings_obj = AISettings.get_settings()
        categories_data = [{"id": cat.id, "name": cat.name} for cat in Category.objects.filter(is_active=True)]
        authors_data = [{"id": u.id, "username": u.username, "name": u.get_full_name()} for u in User.objects.filter(is_active=True, is_staff=True)]
        first_author = settings_obj.default_authors.first()

        return Response({
            "is_active": settings_obj.is_active,
            "articles_per_day": settings_obj.articles_per_day,
            "default_author_id": first_author.id if first_author else None,
            "categories": [cat.id for cat in settings_obj.categories.all()],
            "all_categories": categories_data,
            "all_authors": authors_data
        })

    def post(self, request, *args, **kwargs):
        settings_obj = AISettings.get_settings()
        
        is_active = request.data.get('is_active')
        articles_per_day = request.data.get('articles_per_day')
        default_author_id = request.data.get('default_author_id')
        categories_ids = request.data.get('categories', [])
        
        if is_active is not None:
            settings_obj.is_active = bool(is_active)
            
        if articles_per_day is not None:
            try:
                settings_obj.articles_per_day = int(articles_per_day)
            except ValueError:
                return Response({"error": "articles_per_day must be an integer"}, status=status.HTTP_400_BAD_REQUEST)
                
        if default_author_id is not None:
            if default_author_id == "":
                settings_obj.default_authors.clear()
            else:
                try:
                    settings_obj.default_authors.set([User.objects.get(id=default_author_id)])
                except User.DoesNotExist:
                    return Response({"error": "User does not exist"}, status=status.HTTP_400_BAD_REQUEST)

        settings_obj.save()

        if categories_ids is not None:
            settings_obj.categories.set(categories_ids)

        first_author = settings_obj.default_authors.first()
        return Response({
            "message": "Settings updated successfully",
            "is_active": settings_obj.is_active,
            "articles_per_day": settings_obj.articles_per_day,
            "default_author_id": first_author.id if first_author else None,
            "categories": [cat.id for cat in settings_obj.categories.all()]
        })

from .models import AISourceGroup

class SourceGroupListView(StaffRequiredMixin, ListView):
    model = AISourceGroup
    template_name = 'ai_dashboard/source_groups_list.html'
    context_object_name = 'groups'

    def get_queryset(self):
        return AISourceGroup.objects.select_related('parent').prefetch_related('children')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['parent_groups'] = [g for g in context['groups'] if g.parent_id is None]
        return context

class SourceGroupCreateView(StaffRequiredMixin, CreateView):
    model = AISourceGroup
    fields = ['name', 'description', 'parent', 'is_price_articles_group']
    template_name = 'ai_dashboard/source_group_form.html'
    success_url = reverse_lazy('news_ai:source_groups')

    def get_initial(self):
        initial = super().get_initial()
        parent_id = self.request.GET.get('parent')
        if parent_id and parent_id.isdigit():
            initial['parent'] = parent_id
        return initial

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        form.fields['parent'].queryset = AISourceGroup.objects.filter(parent__isnull=True)
        return form

    def form_valid(self, form):
        messages.success(self.request, f"تم إنشاء مجموعة المصادر '{form.instance.name}' بنجاح.")
        return super().form_valid(form)

class SourceGroupUpdateView(StaffRequiredMixin, UpdateView):
    model = AISourceGroup
    fields = ['name', 'description', 'parent', 'is_price_articles_group']
    template_name = 'ai_dashboard/source_group_form.html'
    success_url = reverse_lazy('news_ai:source_groups')

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        form.fields['parent'].queryset = AISourceGroup.objects.filter(parent__isnull=True).exclude(pk=self.object.pk)
        return form

    def form_valid(self, form):
        messages.success(self.request, f"تم تعديل مجموعة المصادر '{form.instance.name}' بنجاح.")
        return super().form_valid(form)

class SourceGroupDeleteView(StaffRequiredMixin, DeleteView):
    model = AISourceGroup
    template_name = 'ai_dashboard/source_group_confirm_delete.html'
    success_url = reverse_lazy('news_ai:source_groups')

    def delete(self, request, *args, **kwargs):
        obj = self.get_object()
        messages.success(self.request, f"تم حذف مجموعة المصادر '{obj.name}' بنجاح.")
        return super().delete(request, *args, **kwargs)


class WPConnectionTokenListView(StaffRequiredMixin, ListView):
    model = WPConnectionToken
    template_name = 'ai_dashboard/wp_tokens_list.html'
    context_object_name = 'tokens'

    def get_queryset(self):
        queryset = super().get_queryset()
        from django.utils import timezone
        from django.db.models import Q
        
        # Search
        q = self.request.GET.get('q', '').strip()
        if q:
            queryset = queryset.filter(Q(client_name__icontains=q) | Q(token__icontains=q))
            
        # Status filter
        status = self.request.GET.get('status', '').strip()
        if status == 'used':
            queryset = queryset.filter(is_used=True)
        elif status == 'unused':
            queryset = queryset.filter(is_used=False)
            
        # Expiry filter
        expiry = self.request.GET.get('expiry', '').strip()
        now = timezone.now()
        if expiry == 'expired':
            queryset = queryset.filter(expires_at__lt=now)
        elif expiry == 'active':
            queryset = queryset.filter(Q(expires_at__gte=now) | Q(expires_at__isnull=True))
            
        return queryset.select_related('wp_site').order_by('-created_at')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        from django.utils import timezone
        context['q'] = self.request.GET.get('q', '')
        context['status_filter'] = self.request.GET.get('status', '')
        context['expiry_filter'] = self.request.GET.get('expiry', '')
        
        now = timezone.now()
        for token in context['tokens']:
            token.is_expired = token.expires_at is not None and token.expires_at < now
            
        return context


class WPConnectionTokenCreateView(StaffRequiredMixin, CreateView):
    model = WPConnectionToken
    fields = ['client_name', 'package_daily_limit', 'expires_at']
    template_name = 'ai_dashboard/wp_token_form.html'
    success_url = reverse_lazy('news_ai:wp_tokens')

    def form_valid(self, form):
        messages.success(self.request, f"تم إنشاء كود ربط جديد للعميل '{form.instance.client_name}'.")
        return super().form_valid(form)


class WPConnectionTokenUpdateView(StaffRequiredMixin, UpdateView):
    model = WPConnectionToken
    fields = ['client_name', 'package_daily_limit', 'is_used', 'wp_site', 'expires_at']
    template_name = 'ai_dashboard/wp_token_form.html'
    success_url = reverse_lazy('news_ai:wp_tokens')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['is_edit'] = True
        return context

    def form_valid(self, form):
        messages.success(self.request, f"تم تحديث كود الربط للعميل '{form.instance.client_name}' بنجاح.")
        return super().form_valid(form)


class WPConnectionTokenDeleteView(StaffRequiredMixin, DeleteView):
    model = WPConnectionToken
    template_name = 'ai_dashboard/wp_token_confirm_delete.html'
    success_url = reverse_lazy('news_ai:wp_tokens')

    def delete(self, request, *args, **kwargs):
        obj = self.get_object()
        messages.success(self.request, f"تم حذف كود الربط للعميل '{obj.client_name}' بنجاح.")
        return super().delete(request, *args, **kwargs)


@csrf_exempt
@require_POST
def wp_connect_api_view(request):
    try:
        data = json.loads(request.body)
        token_str = data.get('token')
        site_url = data.get('site_url')
        username = data.get('username')
        application_password = data.get('application_password')

        if not all([token_str, site_url, username, application_password]):
            return JsonResponse({'status': 'error', 'message': 'Missing required fields.'}, status=400)

        # Validate token
        try:
            import uuid
            uuid_obj = uuid.UUID(token_str)
        except ValueError:
            return JsonResponse({'status': 'error', 'message': 'Invalid token format.'}, status=400)

        token_obj = WPConnectionToken.objects.filter(token=token_str).first()
        if not token_obj:
            return JsonResponse({'status': 'error', 'message': 'Invalid token.'}, status=403)
            
        from django.utils import timezone
        if token_obj.expires_at and timezone.now() > token_obj.expires_at:
            return JsonResponse({'status': 'error', 'message': 'Token has expired.'}, status=403)
        
        settings_data = data.get('settings', {})

        if token_obj.is_used:
            # Check if this token belongs to the same site URL (for updates)
            if token_obj.wp_site and token_obj.wp_site.url.rstrip('/') == site_url.rstrip('/'):
                wp_site = token_obj.wp_site
                wp_site.username = username
                wp_site.application_password = application_password
                # Update settings
                wp_site.use_rich_formatting = bool(settings_data.get('use_rich_formatting', wp_site.use_rich_formatting))
                wp_site.heading_color = settings_data.get('heading_color', wp_site.heading_color)
                wp_site.use_internal_links = bool(settings_data.get('use_internal_links', wp_site.use_internal_links))
                wp_site.use_explainer_style = bool(settings_data.get('use_explainer_style', wp_site.use_explainer_style))
                wp_site.site_tags = settings_data.get('site_tags', wp_site.site_tags)
                
                # Prices
                from .models import WordPressScheduleSlot
                enabled_price_articles = data.get('enabled_price_articles', [])
                for key, _ in WordPressScheduleSlot.CONTENT_TYPE_CHOICES:
                    pf = f"generate_{key}_price_articles"
                    if hasattr(wp_site, pf):
                        setattr(wp_site, pf, key in enabled_price_articles)
                
                if 'category_mapping' in settings_data:
                    wp_site.category_mapping = settings_data['category_mapping']
                
                if 'wp_author_ids' in settings_data:
                    wp_site.wp_author_ids = settings_data['wp_author_ids']

                if token_obj.included_facebook_addon_plan:
                    wp_site.facebook_addon_plan = token_obj.included_facebook_addon_plan
                    wp_site.social_image_enabled = True
                    wp_site.facebook_addon_trial_ends_at = token_obj.expires_at

                wp_site.save()
                
                # Source Groups
                if 'source_groups' in settings_data:
                    wp_site.source_groups.set(settings_data['source_groups'])
                    
                # Schedules
                if 'schedules' in settings_data:
                    from .models import WordPressScheduleSlot
                    wp_site.schedule_slots.all().delete()
                    for sched in settings_data['schedules']:
                        WordPressScheduleSlot.objects.create(
                            wp_site=wp_site,
                            time_of_day=sched.get('time_of_day', '00:00'),
                            content_types=','.join(sched.get('content_types', [])),
                            regular_news_count=int(sched.get('regular_news_count', 1)),
                            is_active=True
                        )

                return JsonResponse({'status': 'success', 'message': 'Site settings updated successfully.'})
            else:
                return JsonResponse({'status': 'error', 'message': 'Token has already been used by a different site.'}, status=403)

        # Create new WordPressSite
        from urllib.parse import urlparse
        domain = urlparse(site_url).netloc or site_url
        site_name = f"{token_obj.client_name} ({domain})"

        wp_site = WordPressSite(
            name=site_name,
            url=site_url,
            username=username,
            application_password=application_password,
            daily_limit=token_obj.package_daily_limit,
            is_active=True,
            expires_at=token_obj.expires_at
        )
        if token_obj.included_facebook_addon_plan:
            wp_site.facebook_addon_plan = token_obj.included_facebook_addon_plan
            wp_site.social_image_enabled = True
            wp_site.facebook_addon_trial_ends_at = token_obj.expires_at

        # Apply settings
        wp_site.use_rich_formatting = bool(settings_data.get('use_rich_formatting', False))
        if settings_data.get('heading_color'):
            wp_site.heading_color = settings_data['heading_color']
        wp_site.use_internal_links = bool(settings_data.get('use_internal_links', False))
        wp_site.use_explainer_style = bool(settings_data.get('use_explainer_style', False))
        if settings_data.get('site_tags'):
            wp_site.site_tags = settings_data['site_tags']
        
        from .models import WordPressScheduleSlot
        enabled_price_articles = data.get('enabled_price_articles', [])
        for key, _ in WordPressScheduleSlot.CONTENT_TYPE_CHOICES:
            pf = f"generate_{key}_price_articles"
            if hasattr(wp_site, pf):
                setattr(wp_site, pf, key in enabled_price_articles)
        
        if settings_data.get('category_mapping'):
            wp_site.category_mapping = settings_data['category_mapping']

        if 'wp_author_ids' in settings_data:
            wp_site.wp_author_ids = settings_data['wp_author_ids']

        wp_site.save()

        # Source Groups
        if 'source_groups' in settings_data:
            wp_site.source_groups.set(settings_data['source_groups'])
            
        # Schedules
        if 'schedules' in settings_data:
            from .models import WordPressScheduleSlot
            for sched in settings_data['schedules']:
                WordPressScheduleSlot.objects.create(
                    wp_site=wp_site,
                    time_of_day=sched.get('time_of_day', '00:00'),
                    content_types=','.join(sched.get('content_types', [])),
                    regular_news_count=int(sched.get('regular_news_count', 1)),
                    is_active=True
                )

        # Mark token as used
        token_obj.is_used = True
        token_obj.wp_site = wp_site
        token_obj.save()

        return JsonResponse({'status': 'success', 'message': 'Site connected successfully.'})

    except Exception as e:
        import logging
        logger = logging.getLogger(__name__)
        logger.error(f"WP Connect API Error: {str(e)}")
        return JsonResponse({'status': 'error', 'message': 'Internal server error.'}, status=500)


@csrf_exempt
def wp_plugin_data_api_view(request):
    try:
        from .models import AISourceGroup, WordPressScheduleSlot, WPConnectionToken
        groups = AISourceGroup.objects.all().values('id', 'name', 'parent_id', 'is_price_articles_group')
        
        content_types = [
            {'id': key, 'name': label} 
            for key, label in WordPressScheduleSlot.CONTENT_TYPE_CHOICES
        ]
        
        daily_limit = 3
        token = request.GET.get('token') or request.POST.get('token')
        if token:
            try:
                token_obj = WPConnectionToken.objects.get(token=token)
                if token_obj.wp_site:
                    daily_limit = token_obj.wp_site.daily_limit
            except WPConnectionToken.DoesNotExist:
                pass
        
        return JsonResponse({
            'status': 'success',
            'data': {
                'source_groups': list(groups),
                'content_types': content_types,
                'daily_limit': daily_limit
            }
        })
    except Exception as e:
        return JsonResponse({'status': 'error', 'message': str(e)}, status=500)


@csrf_exempt
@require_POST
def wp_post_published_api_view(request):
    """
    Called by the WP plugin's transition_post_status hook on every post
    published on a customer site - by a human editor in wp-admin, or by
    Sahafi Hub's own REST push (push_article_to_wordpress). Drives the
    Facebook auto-posting add-on, which is gated per-site and billed
    separately from the core syndication service (WordPressSite.social_image_enabled
    / facebook_addon_is_active - see models.py).
    """
    import logging
    logger = logging.getLogger(__name__)
    try:
        data = json.loads(request.body)
        token_str = data.get('token')
        site_url = data.get('site_url', '')

        token_obj = WPConnectionToken.objects.filter(token=token_str, is_used=True).select_related('wp_site').first()
        if not token_obj or not token_obj.wp_site:
            return JsonResponse({'status': 'error', 'message': 'Invalid token.'}, status=403)

        wp_site = token_obj.wp_site
        if wp_site.url.rstrip('/') != site_url.rstrip('/'):
            return JsonResponse({'status': 'error', 'message': 'Site URL mismatch.'}, status=403)

        if not wp_site.is_active or not wp_site.facebook_addon_is_active:
            return JsonResponse({'status': 'ok', 'skipped': True})

        try:
            from .social_image_utils import generate_and_publish_social_share_from_wp_payload
            generate_and_publish_social_share_from_wp_payload(wp_site, data)
        except Exception as e:
            # Best-effort add-on - must never surface as an error to WordPress.
            logger.error(f"Error in Facebook social share pipeline for {wp_site.name}: {e}")

        return JsonResponse({'status': 'ok'})
    except Exception as e:
        logger.error(f"WP Post Published API Error: {str(e)}")
        return JsonResponse({'status': 'error', 'message': 'Internal server error.'}, status=500)


def _resolve_used_wp_connection_token(token_str):
    """
    Looks up a used WPConnectionToken by its (UUID) token string, returning
    None for anything that isn't a valid UUID instead of letting Django raise
    a ValidationError - callers just get a clean "invalid token" either way.
    """
    if not token_str:
        return None
    try:
        return WPConnectionToken.objects.filter(token=token_str, is_used=True).select_related('wp_site').first()
    except (ValueError, ValidationError):
        return None


@csrf_exempt
def pending_image_articles_api_view(request):
    """
    Called by the WP plugin's "Articles Needing a Photo" admin section (see
    hold_article_pending_image in ai_utils.py) - lists this site's articles
    that were fully written but have no usable photo anywhere (source feed,
    linked article page, or Wikimedia Commons), so the customer can pick one
    themselves instead of every such article publishing with the same
    generic stock photo.
    """
    token_str = request.GET.get('token') or request.POST.get('token')
    token_obj = _resolve_used_wp_connection_token(token_str)
    if not token_obj or not token_obj.wp_site:
        return JsonResponse({'status': 'error', 'message': 'Invalid token.'}, status=403)

    logs = (
        AIImportLog.objects.filter(wp_site=token_obj.wp_site, status='pending_image')
        .select_related('article').order_by('-created_at')
    )
    items = [
        {
            'id': log.id,
            'title': (log.article.title if log.article else log.title) or '',
            'excerpt': (log.article.excerpt if log.article else '') or '',
            'created_at': log.created_at.isoformat(),
        }
        for log in logs
    ]
    return JsonResponse({'status': 'success', 'items': items})


@csrf_exempt
def published_articles_log_api_view(request):
    """
    Called by the WP plugin's "Publishing Log" admin section - lists this
    site's successfully-published articles (most recent first), so the
    customer can see what's actually gone live on their site without
    needing to log in to the Enshrly dashboard.
    """
    token_str = request.GET.get('token') or request.POST.get('token')
    token_obj = _resolve_used_wp_connection_token(token_str)
    if not token_obj or not token_obj.wp_site:
        return JsonResponse({'status': 'error', 'message': 'Invalid token.'}, status=403)

    logs = (
        AIImportLog.objects.filter(wp_site=token_obj.wp_site, status='success')
        .exclude(published_url='').exclude(published_url__isnull=True)
        .order_by('-created_at')[:50]
    )
    items = [
        {
            'id': log.id,
            'title': log.title or '',
            'published_url': log.published_url or '',
            'wp_category_name': log.wp_category_name or '',
            'created_at': log.created_at.isoformat(),
        }
        for log in logs
    ]
    return JsonResponse({'status': 'success', 'items': items})


@csrf_exempt
@require_POST
def submit_pending_image_api_view(request, log_id):
    """
    Called by the WP plugin once the customer has picked a photo (via the
    native WordPress media picker) for one of their pending-image articles.
    Downloads that photo and hands off to finish_pending_image_publish(),
    which pushes the article to WordPress using the same category/tags/SEO
    metadata already generated back when the article was written.
    """
    import logging
    logger = logging.getLogger(__name__)

    token_str = request.POST.get('token')
    image_url = (request.POST.get('image_url') or '').strip()

    token_obj = _resolve_used_wp_connection_token(token_str)
    if not token_obj or not token_obj.wp_site:
        return JsonResponse({'status': 'error', 'message': 'Invalid token.'}, status=403)

    if not image_url:
        return JsonResponse({'status': 'error', 'message': 'الرجاء اختيار صورة.'}, status=400)

    pending_log = AIImportLog.objects.filter(id=log_id, wp_site=token_obj.wp_site, status='pending_image').first()
    if not pending_log:
        return JsonResponse({'status': 'error', 'message': 'هذا الطلب غير موجود أو تم التعامل معه بالفعل.'}, status=404)

    try:
        import requests
        res = requests.get(image_url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=15)
        res.raise_for_status()
        image_bytes = res.content
        filename = image_url.split('/')[-1].split('?')[0] or 'cover.jpg'
    except Exception as e:
        logger.error(f"Failed to download customer-picked image {image_url}: {e}")
        return JsonResponse({'status': 'error', 'message': 'تعذّر تحميل الصورة من الرابط المُرسَل.'}, status=400)

    from .ai_utils import finish_pending_image_publish
    published_url = finish_pending_image_publish(pending_log, image_bytes, filename)
    if not published_url:
        return JsonResponse({'status': 'error', 'message': pending_log.error_message or 'فشل النشر على ووردبريس.'}, status=500)

    return JsonResponse({'status': 'success', 'published_url': published_url})

from datetime import datetime
from django.shortcuts import render, redirect
from django.views.generic import ListView, CreateView, UpdateView, DeleteView, View, TemplateView
from django.contrib.auth.mixins import UserPassesTestMixin
from django.urls import reverse_lazy, reverse
from django.contrib import messages
from django.shortcuts import get_object_or_404
from django.utils.http import url_has_allowed_host_and_scheme
from django.db.models import Sum, Q
from django.conf import settings
from .models import AISettings, AISource, AIImportLog, Category, Article, WordPressSite, WordPressScheduleSlot, WordPressSiteGroup, SocialSharePost
from .tasks import scrape_and_generate_news_task

class StaffRequiredMixin(UserPassesTestMixin):
    """
    Mixin that ensures the user is logged in and is a staff member.
    """
    login_url = '/accounts/login/' # Or wherever the login path is configured
    
    def test_func(self):
        return self.request.user.is_authenticated and self.request.user.is_staff


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
    fields = ['gemini_api_key', 'telegram_bot_token', 'telegram_allowed_chats', 'articles_per_day', 'max_words', 'is_active', 'publish_to_main_site', 'daily_cost_limit_usd']
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

    def form_valid(self, form):
        messages.success(self.request, f"تمت إضافة المصدر '{form.instance.name}' بنجاح.")
        return super().form_valid(form)


class SourceUpdateView(StaffRequiredMixin, UpdateView):
    model = AISource
    fields = ['name', 'url', 'language', 'group', 'is_active']
    template_name = 'ai_dashboard/source_form.html'
    success_url = reverse_lazy('news_ai:sources')

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
            messages.success(request, f"تم إرسال طلب التوليد الفوري لموقع '{wp_site.name}' إلى الخلفية، وسيتم تنفيذه خلال دقائق قليلة. تحقّق من سجلات الاستيراد بعد قليل لمتابعة النتيجة.")
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


WP_SITE_FORM_FIELDS = ['name', 'url', 'username', 'application_password', 'wp_author_ids', 'daily_limit', 'articles_per_run', 'is_active', 'sources', 'merge_group', 'category_mapping', 'use_rich_formatting', 'heading_color', 'use_internal_links', 'generate_gold_price_articles', 'generate_silver_price_articles', 'generate_dollar_price_articles', 'generate_iron_price_articles', 'generate_cement_price_articles', 'generate_poultry_price_articles', 'generate_fish_price_articles', 'generate_vegetable_price_articles', 'generate_arab_currencies_articles', 'site_tags', 'use_explainer_style', 'social_image_enabled', 'social_template', 'social_logo', 'social_primary_color', 'social_secondary_color', 'facebook_page_id', 'facebook_access_token']


class WordPressSiteCreateView(StaffRequiredMixin, CreateView):
    model = WordPressSite
    fields = WP_SITE_FORM_FIELDS
    template_name = 'ai_dashboard/wp_site_form.html'
    success_url = reverse_lazy('news_ai:wp_sites')

    def form_valid(self, form):
        messages.success(self.request, f"تمت إضافة الموقع '{form.instance.name}' بنجاح.")
        return super().form_valid(form)


class WordPressSiteUpdateView(StaffRequiredMixin, UpdateView):
    model = WordPressSite
    fields = WP_SITE_FORM_FIELDS
    template_name = 'ai_dashboard/wp_site_form.html'
    success_url = reverse_lazy('news_ai:wp_sites')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['has_schedule_slots'] = self.object.schedule_slots.filter(is_active=True).exists()
        from .views_facebook_connect import make_facebook_connect_token
        if settings.FACEBOOK_APP_ID:
            token = make_facebook_connect_token(self.object.pk)
            context['facebook_connect_link'] = self.request.build_absolute_uri(
                reverse('facebook_connect_start', kwargs={'token': token})
            )
        return context

    def form_valid(self, form):
        messages.success(self.request, f"تم تحديث الموقع '{form.instance.name}' بنجاح.")
        return super().form_valid(form)


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


def _parse_slot_form(request):
    """
    Shared parsing/validation for the schedule-slot create/update forms: reads
    time_of_day, content_types (checkbox group) and regular_news_count from
    POST, validated against WordPressScheduleSlot.CONTENT_TYPE_CHOICES.
    Returns (cleaned_dict, error_message_or_None).
    """
    time_str = request.POST.get('time_of_day', '').strip()
    try:
        time_of_day = datetime.strptime(time_str, '%H:%M').time()
    except ValueError:
        return None, "صيغة الوقت غير صحيحة."

    valid_keys = {c[0] for c in WordPressScheduleSlot.CONTENT_TYPE_CHOICES}
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
        return context


class ScheduleSlotCreateView(StaffRequiredMixin, View):
    def post(self, request, wp_site_id):
        wp_site = get_object_or_404(WordPressSite, pk=wp_site_id)
        cleaned, error = _parse_slot_form(request)
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
        cleaned, error = _parse_slot_form(request)
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

class SourceGroupCreateView(StaffRequiredMixin, CreateView):
    model = AISourceGroup
    fields = ['name', 'description']
    template_name = 'ai_dashboard/source_group_form.html'
    success_url = reverse_lazy('news_ai:source_groups')

    def form_valid(self, form):
        messages.success(self.request, f"??? ????? ???????? '{form.instance.name}' ?????.")
        return super().form_valid(form)

class SourceGroupUpdateView(StaffRequiredMixin, UpdateView):
    model = AISourceGroup
    fields = ['name', 'description']
    template_name = 'ai_dashboard/source_group_form.html'
    success_url = reverse_lazy('news_ai:source_groups')

    def form_valid(self, form):
        messages.success(self.request, f"?? ????? ???????? '{form.instance.name}' ?????.")
        return super().form_valid(form)

class SourceGroupDeleteView(StaffRequiredMixin, DeleteView):
    model = AISourceGroup
    template_name = 'ai_dashboard/source_group_confirm_delete.html'
    success_url = reverse_lazy('news_ai:source_groups')

    def delete(self, request, *args, **kwargs):
        obj = self.get_object()
        messages.success(self.request, f"?? ??? ???????? '{obj.name}' ?????.")
        return super().delete(request, *args, **kwargs)

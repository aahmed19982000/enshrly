import json
import uuid
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .ai_utils import (
    _find_duplicate_recent_article_for_site, fetch_news_items_from_source, get_today_total_cost,
    hold_article_pending_image, finish_pending_image_publish, _process_cover_image_bytes,
)
from .models import (
    AIImportLog, AISettings, AISource, AISourceGroup, Article, WordPressSite, WordPressScheduleSlot,
    WPConnectionToken,
)


def _fake_jpeg_bytes(size=(600, 400), color=(200, 50, 50)):
    from io import BytesIO
    from PIL import Image
    buf = BytesIO()
    Image.new('RGB', size, color).save(buf, format='JPEG')
    return buf.getvalue()


class WPConnectAPITests(TestCase):
    def setUp(self):
        self.url = reverse('news_ai:wp_connect')
        self.token = WPConnectionToken.objects.create(client_name='Test Client', package_daily_limit=5)

    def _post(self, payload):
        return self.client.post(self.url, data=json.dumps(payload), content_type='application/json')

    def test_valid_unused_token_creates_site_and_marks_token_used(self):
        resp = self._post({
            'token': str(self.token.token), 'site_url': 'https://client-site.com',
            'username': 'admin', 'application_password': 'app-pass-123',
        })

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['status'], 'success')
        self.token.refresh_from_db()
        self.assertTrue(self.token.is_used)
        self.assertIsNotNone(self.token.wp_site)
        self.assertEqual(self.token.wp_site.url, 'https://client-site.com')

    def test_missing_required_field_rejected(self):
        resp = self._post({'token': str(self.token.token), 'site_url': 'https://client-site.com'})

        self.assertEqual(resp.status_code, 400)

    def test_garbage_token_rejected(self):
        resp = self._post({
            'token': 'not-a-real-token', 'site_url': 'https://client-site.com',
            'username': 'admin', 'application_password': 'app-pass-123',
        })

        self.assertEqual(resp.status_code, 400)

    def test_unknown_uuid_token_rejected(self):
        resp = self._post({
            'token': str(uuid.uuid4()), 'site_url': 'https://client-site.com',
            'username': 'admin', 'application_password': 'app-pass-123',
        })

        self.assertEqual(resp.status_code, 403)

    def test_expired_token_rejected(self):
        self.token.expires_at = timezone.now() - timedelta(days=1)
        self.token.save(update_fields=['expires_at'])

        resp = self._post({
            'token': str(self.token.token), 'site_url': 'https://client-site.com',
            'username': 'admin', 'application_password': 'app-pass-123',
        })

        self.assertEqual(resp.status_code, 403)

    def test_used_token_different_site_url_rejected(self):
        site = WordPressSite.objects.create(
            name='Existing Site', url='https://original-site.com', username='u', application_password='p', daily_limit=5,
        )
        self.token.is_used = True
        self.token.wp_site = site
        self.token.save(update_fields=['is_used', 'wp_site'])

        resp = self._post({
            'token': str(self.token.token), 'site_url': 'https://a-completely-different-site.com',
            'username': 'admin', 'application_password': 'app-pass-123',
        })

        self.assertEqual(resp.status_code, 403)

    def test_used_token_same_site_url_updates_existing_site(self):
        site = WordPressSite.objects.create(
            name='Existing Site', url='https://client-site.com', username='old-user', application_password='p', daily_limit=5,
        )
        self.token.is_used = True
        self.token.wp_site = site
        self.token.save(update_fields=['is_used', 'wp_site'])

        resp = self._post({
            'token': str(self.token.token), 'site_url': 'https://client-site.com',
            'username': 'new-user', 'application_password': 'new-pass',
        })

        self.assertEqual(resp.status_code, 200)
        site.refresh_from_db()
        self.assertEqual(site.username, 'new-user')
        self.assertEqual(WordPressSite.objects.count(), 1)


class WPPluginDataAPITests(TestCase):
    def setUp(self):
        self.url = reverse('news_ai:wp_plugin_data_api')
        # Distinct names to avoid the unique-name collision with the real
        # group hierarchy seeded by syndicator/migrations/0013 (e.g. "الاقتصاد").
        self.parent_group = AISourceGroup.objects.create(name='__Test Parent Group__')
        self.child_group = AISourceGroup.objects.create(name='__Test Child Group__', parent=self.parent_group, is_price_articles_group=True)

    def test_returns_source_groups_with_hierarchy_and_content_types(self):
        resp = self.client.get(self.url)

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body['status'], 'success')
        groups_by_id = {g['id']: g for g in body['data']['source_groups']}
        self.assertIsNone(groups_by_id[self.parent_group.id]['parent_id'])
        self.assertEqual(groups_by_id[self.child_group.id]['parent_id'], self.parent_group.id)
        self.assertTrue(groups_by_id[self.child_group.id]['is_price_articles_group'])
        self.assertTrue(any(ct['id'] == 'gold' for ct in body['data']['content_types']))

    def test_valid_token_reflects_site_daily_limit(self):
        site = WordPressSite.objects.create(
            name='Site', url='https://s.com', username='u', application_password='p', daily_limit=42,
        )
        token = WPConnectionToken.objects.create(client_name='C', package_daily_limit=5, is_used=True, wp_site=site)

        resp = self.client.get(f"{self.url}?token={token.token}")

        self.assertEqual(resp.json()['data']['daily_limit'], 42)

    def test_missing_token_returns_default_limit_not_an_error(self):
        resp = self.client.get(self.url)

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['data']['daily_limit'], 3)


class WPPostPublishedAPITests(TestCase):
    def setUp(self):
        self.url = reverse('news_ai:wp_post_published_api')
        self.site = WordPressSite.objects.create(
            name='Site', url='https://client-site.com', username='u', application_password='p',
            daily_limit=5, is_active=True,
        )
        self.token = WPConnectionToken.objects.create(
            client_name='C', package_daily_limit=5, is_used=True, wp_site=self.site,
        )

    def _post(self, payload):
        return self.client.post(self.url, data=json.dumps(payload), content_type='application/json')

    def test_valid_used_token_matching_site_url_accepted(self):
        resp = self._post({'token': str(self.token.token), 'site_url': 'https://client-site.com'})

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['status'], 'ok')

    def test_unused_token_rejected(self):
        unused_token = WPConnectionToken.objects.create(client_name='C2', package_daily_limit=5)

        resp = self._post({'token': str(unused_token.token), 'site_url': 'https://client-site.com'})

        self.assertEqual(resp.status_code, 403)

    def test_site_url_mismatch_rejected(self):
        resp = self._post({'token': str(self.token.token), 'site_url': 'https://a-different-site.com'})

        self.assertEqual(resp.status_code, 403)

    def test_inactive_addon_skips_without_dispatching_task(self):
        # self.site never set social_image_enabled=True, so facebook_addon_is_active
        # is False by default - the "skipped" branch, not the task dispatch, is hit.
        with patch('syndicator.tasks.generate_and_publish_social_share_task.delay') as mock_delay:
            resp = self._post({'token': str(self.token.token), 'site_url': 'https://client-site.com'})

        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json().get('skipped'))
        mock_delay.assert_not_called()

    def test_active_addon_dispatches_task_instead_of_running_inline(self):
        # Regression test: this used to call generate_and_publish_social_share_from_wp_payload
        # directly inline, which could exceed gunicorn's worker timeout on a slow
        # image download + Facebook API call and silently vanish with no log
        # line and no SocialSharePost row at all - see generate_and_publish_social_share_task.
        self.site.social_image_enabled = True
        self.site.save(update_fields=['social_image_enabled'])

        with patch('syndicator.tasks.generate_and_publish_social_share_task.delay') as mock_delay:
            resp = self._post({
                'token': str(self.token.token), 'site_url': 'https://client-site.com',
                'title': 'خبر تجريبي', 'link': 'https://client-site.com/story/1', 'image_url': '',
            })

        self.assertEqual(resp.status_code, 200)
        self.assertNotIn('skipped', resp.json())
        mock_delay.assert_called_once()
        called_args = mock_delay.call_args.args
        self.assertEqual(called_args[0], self.site.id)
        self.assertEqual(called_args[1].get('link'), 'https://client-site.com/story/1')


class AISettingsAPITests(TestCase):
    def setUp(self):
        self.staff_user = User.objects.create_user(username='staffuser', password='x', is_staff=True)
        self.ai_settings = AISettings.get_settings()
        self.ai_settings.default_authors.set([self.staff_user])
        self.url = reverse('news_ai:ai_settings')

    def test_correct_bearer_token_returns_settings(self):
        resp = self.client.get(self.url, HTTP_AUTHORIZATION=f"Bearer {self.ai_settings.api_token}")

        self.assertEqual(resp.status_code, 200)
        self.assertIn('articles_per_day', resp.json())

    def test_wrong_bearer_token_rejected(self):
        resp = self.client.get(self.url, HTTP_AUTHORIZATION="Bearer not-the-real-token")

        self.assertIn(resp.status_code, (401, 403))

    def test_missing_auth_header_rejected(self):
        resp = self.client.get(self.url)

        self.assertIn(resp.status_code, (401, 403))


class FindDuplicateRecentArticleForSiteTests(TestCase):
    def setUp(self):
        self.source = AISource.objects.create(name='Source A', url='https://a.com/rss')
        self.site = WordPressSite.objects.create(
            name='Site', url='https://s.com', username='u', application_password='p', daily_limit=5,
        )

    def test_same_story_from_gemini_returns_true(self):
        AIImportLog.objects.create(
            wp_site=self.site, source_url='https://a.com/1',
            title='الأهلي يفوز على الزمالك بثلاثية في قمة الدوري', status='success',
        )
        item = {'title': 'فوز كاسح للأهلي على غريمه التقليدي الزمالك', 'link': 'https://b.com/1'}

        with patch('syndicator.ai_utils.call_gemini_api', return_value=('{"is_duplicate": true}', {'input_tokens': 100, 'output_tokens': 10})):
            result = _find_duplicate_recent_article_for_site(self.site, self.source, item, api_key='fake-key')

        self.assertTrue(result)

    def test_different_story_from_gemini_returns_false(self):
        AIImportLog.objects.create(
            wp_site=self.site, source_url='https://a.com/1',
            title='الأهلي يفوز على الزمالك بثلاثية في قمة الدوري', status='success',
        )
        item = {'title': 'وزارة الصحة تطلق حملة تطعيم جديدة', 'link': 'https://b.com/2'}

        with patch('syndicator.ai_utils.call_gemini_api', return_value=('{"is_duplicate": false}', {'input_tokens': 100, 'output_tokens': 10})):
            result = _find_duplicate_recent_article_for_site(self.site, self.source, item, api_key='fake-key')

        self.assertFalse(result)

    def test_site_with_no_recent_history_short_circuits_without_calling_gemini(self):
        item = {'title': 'أي خبر', 'link': 'https://b.com/3'}

        with patch('syndicator.ai_utils.call_gemini_api') as mocked:
            result = _find_duplicate_recent_article_for_site(self.site, self.source, item, api_key='fake-key')

        self.assertFalse(result)
        self.assertFalse(mocked.called)

    def test_real_gemini_call_is_logged_toward_the_daily_cost_cap(self):
        AIImportLog.objects.create(
            wp_site=self.site, source_url='https://a.com/1', title='خبر سابق', status='success',
        )
        item = {'title': 'خبر جديد', 'link': 'https://b.com/4'}
        cost_before = get_today_total_cost() or 0

        with patch('syndicator.ai_utils.call_gemini_api', return_value=('{"is_duplicate": false}', {'input_tokens': 500, 'output_tokens': 20})):
            _find_duplicate_recent_article_for_site(self.site, self.source, item, api_key='fake-key')

        self.assertGreater(get_today_total_cost(), cost_before)

    def test_dedup_check_log_entry_never_collides_with_exact_url_dedup(self):
        AIImportLog.objects.create(
            wp_site=self.site, source_url='https://a.com/1', title='خبر سابق', status='success',
        )
        item = {'title': 'خبر جديد', 'link': 'https://b.com/5'}

        with patch('syndicator.ai_utils.call_gemini_api', return_value=('{"is_duplicate": false}', {'input_tokens': 500, 'output_tokens': 20})):
            _find_duplicate_recent_article_for_site(self.site, self.source, item, api_key='fake-key')

        # The exact-URL duplicate-import check elsewhere in ai_utils.py filters
        # on an EXACT match of source_url - this must never match after a dedup check.
        self.assertFalse(AIImportLog.objects.filter(source_url=item['link'], status='success').exists())


RSS_FEED_XML = """<?xml version="1.0"?>
<rss version="2.0"><channel>
<item>
<title>عنوان الخبر الأول</title>
<link>https://example.com/news/1</link>
<description>&lt;p&gt;وصف الخبر&lt;/p&gt;</description>
<enclosure url="https://example.com/img1.jpg" />
<guid>https://example.com/news/1</guid>
</item>
</channel></rss>
"""

GOOGLE_NEWS_SITEMAP_XML = """<?xml version="1.0"?>
<urlset xmlns:news="http://www.google.com/schemas/sitemap-news/0.9" xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<url>
<loc>https://example.com/news/2</loc>
<news:news><news:title>عنوان خبر السيتماب</news:title></news:news>
</url>
</urlset>
"""

PLAIN_SITEMAP_XML = """<?xml version="1.0"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<url><loc>https://example.com/news/3</loc><lastmod>2026-01-01</lastmod></url>
</urlset>
"""


class FetchNewsItemsFromSourceTests(TestCase):
    def _fake_response(self, content):
        mock_resp = type('R', (), {})()
        mock_resp.content = content.encode()
        mock_resp.raise_for_status = lambda: None
        return mock_resp

    @patch('syndicator.ai_utils.requests.get')
    def test_parses_standard_rss_item(self, mock_get):
        mock_get.return_value = self._fake_response(RSS_FEED_XML)

        items = fetch_news_items_from_source('https://example.com/rss')

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['title'], 'عنوان الخبر الأول')
        self.assertEqual(items[0]['link'], 'https://example.com/news/1')
        self.assertEqual(items[0]['image_url'], 'https://example.com/img1.jpg')

    @patch('syndicator.ai_utils._scrape_image_from_article_page', return_value='https://example.com/scraped.jpg')
    @patch('syndicator.ai_utils.requests.get')
    def test_parses_google_news_sitemap(self, mock_get, _mock_scrape_image):
        mock_get.return_value = self._fake_response(GOOGLE_NEWS_SITEMAP_XML)

        items = fetch_news_items_from_source('https://example.com/feed/sitemap')

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['title'], 'عنوان خبر السيتماب')
        self.assertEqual(items[0]['link'], 'https://example.com/news/2')
        self.assertEqual(items[0]['image_url'], 'https://example.com/scraped.jpg')

    @patch('syndicator.ai_utils._scrape_title_and_image_from_article_page', return_value=('عنوان مسحوب من الصفحة', 'https://example.com/scraped2.jpg'))
    @patch('syndicator.ai_utils.requests.get')
    def test_parses_plain_wordpress_sitemap_with_no_title(self, mock_get, _mock_scrape):
        mock_get.return_value = self._fake_response(PLAIN_SITEMAP_XML)

        items = fetch_news_items_from_source('https://example.com/post-sitemap.xml')

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['title'], 'عنوان مسحوب من الصفحة')
        self.assertEqual(items[0]['link'], 'https://example.com/news/3')
        self.assertEqual(items[0]['image_url'], 'https://example.com/scraped2.jpg')


class SourceProxyRoutingTests(TestCase):
    """
    A handful of feeds (dostor.org, sonna.so, ...) block this server's own
    IP specifically (403 / redirect loops) while working fine from anywhere
    else - AISource.use_proxy + settings.SCRAPING_PROXY_URL lets staff route
    just those through a proxy without paying proxy-bandwidth cost for every
    other source. Covers _proxies_for_source's gating logic and that it
    actually reaches requests.get for the source fetch itself.
    """
    def _fake_response(self, content):
        mock_resp = type('R', (), {})()
        mock_resp.content = content.encode()
        mock_resp.raise_for_status = lambda: None
        return mock_resp

    def test_no_proxy_dict_when_source_has_use_proxy_false(self):
        from .ai_utils import _proxies_for_source
        source = AISource(name='S', url='https://a.com/rss', use_proxy=False)
        self.assertIsNone(_proxies_for_source(source))

    @override_settings(SCRAPING_PROXY_URL='')
    def test_no_proxy_dict_when_flagged_but_nothing_configured_server_side(self):
        from .ai_utils import _proxies_for_source
        source = AISource(name='S', url='https://a.com/rss', use_proxy=True)
        self.assertIsNone(_proxies_for_source(source))

    @override_settings(SCRAPING_PROXY_URL='http://user:pass@proxy.example:8080')
    def test_proxy_dict_when_flagged_and_configured(self):
        from .ai_utils import _proxies_for_source
        source = AISource(name='S', url='https://a.com/rss', use_proxy=True)
        self.assertEqual(
            _proxies_for_source(source),
            {'http': 'http://user:pass@proxy.example:8080', 'https': 'http://user:pass@proxy.example:8080'},
        )

    @override_settings(SCRAPING_PROXY_URL='http://user:pass@proxy.example:8080')
    @patch('syndicator.ai_utils.requests.get')
    def test_fetch_news_items_passes_proxies_through_to_requests(self, mock_get):
        mock_get.return_value = self._fake_response(RSS_FEED_XML)

        fetch_news_items_from_source(
            'https://a.com/rss', proxies={'http': 'http://user:pass@proxy.example:8080', 'https': 'http://user:pass@proxy.example:8080'},
        )

        mock_get.assert_called_once()
        self.assertEqual(
            mock_get.call_args.kwargs.get('proxies'),
            {'http': 'http://user:pass@proxy.example:8080', 'https': 'http://user:pass@proxy.example:8080'},
        )

    @patch('syndicator.ai_utils.requests.get')
    def test_fetch_news_items_passes_none_proxies_by_default(self, mock_get):
        mock_get.return_value = self._fake_response(RSS_FEED_XML)

        fetch_news_items_from_source('https://a.com/rss')

        self.assertIsNone(mock_get.call_args.kwargs.get('proxies'))


class GenerationLoopIndependenceTests(TestCase):
    """
    Confirms every WordPressSite always gets its own independent generation
    call, with no reword-from-another-site shortcut - the behavior this
    session's merge_group removal was meant to guarantee.
    """
    def test_reword_function_no_longer_exists(self):
        from . import ai_utils
        self.assertFalse(hasattr(ai_utils, 'reword_regular_article_for_site'))

    @patch('syndicator.ai_utils.generate_regular_article_for_site')
    def test_two_sites_sharing_a_source_are_each_generated_independently(self, mock_generate):
        from .ai_utils import run_ai_generation_cycle
        from .models import WordPressSiteGroup

        mock_generate.return_value = {'published': True}

        ai_settings = AISettings.get_settings()
        ai_settings.is_active = True
        ai_settings.articles_per_day = 100
        ai_settings.gemini_api_key = 'fake-test-key'
        ai_settings.save()

        source = AISource.objects.create(name='Shared Source', url='https://shared.com/rss')
        merge_group = WordPressSiteGroup.objects.create(name='Test Merge Group', is_active=True)
        site_a = WordPressSite.objects.create(
            name='Site A', url='https://a.com', username='u', application_password='p',
            daily_limit=50, articles_per_run=50, is_active=True, merge_group=merge_group,
        )
        site_b = WordPressSite.objects.create(
            name='Site B', url='https://b.com', username='u', application_password='p',
            daily_limit=50, articles_per_run=50, is_active=True, merge_group=merge_group,
        )
        site_a.sources.add(source)
        site_b.sources.add(source)

        fake_item = {
            'title': 'خبر تجريبي', 'link': 'https://shared.com/story/1',
            'description': 'تفاصيل', 'image_url': '', 'guid': 'https://shared.com/story/1',
        }
        with patch('syndicator.ai_utils.fetch_news_items_from_source', return_value=[fake_item]):
            run_ai_generation_cycle()

        self.assertEqual(mock_generate.call_count, 2)
        called_sites = {call.args[0].id for call in mock_generate.call_args_list}
        self.assertEqual(called_sites, {site_a.id, site_b.id})

    @patch('syndicator.ai_utils.generate_regular_article_for_site')
    def test_only_first_site_sharing_an_item_prefers_the_source_image(self, mock_generate):
        """
        Second+ site sharing the same item must not reuse the exact same
        source photo as the first - see prefer_source_image on
        generate_regular_article_for_site and the image-diversification
        fallback chain it drives.
        """
        from .ai_utils import run_ai_generation_cycle
        from .models import WordPressSiteGroup

        mock_generate.return_value = {'published': True}

        ai_settings = AISettings.get_settings()
        ai_settings.is_active = True
        ai_settings.articles_per_day = 100
        ai_settings.gemini_api_key = 'fake-test-key'
        ai_settings.save()

        source = AISource.objects.create(name='Shared Source', url='https://shared.com/rss')
        merge_group = WordPressSiteGroup.objects.create(name='Test Merge Group 2', is_active=True)
        site_a = WordPressSite.objects.create(
            name='Site A2', url='https://a2.com', username='u', application_password='p',
            daily_limit=50, articles_per_run=50, is_active=True, merge_group=merge_group,
        )
        site_b = WordPressSite.objects.create(
            name='Site B2', url='https://b2.com', username='u', application_password='p',
            daily_limit=50, articles_per_run=50, is_active=True, merge_group=merge_group,
        )
        site_a.sources.add(source)
        site_b.sources.add(source)

        fake_item = {
            'title': 'خبر تجريبي 2', 'link': 'https://shared.com/story/2',
            'description': 'تفاصيل', 'image_url': 'https://shared.com/photo.jpg', 'guid': 'https://shared.com/story/2',
        }
        with patch('syndicator.ai_utils.fetch_news_items_from_source', return_value=[fake_item]):
            run_ai_generation_cycle()

        self.assertEqual(mock_generate.call_count, 2)
        prefer_flags = [call.kwargs.get('prefer_source_image') for call in mock_generate.call_args_list]
        self.assertEqual(sorted(prefer_flags), [False, True])


class SourceGroupMembershipGenerationTests(TestCase):
    """
    A WordPressSite that only picked source_groups (the "مجموعات المصادر
    المفضلة" screen in the WordPress plugin, saved via wp_connect_api_view)
    - with nothing in the legacy per-source `sources` M2M that staff set
    manually from the Django admin - must still be eligible for articles
    from any AISource belonging to one of those groups. Regression test for
    the customer-facing group picker being silently disconnected from
    run_ai_generation_cycle's site-matching query.
    """
    @patch('syndicator.ai_utils.generate_regular_article_for_site')
    def test_site_selected_via_source_group_only_still_receives_articles(self, mock_generate):
        from .ai_utils import run_ai_generation_cycle

        mock_generate.return_value = {'published': True}

        ai_settings = AISettings.get_settings()
        ai_settings.is_active = True
        ai_settings.articles_per_day = 100
        ai_settings.gemini_api_key = 'fake-test-key'
        ai_settings.save()

        group = AISourceGroup.objects.create(name='أخبار عامة تجريبية')
        source = AISource.objects.create(name='Grouped Source', url='https://grouped.com/rss', group=group)

        site = WordPressSite.objects.create(
            name='Group-Only Site', url='https://group-only.com', username='u', application_password='p',
            daily_limit=50, articles_per_run=50, is_active=True,
        )
        site.source_groups.add(group)
        # Deliberately NOT calling site.sources.add(source) - this is the whole point.

        fake_item = {
            'title': 'خبر عبر مجموعة', 'link': 'https://grouped.com/story/1',
            'description': 'تفاصيل', 'image_url': '', 'guid': 'https://grouped.com/story/1',
        }
        with patch('syndicator.ai_utils.fetch_news_items_from_source', return_value=[fake_item]):
            run_ai_generation_cycle()

        mock_generate.assert_called_once()
        self.assertEqual(mock_generate.call_args.args[0].id, site.id)

    @patch('syndicator.ai_utils.generate_regular_article_for_site')
    def test_site_in_neither_sources_nor_matching_source_group_is_skipped(self, mock_generate):
        from .ai_utils import run_ai_generation_cycle

        ai_settings = AISettings.get_settings()
        ai_settings.is_active = True
        ai_settings.articles_per_day = 100
        ai_settings.gemini_api_key = 'fake-test-key'
        ai_settings.save()

        group = AISourceGroup.objects.create(name='مجموعة غير مرتبطة')
        other_group = AISourceGroup.objects.create(name='مجموعة الموقع')
        source = AISource.objects.create(name='Ungrouped-Match Source', url='https://nomatch.com/rss', group=group)

        site = WordPressSite.objects.create(
            name='Unrelated Site', url='https://unrelated.com', username='u', application_password='p',
            daily_limit=50, articles_per_run=50, is_active=True,
        )
        site.source_groups.add(other_group)

        fake_item = {
            'title': 'خبر غير مرتبط', 'link': 'https://nomatch.com/story/1',
            'description': 'تفاصيل', 'image_url': '', 'guid': 'https://nomatch.com/story/1',
        }
        with patch('syndicator.ai_utils.fetch_news_items_from_source', return_value=[fake_item]):
            run_ai_generation_cycle()

        mock_generate.assert_not_called()


class ScheduleSlotGroupTokenTests(TestCase):
    """
    The WordPress plugin's own "جدولة النشر" schedule UI saves one
    'group_<source_group_id>' token per مجموعة مصادر مفضلة selected for a
    time slot (see enshrly-connector.php) - it never sends the literal
    string 'regular'. get_regular_news_run_cap used to look for exactly
    'regular', so any site with schedule slots configured (effectively any
    self-serve customer who used the plugin's own scheduling screen) never
    received regular news on schedule: the cap was always 0. Regression
    tests for get_due_slot_for_regular_news recognizing group_<id> tokens.
    """
    def _cairo_now_time(self):
        from .ai_utils import CAIRO_TZ
        return timezone.now().astimezone(CAIRO_TZ).time().replace(second=0, microsecond=0)

    def test_slot_with_group_token_due_now_yields_its_configured_cap(self):
        from .ai_utils import get_regular_news_run_cap

        group = AISourceGroup.objects.create(name='مجموعة جدولة تجريبية')
        site = WordPressSite.objects.create(
            name='Scheduled Site', url='https://scheduled.example', username='u', application_password='p',
            daily_limit=50, articles_per_run=50, is_active=True,
        )
        site.source_groups.add(group)
        WordPressScheduleSlot.objects.create(
            wp_site=site, time_of_day=self._cairo_now_time(),
            content_types=f'group_{group.id}', regular_news_count=7, is_active=True,
        )

        cap, due_slot = get_regular_news_run_cap(site)
        self.assertEqual(cap, 7)
        self.assertIsNotNone(due_slot)

    def test_slot_with_only_price_types_is_not_a_regular_news_slot(self):
        from .ai_utils import get_regular_news_run_cap

        site = WordPressSite.objects.create(
            name='Price Only Site', url='https://priceonly.example', username='u', application_password='p',
            daily_limit=50, articles_per_run=50, is_active=True,
        )
        WordPressScheduleSlot.objects.create(
            wp_site=site, time_of_day=self._cairo_now_time(),
            content_types='gold,silver', regular_news_count=5, is_active=True,
        )

        cap, due_slot = get_regular_news_run_cap(site)
        self.assertEqual(cap, 0)
        self.assertIsNone(due_slot)

    def test_slot_with_group_token_not_due_yet_yields_zero(self):
        from .ai_utils import get_regular_news_run_cap
        from datetime import time as dt_time

        group = AISourceGroup.objects.create(name='مجموعة جدولة تجريبية 2')
        site = WordPressSite.objects.create(
            name='Not Due Site', url='https://notdue.example', username='u', application_password='p',
            daily_limit=50, articles_per_run=50, is_active=True,
        )
        site.source_groups.add(group)
        now_cairo = self._cairo_now_time()
        far_hour = (now_cairo.hour + 6) % 24
        WordPressScheduleSlot.objects.create(
            wp_site=site, time_of_day=dt_time(hour=far_hour, minute=now_cairo.minute),
            content_types=f'group_{group.id}', regular_news_count=7, is_active=True,
        )

        cap, due_slot = get_regular_news_run_cap(site)
        self.assertEqual(cap, 0)
        self.assertIsNone(due_slot)

    @patch('syndicator.ai_utils.generate_regular_article_for_site')
    def test_full_cycle_generates_for_group_scheduled_site_when_due(self, mock_generate):
        from .ai_utils import run_ai_generation_cycle

        mock_generate.return_value = {'published': True}

        ai_settings = AISettings.get_settings()
        ai_settings.is_active = True
        ai_settings.articles_per_day = 100
        ai_settings.gemini_api_key = 'fake-test-key'
        ai_settings.save()

        group = AISourceGroup.objects.create(name='مجموعة جدولة كاملة')
        source = AISource.objects.create(name='Scheduled Group Source', url='https://sched-group.com/rss', group=group)

        site = WordPressSite.objects.create(
            name='Full Cycle Scheduled Site', url='https://full-cycle-scheduled.example', username='u',
            application_password='p', daily_limit=50, articles_per_run=50, is_active=True,
        )
        site.source_groups.add(group)
        WordPressScheduleSlot.objects.create(
            wp_site=site, time_of_day=self._cairo_now_time(),
            content_types=f'group_{group.id}', regular_news_count=3, is_active=True,
        )

        fake_item = {
            'title': 'خبر مجدول عبر مجموعة', 'link': 'https://sched-group.com/story/1',
            'description': 'تفاصيل', 'image_url': '', 'guid': 'https://sched-group.com/story/1',
        }
        with patch('syndicator.ai_utils.fetch_news_items_from_source', return_value=[fake_item]):
            run_ai_generation_cycle()

        mock_generate.assert_called_once()
        self.assertEqual(mock_generate.call_args.args[0].id, site.id)


class ScheduleSlotStaffAdminGroupVisibilityTests(TestCase):
    """
    The staff-facing schedule page (/ai-dashboard/wp-sites/<id>/schedule/)
    only ever rendered checkboxes for the fixed price CONTENT_TYPE_CHOICES -
    a slot's 'group_<id>' tokens (written by the customer via the WordPress
    plugin's own schedule screen) were invisible, AND saving the form would
    silently strip them out (the old _parse_slot_form only accepted the
    fixed choices, so a group-only slot's content_types became empty and
    either got wiped or failed validation entirely). Regression coverage for
    both the visibility and the data-loss-on-save parts of that bug.
    """
    def setUp(self):
        self.staff = User.objects.create_user(username='schedule_staff', password='x', is_staff=True)
        self.group = AISourceGroup.objects.create(name='مجموعة الجدولة الإدارية')
        self.site = WordPressSite.objects.create(
            name='Admin Schedule Site', url='https://admin-schedule.example', username='u',
            application_password='p', daily_limit=5, is_active=True,
        )
        self.site.source_groups.add(self.group)
        self.slot = WordPressScheduleSlot.objects.create(
            wp_site=self.site, time_of_day='09:00', content_types=f'group_{self.group.id}',
            regular_news_count=4, is_active=True,
        )
        self.client.force_login(self.staff)
        self.url = f'/ai-dashboard/wp-sites/{self.site.id}/schedule/'

    def test_page_shows_the_customers_group_selection_checked(self):
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode('utf-8')
        self.assertIn('مجموعة الجدولة الإدارية', body)
        import re
        m = re.search(rf'name="content_types" value="group_{self.group.id}"[^>]*', body)
        self.assertIsNotNone(m)
        self.assertIn('checked', m.group(0))

    def test_resaving_the_slot_does_not_strip_the_group_token(self):
        edit_url = f'/ai-dashboard/wp-sites/{self.site.id}/schedule/{self.slot.id}/edit/'
        resp = self.client.post(edit_url, {
            'time_of_day': '10:30',
            'regular_news_count': '6',
            'is_active': 'on',
            'content_types': [f'group_{self.group.id}'],
        })
        self.assertEqual(resp.status_code, 302)
        self.slot.refresh_from_db()
        self.assertIn(f'group_{self.group.id}', self.slot.get_content_types_list())
        self.assertEqual(self.slot.regular_news_count, 6)

    def test_new_slot_can_be_created_with_only_a_group_token(self):
        add_url = f'/ai-dashboard/wp-sites/{self.site.id}/schedule/add/'
        resp = self.client.post(add_url, {
            'time_of_day': '14:00',
            'regular_news_count': '2',
            'content_types': [f'group_{self.group.id}'],
        })
        self.assertEqual(resp.status_code, 302)
        new_slot = WordPressScheduleSlot.objects.filter(wp_site=self.site, time_of_day='14:00').first()
        self.assertIsNotNone(new_slot)
        self.assertEqual(new_slot.get_content_types_list(), [f'group_{self.group.id}'])


class ImageMirrorVariantTests(TestCase):
    def test_mirror_flips_image_horizontally(self):
        from PIL import Image
        import io

        raw = _fake_jpeg_bytes(size=(100, 60), color=(10, 200, 10))
        normal = _process_cover_image_bytes(raw, 'test.jpg')
        mirrored = _process_cover_image_bytes(raw, 'test.jpg', mirror=True)

        normal_img = Image.open(io.BytesIO(normal.read()))
        mirrored_img = Image.open(io.BytesIO(mirrored.read()))

        self.assertEqual(normal_img.size, mirrored_img.size)
        # A horizontally-flipped copy of the mirrored image must reproduce the
        # normal (unmirrored) pixels - confirms an actual flip happened, not a
        # no-op or unrelated transform.
        flipped_back = mirrored_img.transpose(Image.FLIP_LEFT_RIGHT)
        self.assertEqual(list(normal_img.getdata()), list(flipped_back.getdata()))


class PendingImageWorkflowTests(TestCase):
    """
    Covers the "no photo exists anywhere for this story" path: the article is
    held instead of publishing with a generic stock photo, and the customer
    finishes the publish themselves later via the WP plugin - see
    hold_article_pending_image / finish_pending_image_publish in ai_utils.py.
    """
    def setUp(self):
        self.author = User.objects.create_user(username='author1', password='x')
        self.source = AISource.objects.create(name='Source A', url='https://a.com/rss')
        self.site = WordPressSite.objects.create(
            name='Site', url='https://s.com', username='u', application_password='p', daily_limit=5,
        )
        self.article = Article.objects.create(
            title='خبر بدون صورة', slug=f'test-article-{uuid.uuid4().hex[:8]}',
            body='<p>نص الخبر</p>', author=self.author, status='draft',
        )

    def test_hold_creates_pending_log_without_publishing(self):
        item = {'link': 'https://a.com/story/1'}
        result = hold_article_pending_image(
            self.article, self.site, self.source, item,
            category_name_for_group='أسعار', wp_category_id_for_push=7,
            focus_keyword='كلمة مفتاحية', meta_description='وصف تعريفي',
            tag_names=['وسم1', 'وسم2'], ai_usage={'input_tokens': 50, 'output_tokens': 20},
        )

        self.assertEqual(result, {'published': False, 'held': True, 'title': self.article.title})
        log = AIImportLog.objects.get(article=self.article)
        self.assertEqual(log.status, 'pending_image')
        self.assertEqual(log.wp_site, self.site)
        self.assertEqual(log.wp_category_id, 7)
        self.assertEqual(log.meta_description, 'وصف تعريفي')
        self.assertEqual(log.tag_names, 'وسم1,وسم2')

    @patch('syndicator.ai_utils.push_article_to_wordpress', return_value='https://s.com/published-article/')
    def test_finish_publish_success_updates_log_and_article(self, mock_push):
        log = AIImportLog.objects.create(
            source=self.source, article=self.article, wp_site=self.site,
            source_url='https://a.com/story/1', title=self.article.title, status='pending_image',
            wp_category_id=3, focus_keyword='fk', meta_description='md', tag_names='t1,t2',
        )

        published_url = finish_pending_image_publish(log, _fake_jpeg_bytes(), 'chosen.jpg')

        self.assertEqual(published_url, 'https://s.com/published-article/')
        log.refresh_from_db()
        self.article.refresh_from_db()
        self.assertEqual(log.status, 'success')
        self.assertEqual(log.published_url, 'https://s.com/published-article/')
        self.assertTrue(self.article.cover_image)
        mock_push.assert_called_once()
        self.assertEqual(mock_push.call_args.kwargs.get('wp_category_id'), 3)
        self.assertEqual(mock_push.call_args.kwargs.get('focus_keyword'), 'fk')

    @patch('syndicator.ai_utils.push_article_to_wordpress', side_effect=Exception('WP is down'))
    def test_finish_publish_wp_failure_marks_log_failed_and_stays_retriable(self, mock_push):
        log = AIImportLog.objects.create(
            source=self.source, article=self.article, wp_site=self.site,
            source_url='https://a.com/story/1', title=self.article.title, status='pending_image',
        )

        published_url = finish_pending_image_publish(log, _fake_jpeg_bytes(), 'chosen.jpg')

        self.assertIsNone(published_url)
        log.refresh_from_db()
        self.assertEqual(log.status, 'failed')
        self.assertIn('WP is down', log.error_message)


class PendingImageArticlesAPITests(TestCase):
    """Covers the WP-plugin-facing endpoints for listing/resolving pending-image articles."""
    def setUp(self):
        self.author = User.objects.create_user(username='author2', password='x')
        self.site = WordPressSite.objects.create(
            name='Site', url='https://client.com', username='u', application_password='p', daily_limit=5,
        )
        self.token = WPConnectionToken.objects.create(
            client_name='C', package_daily_limit=5, is_used=True, wp_site=self.site,
        )
        self.article = Article.objects.create(
            title='خبر معلّق', slug=f'pending-article-{uuid.uuid4().hex[:8]}',
            body='<p>نص</p>', excerpt='ملخص قصير', author=self.author, status='draft',
        )
        self.log = AIImportLog.objects.create(
            article=self.article, wp_site=self.site, source_url='https://a.com/1',
            title=self.article.title, status='pending_image',
        )
        self.list_url = reverse('news_ai:pending_image_articles_api')
        self.submit_url = reverse('news_ai:submit_pending_image_api', args=[self.log.id])

    def test_list_returns_only_this_sites_pending_items(self):
        other_site = WordPressSite.objects.create(
            name='Other', url='https://other.com', username='u', application_password='p', daily_limit=5,
        )
        AIImportLog.objects.create(
            wp_site=other_site, source_url='https://a.com/2', title='خبر موقع تاني', status='pending_image',
        )

        resp = self.client.get(f"{self.list_url}?token={self.token.token}")

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(len(body['items']), 1)
        self.assertEqual(body['items'][0]['title'], 'خبر معلّق')
        self.assertEqual(body['items'][0]['excerpt'], 'ملخص قصير')

    def test_list_rejects_invalid_token(self):
        resp = self.client.get(f"{self.list_url}?token=not-a-real-token")

        self.assertEqual(resp.status_code, 403)

    @patch('requests.get')
    @patch('syndicator.ai_utils.push_article_to_wordpress', return_value='https://client.com/live/')
    def test_submit_image_downloads_and_finishes_publish(self, mock_push, mock_get):
        mock_get.return_value = type('R', (), {
            'content': _fake_jpeg_bytes(), 'raise_for_status': lambda self=None: None,
        })()

        resp = self.client.post(self.submit_url, {
            'token': str(self.token.token), 'image_url': 'https://media.client.com/chosen.jpg',
        })

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['status'], 'success')
        self.log.refresh_from_db()
        self.assertEqual(self.log.status, 'success')

    def test_submit_missing_image_url_returns_400(self):
        resp = self.client.post(self.submit_url, {'token': str(self.token.token)})

        self.assertEqual(resp.status_code, 400)

    def test_submit_rejects_invalid_token(self):
        resp = self.client.post(self.submit_url, {
            'token': 'not-a-real-token', 'image_url': 'https://media.client.com/chosen.jpg',
        })

        self.assertEqual(resp.status_code, 403)

    def test_submit_unknown_log_id_returns_404(self):
        resp = self.client.post(
            reverse('news_ai:submit_pending_image_api', args=[999999]),
            {'token': str(self.token.token), 'image_url': 'https://media.client.com/chosen.jpg'},
        )

        self.assertEqual(resp.status_code, 404)


class PublishedArticlesLogAPITests(TestCase):
    """Covers the WP-plugin-facing publishing log ('what actually went live on my site')."""
    def setUp(self):
        self.site = WordPressSite.objects.create(
            name='Site', url='https://client.com', username='u', application_password='p', daily_limit=5,
        )
        self.token = WPConnectionToken.objects.create(
            client_name='C', package_daily_limit=5, is_used=True, wp_site=self.site,
        )
        self.url = reverse('news_ai:published_articles_log_api')

    def test_lists_only_successful_published_entries_for_this_site(self):
        AIImportLog.objects.create(
            wp_site=self.site, source_url='https://a.com/1', title='خبر منشور',
            status='success', published_url='https://client.com/khabar-1/', wp_category_name='اقتصاد',
        )
        AIImportLog.objects.create(
            wp_site=self.site, source_url='https://a.com/2', title='خبر فشل',
            status='failed', published_url='',
        )
        AIImportLog.objects.create(
            wp_site=self.site, source_url='https://a.com/3', title='خبر بانتظار صورة',
            status='pending_image', published_url='',
        )
        other_site = WordPressSite.objects.create(
            name='Other', url='https://other.com', username='u', application_password='p', daily_limit=5,
        )
        AIImportLog.objects.create(
            wp_site=other_site, source_url='https://a.com/4', title='خبر موقع تاني',
            status='success', published_url='https://other.com/khabar/',
        )

        resp = self.client.get(f"{self.url}?token={self.token.token}")

        self.assertEqual(resp.status_code, 200)
        items = resp.json()['items']
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['title'], 'خبر منشور')
        self.assertEqual(items[0]['published_url'], 'https://client.com/khabar-1/')
        self.assertEqual(items[0]['wp_category_name'], 'اقتصاد')

    def test_rejects_invalid_token(self):
        resp = self.client.get(f"{self.url}?token=not-a-real-token")

        self.assertEqual(resp.status_code, 403)

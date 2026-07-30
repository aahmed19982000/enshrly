import json
import uuid
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .ai_utils import _find_duplicate_recent_article_for_site, fetch_news_items_from_source, get_today_total_cost
from .models import AIImportLog, AISettings, AISource, AISourceGroup, WordPressSite, WPConnectionToken


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

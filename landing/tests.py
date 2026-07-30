from django.test import TestCase
from django.urls import reverse


class LandingSmokeTests(TestCase):
    """
    landing/pages are presentational, not logic-bearing - a basic 200-status
    smoke test is enough coverage here, unlike the risk-focused deep tests
    for payments/accounts/syndicator.
    """
    def test_home_page_loads(self):
        resp = self.client.get(reverse('landing:home'))

        self.assertEqual(resp.status_code, 200)

    def test_main_static_pages_load(self):
        for url_name in ['pages:about', 'pages:privacy', 'pages:terms', 'pages:refund', 'pages:contact']:
            with self.subTest(url_name=url_name):
                resp = self.client.get(reverse(url_name))
                self.assertEqual(resp.status_code, 200)

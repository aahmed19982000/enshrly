from django.contrib.sitemaps import Sitemap
from django.urls import reverse


class StaticViewSitemap(Sitemap):
    """Fixed, low-churn marketing/legal pages - priority reflects how likely
    a search engine should be to crawl/index each relative to the others."""
    changefreq = 'weekly'

    priorities = {
        'landing:home': 1.0,
        'landing:facebook_addon': 0.9,
        'payments:packages': 0.9,
        'blog:list': 0.7,
        'faq:list': 0.6,
        'pages:about': 0.6,
        'pages:contact': 0.5,
        'pages:terms': 0.2,
        'pages:privacy': 0.2,
        'pages:refund': 0.2,
    }

    def items(self):
        return list(self.priorities.keys())

    def location(self, item):
        return reverse(item)

    def priority(self, item):
        return self.priorities.get(item, 0.5)

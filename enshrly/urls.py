from django.contrib import admin
from django.urls import path, include
from django.contrib.sitemaps.views import sitemap
from django.views.generic import TemplateView
from syndicator import views as syndicator_views
from landing.sitemaps import StaticViewSitemap
from blog.sitemaps import BlogPostSitemap

sitemaps = {
    'static': StaticViewSitemap,
    'blog': BlogPostSitemap,
}

urlpatterns = [
    path('admin/', admin.site.urls),
    path('sitemap.xml', sitemap, {'sitemaps': sitemaps}, name='sitemap'),
    path('robots.txt', TemplateView.as_view(template_name='robots.txt', content_type='text/plain'), name='robots'),
    path('auth/', include('accounts.urls')),
    path('payments/', include('payments.urls')),
    path('ai-dashboard/', include('syndicator.urls')), # syndicator.urls has app_name='news_ai'
    # The WordPress connector plugin and other external clients call these at
    # the domain root (no /ai-dashboard/ prefix) — kept in sync with the same
    # paths under syndicator.urls, which those views still also answer to.
    path('api/ai-settings/', syndicator_views.AISettingsAPIView.as_view()),
    path('api/wp-connect/', syndicator_views.wp_connect_api_view),
    path('api/wp-plugin-data/', syndicator_views.wp_plugin_data_api_view),
    path('api/wp-post-published/', syndicator_views.wp_post_published_api_view),
    path('', include('pages.urls')),  # about/privacy/terms/refund-policy/contact — distinct paths, no clash with landing's '' home route
    path('blog/', include('blog.urls')),
    path('faq/', include('faq.urls')),
    path('', include('landing.urls')),
]

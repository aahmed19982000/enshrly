from django.contrib import admin
from django.urls import path, include
from syndicator import views as syndicator_views

urlpatterns = [
    path('admin/', admin.site.urls),
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
    path('', include('landing.urls')),
]

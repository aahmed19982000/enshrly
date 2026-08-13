from django.urls import path

from . import views
from . import views_ads_connect

app_name = 'ads'

urlpatterns = [
    path('', views.ads_dashboard_view, name='dashboard'),
    path('<int:wp_site_id>/connect/', views.ad_account_connect_redirect_view, name='connect_redirect'),
    path('<int:wp_site_id>/campaigns/', views.campaign_list_view, name='campaign_list'),
    path('<int:wp_site_id>/campaigns/new/', views.campaign_wizard_view, name='campaign_wizard'),
    path('<int:wp_site_id>/campaigns/<int:campaign_id>/', views.campaign_detail_view, name='campaign_detail'),
    path('<int:wp_site_id>/campaigns/<int:campaign_id>/toggle/', views.campaign_toggle_status_view, name='campaign_toggle'),

    # Ad account self-serve connect (OAuth) - literal paths must come before
    # the generic '<str:token>/' pattern, same reasoning as syndicator/urls.py's
    # Facebook Page connect flow.
    path('connect/callback/', views_ads_connect.ads_connect_callback, name='ads_connect_callback'),
    path('connect/<str:token>/choose-account/', views_ads_connect.ads_connect_choose_account, name='ads_connect_choose_account'),
    path('connect/<str:token>/', views_ads_connect.ads_connect_start, name='ads_connect_start'),
]

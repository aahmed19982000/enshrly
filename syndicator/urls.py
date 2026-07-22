from django.urls import path
from . import views as views_ai

app_name = 'news_ai'

urlpatterns = [
    path('api/ai-settings/', views_ai.AISettingsAPIView.as_view(), name='ai_settings'),
    path('', views_ai.DashboardIndexView.as_view(), name='index'),
    path('settings/', views_ai.SettingsUpdateView.as_view(), name='settings'),
    path('sources/', views_ai.SourceListView.as_view(), name='sources'),
    path('sources/add/', views_ai.SourceCreateView.as_view(), name='source_add'),
    path('sources/<int:pk>/edit/', views_ai.SourceUpdateView.as_view(), name='source_edit'),
    path('sources/<int:pk>/delete/', views_ai.SourceDeleteView.as_view(), name='source_delete'),
    # Source Groups
    path('sources/groups/', views_ai.SourceGroupListView.as_view(), name='source_groups'),
    path('sources/groups/add/', views_ai.SourceGroupCreateView.as_view(), name='source_group_add'),
    path('sources/groups/<int:pk>/edit/', views_ai.SourceGroupUpdateView.as_view(), name='source_group_edit'),
    path('sources/groups/<int:pk>/delete/', views_ai.SourceGroupDeleteView.as_view(), name='source_group_delete'),
    path('logs/', views_ai.ImportLogListView.as_view(), name='logs'),
    path('logs/<int:pk>/republish/', views_ai.RepublishLogView.as_view(), name='log_republish'),
    path('logs/bulk-redistribute/', views_ai.BulkRedistributeLogsView.as_view(), name='logs_bulk_redistribute'),
    path('trigger/', views_ai.TriggerScraperView.as_view(), name='trigger'),
    # WordPress Sites
    path('wp-sites/', views_ai.WordPressSiteListView.as_view(), name='wp_sites'),
    path('wp-sites/add/', views_ai.WordPressSiteCreateView.as_view(), name='wp_site_add'),
    path('wp-sites/<int:pk>/edit/', views_ai.WordPressSiteUpdateView.as_view(), name='wp_site_edit'),
    path('wp-sites/<int:pk>/delete/', views_ai.WordPressSiteDeleteView.as_view(), name='wp_site_delete'),
    path('wp-sites/<int:wp_site_id>/articles/', views_ai.WordPressSitePublishedArticlesView.as_view(), name='wp_site_articles'),
    path('wp-sites/<int:wp_site_id>/trigger/', views_ai.TriggerSiteScraperView.as_view(), name='wp_site_trigger'),
    path('wp-sites/logs/<int:log_id>/regenerate-social-image/', views_ai.RegenerateSocialImageView.as_view(), name='regenerate_social_image'),
    # Per-site publishing schedule slots
    path('wp-sites/<int:wp_site_id>/schedule/', views_ai.ScheduleSlotListView.as_view(), name='schedule_slots'),
    path('wp-sites/<int:wp_site_id>/schedule/add/', views_ai.ScheduleSlotCreateView.as_view(), name='schedule_slot_add'),
    path('wp-sites/<int:wp_site_id>/schedule/<int:pk>/edit/', views_ai.ScheduleSlotUpdateView.as_view(), name='schedule_slot_edit'),
    path('wp-sites/<int:wp_site_id>/schedule/<int:pk>/delete/', views_ai.ScheduleSlotDeleteView.as_view(), name='schedule_slot_delete'),
    # WordPress site merge groups
    path('wp-site-groups/', views_ai.WordPressSiteGroupListView.as_view(), name='wp_site_groups'),
    path('wp-site-groups/add/', views_ai.WordPressSiteGroupCreateView.as_view(), name='wp_site_group_add'),
    path('wp-site-groups/<int:pk>/edit/', views_ai.WordPressSiteGroupUpdateView.as_view(), name='wp_site_group_edit'),
    path('wp-site-groups/<int:pk>/delete/', views_ai.WordPressSiteGroupDeleteView.as_view(), name='wp_site_group_delete'),
]

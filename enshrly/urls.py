from django.contrib import admin
from django.urls import path, include

urlpatterns = [
    path('admin/', admin.site.urls),
    path('auth/', include('accounts.urls')),
    path('payments/', include('payments.urls')),
    path('ai-dashboard/', include('syndicator.urls')), # syndicator.urls has app_name='news_ai'
    path('', include('landing.urls')),
]

from django.contrib import admin
from .models import AISourceGroup, AISource, AISettings, WordPressSite, AIImportLog

@admin.register(AISourceGroup)
class AISourceGroupAdmin(admin.ModelAdmin):
    list_display = ('name', 'created_at')
    search_fields = ('name',)

@admin.register(AISource)
class AISourceAdmin(admin.ModelAdmin):
    list_display = ('name', 'url', 'group', 'language', 'is_active')
    list_filter = ('language', 'is_active', 'group')
    search_fields = ('name', 'url')

@admin.register(AISettings)
class AISettingsAdmin(admin.ModelAdmin):
    list_display = ('is_active', 'articles_per_day', 'max_words', 'last_run')

@admin.register(WordPressSite)
class WordPressSiteAdmin(admin.ModelAdmin):
    list_display = ('name', 'url', 'daily_limit', 'is_active')
    search_fields = ('name', 'url')

@admin.register(AIImportLog)
class AIImportLogAdmin(admin.ModelAdmin):
    list_display = ('title', 'source', 'wp_site', 'status', 'created_at')
    list_filter = ('status', 'created_at')
    search_fields = ('title', 'source_url')

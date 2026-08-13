from django.contrib import admin
from .models import AdAccountConnection, Campaign, AdSet, AdCreative, Ad, AdInsightSnapshot


@admin.register(AdAccountConnection)
class AdAccountConnectionAdmin(admin.ModelAdmin):
    list_display = ('wp_site', 'ad_account_name', 'facebook_ad_account_id', 'status', 'ads_addon_plan', 'connected_at')
    list_filter = ('status', 'ads_addon_plan')
    search_fields = ('wp_site__name', 'ad_account_name', 'facebook_ad_account_id')
    readonly_fields = ('access_token', 'connected_at', 'last_verified_at')


class AdSetInline(admin.TabularInline):
    model = AdSet
    extra = 0
    readonly_fields = ('facebook_adset_id', 'status')


@admin.register(Campaign)
class CampaignAdmin(admin.ModelAdmin):
    list_display = ('name', 'ad_account', 'objective', 'status', 'daily_budget', 'start_date', 'created_at')
    list_filter = ('status', 'objective')
    search_fields = ('name', 'facebook_campaign_id', 'ad_account__wp_site__name')
    inlines = [AdSetInline]


@admin.register(AdSet)
class AdSetAdmin(admin.ModelAdmin):
    list_display = ('campaign', 'facebook_adset_id', 'status', 'created_at')
    list_filter = ('status',)


@admin.register(AdCreative)
class AdCreativeAdmin(admin.ModelAdmin):
    list_display = ('headline', 'ad_set', 'source_type', 'created_at')
    list_filter = ('source_type',)


@admin.register(Ad)
class AdAdmin(admin.ModelAdmin):
    list_display = ('name', 'ad_set', 'status', 'created_at')
    list_filter = ('status',)


@admin.register(AdInsightSnapshot)
class AdInsightSnapshotAdmin(admin.ModelAdmin):
    list_display = ('campaign', 'date', 'impressions', 'clicks', 'spend', 'ctr')
    list_filter = ('date',)
    search_fields = ('campaign__name',)

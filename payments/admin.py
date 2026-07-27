from django.contrib import admin
from .models import SubscriptionPackage, Transaction

@admin.register(SubscriptionPackage)
class SubscriptionPackageAdmin(admin.ModelAdmin):
    list_display = ('name', 'price', 'daily_limit', 'monthly_articles_limit', 'is_recommended', 'is_active', 'is_custom')
    list_filter = ('is_active', 'is_custom', 'is_recommended')
    search_fields = ('name',)
    fieldsets = (
        ('معلومات السعر الأساسية', {
            'fields': ('name', 'price', 'features'),
        }),
        ('حدود الاستخدام', {
            'fields': ('daily_limit', 'monthly_articles_limit'),
        }),
        ('فترات الاشتراك ونسب الخصم', {
            'fields': ('quarterly_discount_percent', 'semiannual_discount_percent', 'annual_discount_percent'),
        }),
        ('الحالة', {
            'fields': ('is_recommended', 'is_active', 'is_custom'),
        }),
    )

@admin.register(Transaction)
class TransactionAdmin(admin.ModelAdmin):
    list_display = ('transaction_id', 'customer', 'package', 'amount', 'gateway', 'status', 'created_at')
    list_filter = ('gateway', 'status', 'created_at')
    search_fields = ('transaction_id', 'gateway_transaction_id')


from django.contrib import admin
from .models import SubscriptionPackage, Transaction

@admin.register(SubscriptionPackage)
class SubscriptionPackageAdmin(admin.ModelAdmin):
    list_display = ('name', 'price', 'daily_limit', 'is_active', 'is_custom')
    list_filter = ('is_active', 'is_custom')
    search_fields = ('name',)

@admin.register(Transaction)
class TransactionAdmin(admin.ModelAdmin):
    list_display = ('transaction_id', 'customer', 'package', 'amount', 'gateway', 'status', 'created_at')
    list_filter = ('gateway', 'status', 'created_at')
    search_fields = ('transaction_id', 'gateway_transaction_id')


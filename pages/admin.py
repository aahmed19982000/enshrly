from django.contrib import admin
from .models import ContactMessage


@admin.register(ContactMessage)
class ContactMessageAdmin(admin.ModelAdmin):
    list_display = ('name', 'contact_info', 'created_at')
    readonly_fields = ('created_at',)
    search_fields = ('name', 'contact_info', 'message')

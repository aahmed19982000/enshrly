from django.contrib import admin
from .models import FAQCategory, FAQItem


class FAQItemInline(admin.TabularInline):
    model = FAQItem
    extra = 1
    fields = ('question', 'answer', 'order', 'is_active')


@admin.register(FAQCategory)
class FAQCategoryAdmin(admin.ModelAdmin):
    list_display = ('name', 'order')
    inlines = [FAQItemInline]


@admin.register(FAQItem)
class FAQItemAdmin(admin.ModelAdmin):
    list_display = ('question', 'category', 'order', 'is_active')
    list_filter = ('category', 'is_active')
    list_editable = ('order', 'is_active')
    search_fields = ('question', 'answer')

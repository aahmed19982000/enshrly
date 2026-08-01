from django.shortcuts import render
from django.db.models import Prefetch
from .models import FAQCategory, FAQItem


def faq_list_view(request):
    categories = FAQCategory.objects.prefetch_related(
        Prefetch('items', queryset=FAQItem.objects.filter(is_active=True))
    ).all()
    categories = [c for c in categories if c.items.all()]
    return render(request, 'faq/list.html', {'categories': categories})

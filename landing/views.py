from django.shortcuts import render
from payments.models import SubscriptionPackage

def home_view(request):
    packages = SubscriptionPackage.objects.filter(is_active=True).order_by('price')
    return render(request, 'landing/home.html', {'packages': packages})

from django.shortcuts import render
from payments.models import SubscriptionPackage, FacebookAddonPlan

def home_view(request):
    packages = SubscriptionPackage.objects.filter(is_active=True).order_by('price')
    period_prices = {pkg.id: pkg.all_period_prices() for pkg in packages}

    # أعلى نسبة خصم متاحة لكل فترة عبر كل الباقات المعروضة، لإظهار شارة تسويقية
    # على زرار الفترة نفسه (مثلاً "وفّر حتى 20%") بدل تكرارها داخل كل كارت.
    max_period_discount = {
        period: max((prices[period]['discount'] for prices in period_prices.values()), default=0)
        for period, _ in SubscriptionPackage.BILLING_PERIOD_CHOICES
    }

    return render(request, 'landing/home.html', {
        'packages': packages,
        'period_prices': period_prices,
        'max_period_discount': max_period_discount,
    })

def facebook_addon_view(request):
    plans = FacebookAddonPlan.objects.filter(is_active=True).order_by('price')
    period_prices = {plan.id: plan.all_period_prices() for plan in plans}

    max_period_discount = {
        period: max((prices[period]['discount'] for prices in period_prices.values()), default=0)
        for period, _ in FacebookAddonPlan.BILLING_PERIOD_CHOICES
    }

    return render(request, 'landing/facebook_addon.html', {
        'plans': plans,
        'period_prices': period_prices,
        'max_period_discount': max_period_discount,
    })

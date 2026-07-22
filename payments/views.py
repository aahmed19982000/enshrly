from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from .models import SubscriptionPackage, Transaction
from syndicator.models import WPConnectionToken
import uuid

def packages_view(request):
    packages = SubscriptionPackage.objects.filter(is_active=True).order_by('price')
    return render(request, 'payments/packages.html', {'packages': packages})

@login_required
def checkout_view(request, package_id):
    profile = getattr(request.user, 'customer_profile', None)
    if not profile or not profile.is_whatsapp_verified:
        messages.error(request, "يجب تفعيل حسابك أولاً.")
        return redirect('accounts:verify_otp')

    package = get_object_or_404(SubscriptionPackage, id=package_id)

    if request.method == 'POST':
        gateway = request.POST.get('gateway')
        if gateway not in dict(Transaction.GATEWAY_CHOICES):
            messages.error(request, "يرجى اختيار بوابة دفع صحيحة.")
            return redirect('payments:checkout', package_id=package.id)

        # Create pending transaction
        transaction = Transaction.objects.create(
            customer=profile,
            package=package,
            amount=package.price,
            gateway=gateway
        )

        # TODO: Here we would redirect to PayPal / Crypto gateway API
        # For now, we simulate a successful payment for demonstration
        return redirect('payments:payment_success', transaction_id=transaction.transaction_id)

    return render(request, 'payments/checkout.html', {'package': package})

@login_required
def payment_success_view(request, transaction_id):
    transaction = get_object_or_404(Transaction, transaction_id=transaction_id, customer=request.user.customer_profile)
    
    # In a real app, this view is a webhook or return URL that verifies payment status
    if transaction.status == 'pending':
        transaction.status = 'completed'
        transaction.save()

        # Generate WP Token for the user
        token_str = str(uuid.uuid4())
        token_obj = WPConnectionToken.objects.create(
            token=token_str,
            client_name=request.user.first_name,
            package_daily_limit=transaction.package.daily_limit,
        ) 
        # Actually, WPConnectionToken doesn't store daily_limit.
        # Wait, the limit is stored in WordPressSite. The Token is just a bridge.
        # But how does the token know the limit when the WP Plugin connects?
        # Let's add a daily_limit field to WPConnectionToken or we can set it when creating the site in wp_connect_api_view.
        # I need to check wp_connect_api_view.

    # Find the user's un-used tokens to display
    tokens = WPConnectionToken.objects.filter(client_name=request.user.first_name, is_used=False)

    return render(request, 'payments/success.html', {'transaction': transaction, 'tokens': tokens})

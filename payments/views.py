from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.conf import settings
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.db import models
from .models import SubscriptionPackage, FacebookAddonPlan, Transaction, DevicePairing
from syndicator.models import WPConnectionToken
import uuid
import json
from decimal import Decimal
from django.utils import timezone
from datetime import timedelta
from .tasks import (
    send_payment_success_whatsapp,
    send_payment_failed_whatsapp,
    send_underpayment_whatsapp,
    send_page_closed_whatsapp,
)

def _token_expiry_days(transaction):
    """مدة صلاحية كود الربط (أو فترة تجديد باقة فيسبوك) الناتجة عن هذه المعاملة، حسب فترة الاشتراك المدفوعة فعلياً."""
    return SubscriptionPackage.BILLING_PERIOD_DAYS.get(transaction.billing_period, 30)

def _product_name(transaction):
    """اسم المنتج المدفوع فعلياً بغض النظر عن نوعه - لرسائل واتساب المشتركة بين الباقات وإضافة فيسبوك."""
    return transaction.product_display_name

def _checkout_retry_url(transaction):
    """رابط صفحة الدفع المناسبة للمحاولة مجدداً - يختلف باختلاف نوع المنتج."""
    if transaction.product_type == 'facebook_addon':
        return reverse('payments:facebook_addon_checkout', kwargs={
            'plan_id': transaction.facebook_addon_plan_id, 'wp_site_id': transaction.wp_site_id,
        })
    return reverse('payments:checkout', kwargs={'package_id': transaction.package_id})

def _activate_facebook_addon(transaction):
    """
    يطبّق معاملة إضافة فيسبوك المكتملة مباشرة على الموقع المستهدف: يضبط
    الباقة، يفعّل الخدمة، ويمدّد facebook_addon_trial_ends_at بمدة الفترة
    المدفوعة - مع تراكم الوقت المتبقي (لو نفس الباقة ولسه سارية) بدل ما
    يضيع على العميل لو جدّد قبل الانتهاء.
    """
    site = transaction.wp_site
    if not site or not transaction.facebook_addon_plan:
        return
    now = timezone.now()
    base = now
    if (site.facebook_addon_plan_id == transaction.facebook_addon_plan_id
            and site.facebook_addon_trial_ends_at and site.facebook_addon_trial_ends_at > now):
        base = site.facebook_addon_trial_ends_at
    site.facebook_addon_plan = transaction.facebook_addon_plan
    site.facebook_addon_trial_ends_at = base + timedelta(days=_token_expiry_days(transaction))
    site.social_image_enabled = True
    site.save(update_fields=['facebook_addon_plan', 'facebook_addon_trial_ends_at', 'social_image_enabled'])

def _complete_transaction(transaction, client_name):
    """
    منطق "ماذا يحدث عند وصول الدفعة" المشترك، يُستدعى من كل مسار تأكيد بوابة
    دفع بعد أن يستحوذ (atomically) على حالة pending->completed. لباقات
    النشر: يُصدر WPConnectionToken (يُستبدل لاحقاً بموقع). لإضافة فيسبوك:
    يُفعَّل مباشرة على الموقع المستهدف الموجود بالفعل. يُرجع token الربط
    (أو None لإضافة فيسبوك) ليقرر المستدعي هل يرسل رسالة واتساب بالكود.
    """
    if transaction.product_type == 'facebook_addon':
        _activate_facebook_addon(transaction)
        return None

    token_str = str(uuid.uuid4())
    WPConnectionToken.objects.create(
        token=token_str,
        customer=transaction.customer,
        client_name=client_name,
        package_daily_limit=transaction.package.daily_limit,
        included_facebook_addon_plan=transaction.package.included_facebook_addon_plan,
        expires_at=timezone.now() + timedelta(days=_token_expiry_days(transaction)),
    )
    return token_str

def _get_wallet_number():
    from syndicator.models import AISettings
    try:
        return AISettings.get_settings().wallet_number or getattr(settings, 'WALLET_NUMBER', '')
    except Exception:
        return getattr(settings, 'WALLET_NUMBER', '')

def _find_underpaid_transaction(received_amount, phone_last_digits=None):
    """
    Looks for a pending 'local' wallet transaction that requires MORE than what
    was just received, matched by the sender's phone last digits. Used so an
    underpaid transfer gets an explanatory WhatsApp message instead of just
    sitting pending with no automatic match and no explanation.
    """
    if not phone_last_digits:
        return None
    candidates = Transaction.objects.filter(
        status='pending', gateway='local', amount__gt=received_amount
    ).order_by('created_at')
    for tx in candidates:
        if tx.sender_phone:
            tx_phone = tx.sender_phone.replace('+', '').replace(' ', '')
            if tx_phone.endswith(phone_last_digits):
                return tx
    return None

def packages_view(request):
    packages = SubscriptionPackage.objects.filter(is_active=True).order_by('price')
    has_used_trial = False
    if request.user.is_authenticated:
        try:
            has_used_trial = request.user.customer_profile.has_used_trial
        except Exception:
            pass

    period_prices = {pkg.id: pkg.all_period_prices() for pkg in packages}

    return render(request, 'payments/packages.html', {
        'packages': packages,
        'has_used_trial': has_used_trial,
        'period_prices': period_prices,
    })

@login_required
def start_free_trial(request):
    profile = getattr(request.user, 'customer_profile', None)
    if not profile or not profile.is_whatsapp_verified:
        messages.error(request, "يجب تفعيل حسابك أولاً عن طريق الواتساب.")
        return redirect('accounts:verify_otp')

    if profile.has_used_trial:
        messages.error(request, "لقد قمت باستخدام الفترة التجريبية المجانية بالفعل.")
        return redirect('payments:packages')

    # Get or create the Free Trial Package
    trial_package, created = SubscriptionPackage.objects.get_or_create(
        name="فترة تجريبية 7 أيام",
        defaults={
            'price': Decimal('0.00'),
            'daily_limit': 3,
            'features': "تجميع ونشر تلقائي\nحد أقصى 3 أخبار يومياً\nفترة تجريبية لمدة 7 أيام",
            'is_active': False
        }
    )

    # Set profile trial status
    profile.has_used_trial = True
    profile.save()

    # Create trial transaction
    from django.utils import timezone
    from datetime import timedelta
    
    transaction = Transaction.objects.create(
        customer=profile,
        package=trial_package,
        amount=Decimal('0.00'),
        currency='USD',
        gateway='local',
        status='completed',
        verified_transaction_id=f"TRIAL-{uuid.uuid4().hex[:10].upper()}"
    )

    # Create WP Connection Token with expiration
    WPConnectionToken.objects.create(
        customer=profile,
        client_name=f"{profile.user.first_name or profile.user.username} (Free Trial)",
        package_daily_limit=3,
        expires_at=timezone.now() + timedelta(days=7)
    )

    messages.success(request, "تم تفعيل الفترة التجريبية المجانية بنجاح لمدة 7 أيام! يمكنك استخدام كود الربط الآن.")
    return redirect('payments:payment_success', transaction_id=transaction.transaction_id)


@login_required
def checkout_view(request, package_id):
    profile = getattr(request.user, 'customer_profile', None)
    if not profile or not profile.is_whatsapp_verified:
        messages.error(request, "يجب تفعيل حسابك أولاً.")
        return redirect('accounts:verify_otp')

    package = get_object_or_404(SubscriptionPackage, id=package_id)

    from syndicator.models import AISettings
    ai_settings = AISettings.get_settings()
    enabled_gateways = {
        'paypal': ai_settings.enable_paypal_gateway,
        'paymob': ai_settings.enable_paymob_gateway,
        'local': ai_settings.enable_local_wallet_gateway,
        'crypto': True,
    }

    if request.method == 'POST':
        gateway = request.POST.get('gateway')
        currency = request.POST.get('currency', 'USD')
        billing_period = request.POST.get('billing_period', 'monthly')
        sender_phone = request.POST.get('sender_phone', '').strip()

        if gateway not in dict(Transaction.GATEWAY_CHOICES) or not enabled_gateways.get(gateway):
            messages.error(request, "يرجى اختيار بوابة دفع صحيحة.")
            return redirect('payments:checkout', package_id=package.id)

        if currency not in ['USD', 'EGP']:
            currency = 'USD'

        if billing_period not in dict(SubscriptionPackage.BILLING_PERIOD_CHOICES):
            billing_period = 'monthly'

        amount = package.price_for_period(billing_period, currency)

        if gateway == 'local':
            # Re-submitting checkout for the same package (e.g. after closing the
            # pending page and coming back) previously created a brand new pending
            # transaction each time, leaving duplicate rows that a single wallet
            # transfer could only ever match one of — the rest sat pending forever
            # with no explanation. Reuse the existing pending attempt instead.
            existing = Transaction.objects.filter(
                customer=profile, package=package, billing_period=billing_period,
                currency=currency, gateway='local', status='pending'
            ).order_by('-created_at').first()
            if existing:
                if sender_phone and sender_phone != existing.sender_phone:
                    existing.sender_phone = sender_phone
                    existing.save(update_fields=['sender_phone', 'updated_at'])
                return redirect('payments:checkout_pending', transaction_id=existing.transaction_id)

        # Create pending transaction
        transaction = Transaction.objects.create(
            customer=profile,
            package=package,
            billing_period=billing_period,
            amount=amount,
            currency=currency,
            gateway=gateway,
            sender_phone=sender_phone if gateway == 'local' else None
        )

        if gateway == 'local':
            return redirect('payments:checkout_pending', transaction_id=transaction.transaction_id)
        elif gateway == 'paypal':
            return redirect('payments:paypal_checkout', transaction_id=transaction.transaction_id)
        elif gateway == 'paymob':
            return redirect('payments:paymob_checkout', transaction_id=transaction.transaction_id)

        # For crypto or other simulated successes
        return redirect('payments:payment_success', transaction_id=transaction.transaction_id)

    initial_period = request.GET.get('period', 'monthly')
    if initial_period not in dict(SubscriptionPackage.BILLING_PERIOD_CHOICES):
        initial_period = 'monthly'

    return render(request, 'payments/checkout.html', {
        'package': package,
        'period_prices': package.all_period_prices(),
        'initial_period': initial_period,
        'enable_paypal_gateway': ai_settings.enable_paypal_gateway,
        'enable_paymob_gateway': ai_settings.enable_paymob_gateway,
        'enable_local_wallet_gateway': ai_settings.enable_local_wallet_gateway,
    })

@login_required
def facebook_addon_plans_view(request, wp_site_id):
    """Customer-facing plan picker for one of their sites - lists active
    FacebookAddonPlan rows (mirrors packages_view) and links each to
    facebook_addon_checkout_view for that plan+site pair."""
    profile = getattr(request.user, 'customer_profile', None)
    from syndicator.models import WordPressSite
    owned_site_ids = WPConnectionToken.objects.filter(
        customer=profile, is_used=True, wp_site__isnull=False
    ).values_list('wp_site_id', flat=True)
    site = get_object_or_404(WordPressSite, id=wp_site_id, id__in=owned_site_ids)

    plans = FacebookAddonPlan.objects.filter(is_active=True).order_by('price')
    period_prices = {plan.id: plan.all_period_prices() for plan in plans}

    return render(request, 'payments/facebook_addon_plans.html', {
        'site': site,
        'plans': plans,
        'period_prices': period_prices,
    })


@login_required
def facebook_addon_checkout_view(request, plan_id, wp_site_id):
    """
    Customer self-serve checkout for a standalone Facebook add-on plan on
    one of their own (already-provisioned) sites - mirrors checkout_view
    but targets a FacebookAddonPlan + WordPressSite instead of a
    SubscriptionPackage, sharing the same gateway branching downstream.
    """
    profile = getattr(request.user, 'customer_profile', None)
    if not profile or not profile.is_whatsapp_verified:
        messages.error(request, "يجب تفعيل حسابك أولاً.")
        return redirect('accounts:verify_otp')

    plan = get_object_or_404(FacebookAddonPlan, id=plan_id, is_active=True)

    from syndicator.models import WordPressSite
    owned_site_ids = WPConnectionToken.objects.filter(
        customer=profile, is_used=True, wp_site__isnull=False
    ).values_list('wp_site_id', flat=True)
    site = get_object_or_404(WordPressSite, id=wp_site_id, id__in=owned_site_ids)

    from syndicator.models import AISettings
    ai_settings = AISettings.get_settings()
    enabled_gateways = {
        'paypal': ai_settings.enable_paypal_gateway,
        'paymob': ai_settings.enable_paymob_gateway,
        'local': ai_settings.enable_local_wallet_gateway,
        'crypto': True,
    }

    if request.method == 'POST':
        gateway = request.POST.get('gateway')
        currency = request.POST.get('currency', 'USD')
        billing_period = request.POST.get('billing_period', 'monthly')
        sender_phone = request.POST.get('sender_phone', '').strip()

        if gateway not in dict(Transaction.GATEWAY_CHOICES) or not enabled_gateways.get(gateway):
            messages.error(request, "يرجى اختيار بوابة دفع صحيحة.")
            return redirect('payments:facebook_addon_checkout', plan_id=plan.id, wp_site_id=site.id)

        if currency not in ['USD', 'EGP']:
            currency = 'USD'

        if billing_period not in dict(FacebookAddonPlan.BILLING_PERIOD_CHOICES):
            billing_period = 'monthly'

        amount = plan.price_for_period(billing_period, currency)

        if gateway == 'local':
            existing = Transaction.objects.filter(
                customer=profile, product_type='facebook_addon', facebook_addon_plan=plan, wp_site=site,
                billing_period=billing_period, currency=currency, gateway='local', status='pending'
            ).order_by('-created_at').first()
            if existing:
                if sender_phone and sender_phone != existing.sender_phone:
                    existing.sender_phone = sender_phone
                    existing.save(update_fields=['sender_phone', 'updated_at'])
                return redirect('payments:checkout_pending', transaction_id=existing.transaction_id)

        transaction = Transaction.objects.create(
            customer=profile,
            product_type='facebook_addon',
            facebook_addon_plan=plan,
            wp_site=site,
            billing_period=billing_period,
            amount=amount,
            currency=currency,
            gateway=gateway,
            sender_phone=sender_phone if gateway == 'local' else None
        )

        if gateway == 'local':
            return redirect('payments:checkout_pending', transaction_id=transaction.transaction_id)
        elif gateway == 'paypal':
            return redirect('payments:paypal_checkout', transaction_id=transaction.transaction_id)
        elif gateway == 'paymob':
            return redirect('payments:paymob_checkout', transaction_id=transaction.transaction_id)

        return redirect('payments:payment_success', transaction_id=transaction.transaction_id)

    initial_period = request.GET.get('period', 'monthly')
    if initial_period not in dict(FacebookAddonPlan.BILLING_PERIOD_CHOICES):
        initial_period = 'monthly'

    return render(request, 'payments/facebook_addon_checkout.html', {
        'plan': plan,
        'site': site,
        'period_prices': plan.all_period_prices(),
        'initial_period': initial_period,
        'enable_paypal_gateway': ai_settings.enable_paypal_gateway,
        'enable_paymob_gateway': ai_settings.enable_paymob_gateway,
        'enable_local_wallet_gateway': ai_settings.enable_local_wallet_gateway,
    })


import requests

def get_paypal_access_token():
    client_id = getattr(settings, 'PAYPAL_CLIENT_ID', '')
    client_secret = getattr(settings, 'PAYPAL_CLIENT_SECRET', '')
    mode = getattr(settings, 'PAYPAL_MODE', 'live')
    
    if not client_id or not client_secret:
        return None
        
    base_url = "https://api-m.paypal.com" if mode == 'live' else "https://api-m.sandbox.paypal.com"
    url = f"{base_url}/v1/oauth2/token"
    
    headers = {
        "Accept": "application/json",
        "Accept-Language": "en_US",
    }
    data = {"grant_type": "client_credentials"}
    
    try:
        response = requests.post(
            url,
            auth=(client_id, client_secret),
            headers=headers,
            data=data,
            timeout=15
        )
        if response.status_code == 200:
            return response.json().get('access_token')
    except Exception:
        pass
    return None

@login_required
def paypal_checkout_view(request, transaction_id):
    profile = getattr(request.user, 'customer_profile', None)
    transaction = get_object_or_404(Transaction, transaction_id=transaction_id, customer=profile, status='pending')
    
    context = {
        'transaction': transaction,
        'package': transaction.package,
        'paypal_client_id': getattr(settings, 'PAYPAL_CLIENT_ID', ''),
    }
    return render(request, 'payments/paypal_checkout.html', context)

@csrf_exempt
@login_required
def confirm_paypal_payment(request):
    if request.method != 'POST':
        return JsonResponse({"success": False, "message": "Method not allowed"}, status=405)
        
    try:
        data = json.loads(request.body.decode('utf-8'))
        order_id = data.get("orderID")
        tx_uuid = data.get("transactionID")
    except Exception:
        return JsonResponse({"success": False, "message": "Invalid JSON"}, status=400)
        
    if not order_id or not tx_uuid:
        return JsonResponse({"success": False, "message": "Missing required fields"}, status=400)
        
    profile = getattr(request.user, 'customer_profile', None)
    transaction = get_object_or_404(Transaction, transaction_id=tx_uuid, customer=profile, status='pending')
    
    # Check if Client Secret is configured
    client_secret = getattr(settings, 'PAYPAL_CLIENT_SECRET', '')
    if not client_secret:
        return JsonResponse({
            "success": False, 
            "message": "لم يتم إعداد PAYPAL_CLIENT_SECRET في ملف settings.py لتأكيد العملية تلقائياً. يرجى تواصل الإدارة بالعميل."
        }, status=400)
        
    access_token = get_paypal_access_token()
    if not access_token:
        return JsonResponse({"success": False, "message": "فشل الاتصال بخادم باي بال (Access Token Error)"}, status=500)
        
    mode = getattr(settings, 'PAYPAL_MODE', 'live')
    base_url = "https://api-m.paypal.com" if mode == 'live' else "https://api-m.sandbox.paypal.com"
    url = f"{base_url}/v2/checkout/orders/{order_id}"
    
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {access_token}"
    }
    
    try:
        response = requests.get(url, headers=headers, timeout=15)
        order_details = response.json()
        
        if order_details.get('status') == 'COMPLETED':
            from django.urls import reverse
            success_url = reverse('payments:payment_success', kwargs={'transaction_id': transaction.transaction_id})

            # Atomic claim: only the request that actually flips pending->completed
            # proceeds to issue a token, so a retried/duplicate confirmation call
            # can never generate two WPConnectionToken rows for one payment.
            claimed = Transaction.objects.filter(pk=transaction.pk, status='pending').update(
                status='completed',
                gateway_transaction_id=order_id,
                verified_transaction_id=f"PAYPAL-{order_id}",
            )
            if claimed == 0:
                return JsonResponse({"success": True, "redirect_url": success_url})

            client_name = request.user.first_name or request.user.username
            token_str = _complete_transaction(transaction, client_name)
            if token_str:
                send_payment_success_whatsapp.delay(
                    phone_number=transaction.customer.whatsapp_number,
                    client_name=client_name,
                    package_name=transaction.package.name,
                    token_code=token_str,
                    days=_token_expiry_days(transaction)
                )

            return JsonResponse({"success": True, "redirect_url": success_url})
        else:
            send_payment_failed_whatsapp.delay(
                phone_number=transaction.customer.whatsapp_number,
                client_name=request.user.first_name or request.user.username,
                package_name=_product_name(transaction)
            )
            return JsonResponse({"success": False, "message": f"حالة الطلب غير مكتملة في باي بال: {order_details.get('status')}"}, status=400)
            
    except Exception as e:
        return JsonResponse({"success": False, "message": f"خطأ أثناء التحقق: {str(e)}"}, status=500)


@login_required
def checkout_pending_view(request, transaction_id):
    transaction = get_object_or_404(Transaction, transaction_id=transaction_id, customer=request.user.customer_profile)

    if transaction.status == 'completed':
        return redirect('payments:payment_success', transaction_id=transaction.transaction_id)

    return render(request, 'payments/pending.html', {
        'transaction': transaction,
        'wallet_number': _get_wallet_number()
    })

@csrf_exempt
@login_required
def payment_page_closed_beacon(request, transaction_id):
    """
    Fired via navigator.sendBeacon when a customer navigates away from/closes
    the local-wallet pending checkout page before their transfer was confirmed.
    Reassures them via WhatsApp that the order is still open and will confirm
    automatically, instead of leaving them assuming they need to start over.
    csrf_exempt because sendBeacon cannot attach the CSRF header; ownership is
    still enforced via the authenticated customer_profile lookup below.
    """
    if request.method != 'POST':
        return JsonResponse({"success": False}, status=405)

    try:
        transaction = Transaction.objects.get(
            transaction_id=transaction_id,
            customer=request.user.customer_profile,
            status='pending',
            gateway='local',
        )
    except Transaction.DoesNotExist:
        return JsonResponse({"success": True})

    claimed = Transaction.objects.filter(pk=transaction.pk, page_closed_notified=False).update(page_closed_notified=True)
    if claimed:
        send_page_closed_whatsapp.delay(
            phone_number=transaction.customer.whatsapp_number,
            client_name=transaction.customer.user.first_name or transaction.customer.user.username,
            amount=str(transaction.amount),
            currency=transaction.currency,
            wallet_number=_get_wallet_number(),
        )
    return JsonResponse({"success": True})

@login_required
def check_payment_status_api(request, transaction_id):
    transaction = get_object_or_404(Transaction, transaction_id=transaction_id, customer=request.user.customer_profile)
    return JsonResponse({'status': transaction.status})

@csrf_exempt
def mobile_post_transaction(request):
    """
    Endpoint for syncing transactions from the Kivy mobile app.
    """
    if request.method != 'POST':
        return JsonResponse({"success": False, "message": "Method not allowed"}, status=405)

    try:
        data = json.loads(request.body.decode('utf-8'))
    except Exception:
        return JsonResponse({"success": False, "message": "Invalid JSON payload"}, status=400)

    # Verify authorization
    license_key = data.get("license_key") or request.headers.get("X-License-Key")
    if not license_key:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            license_key = auth_header.split(" ")[1]

    expected_key = getattr(settings, 'WALLET_API_KEY', '')
    if not license_key or license_key != expected_key:
        return JsonResponse({"success": False, "message": "Unauthorized: Invalid API Key"}, status=401)

    tx_id = data.get("transaction_id")
    tx_type = data.get("type")
    amount = data.get("amount")
    counterpart = data.get("counterpart", "")

    if not tx_id or not tx_type or amount is None:
        return JsonResponse({"success": False, "message": "Missing transaction_id, type, or amount"}, status=400)

    if tx_type != 'RECEIVED':
        return JsonResponse({"success": True, "message": "Skipped: Not a received transaction"})

    # Check for replay attack / already matched transaction
    if Transaction.objects.filter(verified_transaction_id=tx_id, status='completed').exists():
        return JsonResponse({"success": False, "message": "Transaction already processed"}, status=400)

    try:
        amount_dec = Decimal(str(amount))
    except Exception:
        return JsonResponse({"success": False, "message": "Invalid amount format"}, status=400)

    # Normalize incoming phone number
    normalized_counterpart = counterpart.replace('+', '').replace(' ', '')
    if normalized_counterpart.startswith('01') and len(normalized_counterpart) == 11:
        normalized_counterpart = '2' + normalized_counterpart
    elif normalized_counterpart.startswith('1') and len(normalized_counterpart) == 10:
        normalized_counterpart = '20' + normalized_counterpart

    # Find pending EGP local wallet transaction
    pending_txs = Transaction.objects.filter(
        amount=amount_dec,
        status='pending',
        gateway='local'
    ).order_by('created_at')

    matched_tx = None
    if normalized_counterpart:
        # Match by phone number if provided
        for tx in pending_txs:
            if tx.sender_phone:
                tx_phone = tx.sender_phone.replace('+', '').replace(' ', '')
                if tx_phone.startswith('01') and len(tx_phone) == 11:
                    tx_phone = '2' + tx_phone
                
                # Check exact match or last 4 digits match
                if tx_phone == normalized_counterpart or normalized_counterpart.endswith(tx_phone[-4:]):
                    matched_tx = tx
                    break
        
        # Fallback to oldest pending transaction without specific phone number
        if not matched_tx:
            matched_tx = pending_txs.filter(models.Q(sender_phone__isnull=True) | models.Q(sender_phone='')).first()
    else:
        matched_tx = pending_txs.first()

    if not matched_tx:
        underpaid_tx = _find_underpaid_transaction(amount_dec, normalized_counterpart[-4:] if normalized_counterpart else None)
        if underpaid_tx:
            send_underpayment_whatsapp.delay(
                phone_number=underpaid_tx.customer.whatsapp_number,
                client_name=underpaid_tx.customer.user.first_name or underpaid_tx.customer.user.username,
                package_name=_product_name(underpaid_tx),
                required_amount=str(underpaid_tx.amount),
                received_amount=str(amount_dec),
                currency=underpaid_tx.currency,
            )
        return JsonResponse({"success": False, "message": "No matching pending transaction found"}, status=200)

    # Atomic claim: only one caller (this endpoint, the Java worker endpoint below,
    # or a retried call) can flip this transaction pending->completed, so the same
    # incoming wallet payment can never mint two WPConnectionToken rows.
    claimed = Transaction.objects.filter(pk=matched_tx.pk, status='pending').update(
        status='completed',
        verified_transaction_id=tx_id,
    )
    if claimed == 0:
        return JsonResponse({"success": False, "message": "Transaction already processed"}, status=400)

    client_name = matched_tx.customer.user.first_name or matched_tx.customer.user.username
    token_str = _complete_transaction(matched_tx, client_name)
    if token_str:
        send_payment_success_whatsapp.delay(
            phone_number=matched_tx.customer.whatsapp_number,
            client_name=client_name,
            package_name=matched_tx.package.name,
            token_code=token_str,
            days=_token_expiry_days(matched_tx)
        )

    return JsonResponse({
        "success": True,
        "message": "Payment verified and Token issued successfully.",
        "details": {
            "transaction_id": matched_tx.transaction_id,
            "status": "completed"
        }
    })

@login_required
def payment_success_view(request, transaction_id):
    transaction = get_object_or_404(Transaction, transaction_id=transaction_id, customer=request.user.customer_profile)
    
    # In a real app, this view is a webhook or return URL that verifies payment status.
    # The atomic claim below guards against this page being loaded/refreshed twice
    # (or racing a background wallet-watcher confirmation) and minting two tokens.
    if transaction.status == 'pending':
        claimed = Transaction.objects.filter(pk=transaction.pk, status='pending').update(status='completed')
        if claimed:
            client_name = request.user.first_name or request.user.username
            token_str = _complete_transaction(transaction, client_name)
            if token_str:
                send_payment_success_whatsapp.delay(
                    phone_number=transaction.customer.whatsapp_number,
                    client_name=client_name,
                    package_name=transaction.package.name,
                    token_code=token_str,
                    days=_token_expiry_days(transaction)
                )

    # Find the user's un-used tokens to display
    tokens = WPConnectionToken.objects.filter(customer=transaction.customer, is_used=False)

    return render(request, 'payments/success.html', {'transaction': transaction, 'tokens': tokens})

@csrf_exempt
def confirm_pairing(request):
    """
    Pairing endpoint for the mobile app: exchanges the pairing token embedded in
    the QR code (pair_qr_view) for the real WALLET_API_KEY. Only a caller who
    actually scanned that QR code (or otherwise knows the token) can complete
    the exchange — this must never hand back the key unconditionally.
    """
    if request.method != 'POST':
        return JsonResponse({"success": False, "message": "Method not allowed"}, status=405)

    try:
        data = json.loads(request.body.decode('utf-8'))
    except Exception:
        return JsonResponse({"success": False, "message": "Invalid JSON"}, status=400)

    pair_token = data.get('pair_token')
    expected_token = getattr(settings, 'PAIRING_TOKEN', '')
    if not expected_token or not pair_token or not hmac.compare_digest(pair_token, expected_token):
        return JsonResponse({"success": False, "message": "Invalid or missing pairing token"}, status=401)

    pairing = DevicePairing.get_singleton()
    pairing.paired = True
    pairing.save(update_fields=['paired', 'updated_at'])

    return JsonResponse({
        "success": True,
        "data": {
            "api_key": getattr(settings, 'WALLET_API_KEY', ''),
            "merchant_name": "enshrly_admin"
        }
    })

def pair_qr_view(request):
    import base64
    import io
    import json
    import qrcode

    server_url = f"{settings.SITE_BASE_URL}/payments/api/v1/payments"
    payload = {
        "pair_token": getattr(settings, 'PAIRING_TOKEN', ''),
        "server_url": server_url
    }
    payload_str = json.dumps(payload)

    qr = qrcode.QRCode(version=1, box_size=10, border=5)
    qr.add_data(payload_str)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    qr_base64 = base64.b64encode(buffer.getvalue()).decode("ascii")

    # Reset pairing state each time the QR is (re)displayed so an old
    # confirmation from a previous scan doesn't show as already-paired.
    pairing = DevicePairing.get_singleton()
    pairing.paired = False
    pairing.save(update_fields=['paired', 'updated_at'])

    return render(request, 'payments/pair.html', {
        'qr_base64': qr_base64,
        'server_url': server_url,
        'payload': payload_str
    })


def pairing_status(request):
    return JsonResponse({'paired': DevicePairing.get_singleton().paired})

@csrf_exempt
def confirm_payment_api(request):
    """
    Endpoint for Java Android worker confirming a payment.
    """
    if request.method != 'POST':
        return JsonResponse({"success": False, "message": "Method not allowed"}, status=405)

    try:
        data = json.loads(request.body.decode('utf-8'))
    except Exception:
        return JsonResponse({"success": False, "message": "Invalid JSON"}, status=400)

    # Verify authorization
    license_key = request.headers.get("X-License-Key")
    if not license_key:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            license_key = auth_header.split(" ")[1]

    expected_key = getattr(settings, 'WALLET_API_KEY', '')
    if not license_key or license_key != expected_key:
        return JsonResponse({"success": False, "message": "Unauthorized: Invalid API Key"}, status=401)

    amount = data.get("amount")
    sender_last4 = data.get("sender_last4", "")
    
    if amount is None or not sender_last4:
        return JsonResponse({"success": False, "message": "Missing required fields"}, status=400)

    try:
        amount_dec = Decimal(str(amount))
    except Exception:
        return JsonResponse({"success": False, "message": "Invalid amount format"}, status=400)

    # Find pending EGP local wallet transaction matching amount
    pending_txs = Transaction.objects.filter(
        amount=amount_dec,
        status='pending',
        gateway='local'
    ).order_by('created_at')

    matched_tx = None
    # Match by last 4 digits
    for tx in pending_txs:
        if tx.sender_phone:
            tx_phone = tx.sender_phone.replace('+', '').replace(' ', '')
            if tx_phone.endswith(sender_last4):
                matched_tx = tx
                break

    if not matched_tx:
        # Fallback to oldest transaction without specific phone number
        matched_tx = pending_txs.filter(models.Q(sender_phone__isnull=True) | models.Q(sender_phone='')).first()

    if not matched_tx:
        underpaid_tx = _find_underpaid_transaction(amount_dec, sender_last4)
        if underpaid_tx:
            send_underpayment_whatsapp.delay(
                phone_number=underpaid_tx.customer.whatsapp_number,
                client_name=underpaid_tx.customer.user.first_name or underpaid_tx.customer.user.username,
                package_name=_product_name(underpaid_tx),
                required_amount=str(underpaid_tx.amount),
                received_amount=str(amount_dec),
                currency=underpaid_tx.currency,
            )
        return JsonResponse({"success": True, "message": "Logged but no matching transaction yet"})

    # Atomic claim — this endpoint (Java worker) and the Kivy wallet-sync endpoint
    # above can both independently match the same incoming payment; only the call
    # that actually flips pending->completed proceeds to issue a token.
    verified_id = f"CONF-JAVA-{uuid.uuid4().hex[:10].upper()}"
    claimed = Transaction.objects.filter(pk=matched_tx.pk, status='pending').update(
        status='completed',
        verified_transaction_id=verified_id,
    )
    if claimed == 0:
        return JsonResponse({"success": True, "message": "Logged but transaction already processed"})

    client_name = matched_tx.customer.user.first_name or matched_tx.customer.user.username
    token_str = _complete_transaction(matched_tx, client_name)
    if token_str:
        send_payment_success_whatsapp.delay(
            phone_number=matched_tx.customer.whatsapp_number,
            client_name=client_name,
            package_name=matched_tx.package.name,
            token_code=token_str,
            days=_token_expiry_days(matched_tx)
        )

    return JsonResponse({
        "success": True,
        "message": "Payment verified and Token issued successfully."
    })


def get_paymob_auth_token():
    api_key = getattr(settings, 'PAYMOB_API_KEY', '')
    if not api_key:
        return None
    url = "https://accept.paymob.com/api/auth/tokens"
    try:
        response = requests.post(url, json={"api_key": api_key}, timeout=15)
        if response.status_code in [200, 201]:
            return response.json().get('token')
    except Exception:
        pass
    return None

def register_paymob_order(auth_token, amount_cents, merchant_order_id):
    url = "https://accept.paymob.com/api/ecommerce/orders"
    payload = {
        "auth_token": auth_token,
        "delivery_needed": "false",
        "amount_cents": str(int(amount_cents)),
        "currency": "EGP",
        "merchant_order_id": str(merchant_order_id),
        "items": []
    }
    try:
        response = requests.post(url, json=payload, timeout=15)
        if response.status_code in [200, 201]:
            return response.json().get('id')
    except Exception:
        pass
    return None

def get_paymob_payment_key(auth_token, amount_cents, order_id, integration_id, customer_user):
    url = "https://accept.paymob.com/api/acceptance/payment_keys"
    billing_data = {
        "apartment": "NA",
        "email": customer_user.email or "customer@sahafihub.com",
        "floor": "NA",
        "first_name": customer_user.first_name or "Client",
        "street": "NA",
        "building": "NA",
        "phone_number": getattr(customer_user.customer_profile, 'whatsapp_number', '01000000000'),
        "shipping_method": "PKG",
        "postal_code": "NA",
        "city": "Cairo",
        "country": "EG",
        "last_name": customer_user.last_name or "Sahafi Hub",
        "state": "Cairo"
    }
    payload = {
        "auth_token": auth_token,
        "amount_cents": str(int(amount_cents)),
        "expiration": 3600,
        "order_id": str(order_id),
        "billing_data": billing_data,
        "currency": "EGP",
        "integration_id": int(integration_id)
    }
    try:
        response = requests.post(url, json=payload, timeout=15)
        if response.status_code in [200, 201]:
            return response.json().get('token')
    except Exception:
        pass
    return None

@login_required
def paymob_checkout_view(request, transaction_id):
    profile = getattr(request.user, 'customer_profile', None)
    transaction = get_object_or_404(Transaction, transaction_id=transaction_id, customer=profile, status='pending')
    
    amount_cents = transaction.amount * 100
    
    auth_token = get_paymob_auth_token()
    if not auth_token:
        messages.error(request, "فشل الاتصال بـ Paymob (Auth Token Error).")
        return redirect(_checkout_retry_url(transaction))

    paymob_order_id = register_paymob_order(auth_token, amount_cents, transaction.transaction_id)
    if not paymob_order_id:
        messages.error(request, "فشل تسجيل المعاملة في Paymob.")
        return redirect(_checkout_retry_url(transaction))

    integration_id = getattr(settings, 'PAYMOB_CARD_INTEGRATION_ID', '5792603')
    payment_key = get_paymob_payment_key(auth_token, amount_cents, paymob_order_id, integration_id, request.user)

    if not payment_key:
        messages.error(request, "فشل توليد مفتاح الدفع لـ Paymob.")
        return redirect(_checkout_retry_url(transaction))
        
    iframe_id = getattr(settings, 'PAYMOB_IFRAME_ID', '150')
    paymob_url = f"https://accept.paymob.com/api/acceptance/iframes/{iframe_id}?payment_token={payment_key}"
    return redirect(paymob_url)

@login_required
def paymob_callback_view(request):
    """
    Browser return URL after the Paymob iframe redirect. This is NOT a trusted
    signal (query params come straight from the user's browser, unsigned) —
    only the signed server-to-server webhook (paymob_webhook_view) is allowed
    to mark a transaction as completed. This view just routes the user to the
    right waiting/success screen based on the transaction's current,
    webhook-verified status, and never trusts request.GET for that decision.
    """
    profile = getattr(request.user, 'customer_profile', None)
    merchant_order_id = request.GET.get('merchant_order_id')

    transaction = None
    if merchant_order_id and profile:
        transaction = Transaction.objects.filter(transaction_id=merchant_order_id, customer=profile).first()

    if not transaction:
        messages.error(request, "لم تكتمل عملية الدفع أو تم إلغاؤها.")
        return redirect('payments:packages')

    if transaction.status == 'completed':
        return redirect('payments:payment_success', transaction_id=transaction.transaction_id)

    # Still pending on our side — wait for the verified webhook to land.
    return redirect('payments:checkout_pending', transaction_id=transaction.transaction_id)

import hmac
import hashlib

@csrf_exempt
def paymob_webhook_view(request):
    if request.method != 'POST':
        return JsonResponse({"success": False, "message": "Method not allowed"}, status=405)
        
    try:
        data = json.loads(request.body.decode('utf-8'))
    except Exception:
        return JsonResponse({"success": False, "message": "Invalid JSON"}, status=400)
        
    hmac_received = request.GET.get('hmac')
    obj = data.get('obj', {})

    hmac_key = getattr(settings, 'PAYMOB_HMAC_KEY', '')
    if not hmac_key:
        # Misconfiguration should never silently downgrade to "trust the payload" —
        # reject instead so payment confirmation never runs unsigned.
        return JsonResponse({"success": False, "message": "Server HMAC key not configured"}, status=500)
    if not hmac_received:
        return JsonResponse({"success": False, "message": "Missing HMAC signature"}, status=401)

    amount_cents = obj.get('amount_cents')
    created_at = obj.get('created_at')
    currency = obj.get('currency')
    error_occured = obj.get('error_occured')
    has_parent_transaction = obj.get('has_parent_transaction')
    obj_id = obj.get('id')
    integration_id = obj.get('integration_id')
    is_3d_secure = obj.get('is_3d_secure')
    is_auth = obj.get('is_auth')
    is_capture = obj.get('is_capture')
    is_voided = obj.get('is_voided')
    is_refunded = obj.get('is_refunded')
    owner = obj.get('owner')
    pending = obj.get('pending')

    source_data = obj.get('source_data', {})
    pan = source_data.get('pan', '')
    sub_type = source_data.get('sub_type', '')
    source_type = source_data.get('type', '')
    success = obj.get('success')

    # Sort and concatenate strings for signature validation
    str_to_sign = (
        f"{amount_cents}{created_at}{currency}{error_occured}"
        f"{has_parent_transaction}{obj_id}{integration_id}{is_3d_secure}"
        f"{is_auth}{is_capture}{is_voided}{is_refunded}{owner}{pending}"
        f"{pan}{sub_type}{source_type}{success}"
    )

    calculated_hmac = hmac.new(
        hmac_key.encode('utf-8'),
        str_to_sign.encode('utf-8'),
        hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(calculated_hmac, hmac_received):
        return JsonResponse({"success": False, "message": "Invalid HMAC signature"}, status=401)

    order = obj.get('order', {})
    merchant_order_id = order.get('merchant_order_id')
    paymob_tx_id = obj.get('id')

    if success is True and merchant_order_id:
        try:
            transaction = Transaction.objects.get(transaction_id=merchant_order_id, status='pending')

            # Paymob may redeliver the same webhook (retry on slow/failed response).
            # This atomic claim ensures only the delivery that actually flips the
            # status proceeds to issue a token — a redelivered webhook is a no-op.
            claimed = Transaction.objects.filter(pk=transaction.pk, status='pending').update(
                status='completed',
                gateway_transaction_id=paymob_tx_id,
                verified_transaction_id=f"PAYMOB-{paymob_tx_id}",
            )
            if claimed:
                client_name = transaction.customer.user.first_name or transaction.customer.user.username
                token_str = _complete_transaction(transaction, client_name)
                if token_str:
                    send_payment_success_whatsapp.delay(
                        phone_number=transaction.customer.whatsapp_number,
                        client_name=client_name,
                        package_name=transaction.package.name,
                        token_code=token_str,
                        days=_token_expiry_days(transaction)
                    )
        except Transaction.DoesNotExist:
            pass
    elif success is False and merchant_order_id:
        try:
            transaction = Transaction.objects.get(transaction_id=merchant_order_id, status='pending')
            transaction.status = 'failed'
            transaction.gateway_transaction_id = paymob_tx_id
            transaction.save()

            send_payment_failed_whatsapp.delay(
                phone_number=transaction.customer.whatsapp_number,
                client_name=transaction.customer.user.first_name or transaction.customer.user.username,
                package_name=_product_name(transaction)
            )
        except Transaction.DoesNotExist:
            pass

    return JsonResponse({"success": True})





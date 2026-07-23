from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.conf import settings
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.db import models
from .models import SubscriptionPackage, Transaction
from syndicator.models import WPConnectionToken
import uuid
import json
from decimal import Decimal
from django.utils import timezone
from datetime import timedelta
from accounts.utils import send_whatsapp_payment_success

def packages_view(request):
    packages = SubscriptionPackage.objects.filter(is_active=True).order_by('price')
    has_used_trial = False
    if request.user.is_authenticated:
        try:
            has_used_trial = request.user.customer_profile.has_used_trial
        except Exception:
            pass
    return render(request, 'payments/packages.html', {'packages': packages, 'has_used_trial': has_used_trial})

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

    if request.method == 'POST':
        gateway = request.POST.get('gateway')
        currency = request.POST.get('currency', 'USD')
        sender_phone = request.POST.get('sender_phone', '').strip()
        
        if gateway not in dict(Transaction.GATEWAY_CHOICES):
            messages.error(request, "يرجى اختيار بوابة دفع صحيحة.")
            return redirect('payments:checkout', package_id=package.id)
        
        if currency not in ['USD', 'EGP']:
            currency = 'USD'
            
        amount = package.price if currency == 'USD' else package.price_egp

        # Create pending transaction
        transaction = Transaction.objects.create(
            customer=profile,
            package=package,
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

    return render(request, 'payments/checkout.html', {'package': package})

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
            transaction.status = 'completed'
            transaction.gateway_transaction_id = order_id
            transaction.verified_transaction_id = f"PAYPAL-{order_id}"
            transaction.save()
            
            # Generate Token
            token_str = str(uuid.uuid4())
            WPConnectionToken.objects.create(
                token=token_str,
                client_name=request.user.first_name or request.user.username,
                package_daily_limit=transaction.package.daily_limit,
                expires_at=timezone.now() + timedelta(days=30),
            )
            
            # Send WhatsApp confirmation
            send_whatsapp_payment_success(
                phone_number=transaction.customer.whatsapp_number,
                client_name=request.user.first_name or request.user.username,
                package_name=transaction.package.name,
                token_code=token_str
            )
            
            from django.urls import reverse
            success_url = reverse('payments:payment_success', kwargs={'transaction_id': transaction.transaction_id})
            return JsonResponse({"success": True, "redirect_url": success_url})
        else:
            return JsonResponse({"success": False, "message": f"حالة الطلب غير مكتملة في باي بال: {order_details.get('status')}"}, status=400)
            
    except Exception as e:
        return JsonResponse({"success": False, "message": f"خطأ أثناء التحقق: {str(e)}"}, status=500)


@login_required
def checkout_pending_view(request, transaction_id):
    transaction = get_object_or_404(Transaction, transaction_id=transaction_id, customer=request.user.customer_profile)
    from syndicator.models import AISettings
    try:
        wallet_number = AISettings.get_settings().wallet_number or getattr(settings, 'WALLET_NUMBER', '')
    except Exception:
        wallet_number = getattr(settings, 'WALLET_NUMBER', '')
    
    if transaction.status == 'completed':
        return redirect('payments:payment_success', transaction_id=transaction.transaction_id)
        
    return render(request, 'payments/pending.html', {
        'transaction': transaction,
        'wallet_number': wallet_number
    })

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
        return JsonResponse({"success": False, "message": "No matching pending transaction found"}, status=200)

    # Match and mark successful
    matched_tx.status = 'completed'
    matched_tx.verified_transaction_id = tx_id
    matched_tx.save()

    # Generate token
    token_str = str(uuid.uuid4())
    WPConnectionToken.objects.create(
        token=token_str,
        client_name=matched_tx.customer.user.first_name,
        package_daily_limit=matched_tx.package.daily_limit,
        expires_at=timezone.now() + timedelta(days=30),
    )
    
    # Send WhatsApp confirmation
    send_whatsapp_payment_success(
        phone_number=matched_tx.customer.whatsapp_number,
        client_name=matched_tx.customer.user.first_name or matched_tx.customer.user.username,
        package_name=matched_tx.package.name,
        token_code=token_str
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
            expires_at=timezone.now() + timedelta(days=30),
        )
        
        # Send WhatsApp confirmation
        send_whatsapp_payment_success(
            phone_number=transaction.customer.whatsapp_number,
            client_name=request.user.first_name or request.user.username,
            package_name=transaction.package.name,
            token_code=token_str
        ) 

    # Find the user's un-used tokens to display
    tokens = WPConnectionToken.objects.filter(client_name=request.user.first_name, is_used=False)

    return render(request, 'payments/success.html', {'transaction': transaction, 'tokens': tokens})

@csrf_exempt
def confirm_pairing(request):
    """
    Mock pairing endpoint for the mobile app QR code login.
    """
    return JsonResponse({
        "success": True,
        "data": {
            "api_key": getattr(settings, 'WALLET_API_KEY', 'enshrly_wallet_secret_token_2026'),
            "secret_key": "dummy_secret",
            "merchant_name": "enshrly_admin"
        }
    })

def pair_qr_view(request):
    import base64
    import io
    import json
    import qrcode
    import socket
    
    # Get local IP dynamically
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('8.8.8.8', 80))
        local_ip = s.getsockname()[0]
    except Exception:
        local_ip = '127.0.0.1'
    finally:
        s.close()
        
    server_url = f"http://{local_ip}:8000/payments/api/v1/payments"
    payload = {
        "pair_token": "enshrly_pairing_token_2026",
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
    
    return render(request, 'payments/pair.html', {
        'qr_base64': qr_base64,
        'server_url': server_url,
        'payload': payload_str
    })

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
        return JsonResponse({"success": True, "message": "Logged but no matching transaction yet"})

    # Mark completed
    matched_tx.status = 'completed'
    matched_tx.verified_transaction_id = f"CONF-JAVA-{uuid.uuid4().hex[:10].upper()}"
    matched_tx.save()

    # Generate token
    token_str = str(uuid.uuid4())
    WPConnectionToken.objects.create(
        token=token_str,
        client_name=matched_tx.customer.user.first_name,
        package_daily_limit=matched_tx.package.daily_limit,
        expires_at=timezone.now() + timedelta(days=30),
    )
    
    # Send WhatsApp confirmation
    send_whatsapp_payment_success(
        phone_number=matched_tx.customer.whatsapp_number,
        client_name=matched_tx.customer.user.first_name or matched_tx.customer.user.username,
        package_name=matched_tx.package.name,
        token_code=token_str
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
        "email": customer_user.email or "customer@enshrly.com",
        "floor": "NA",
        "first_name": customer_user.first_name or "Client",
        "street": "NA",
        "building": "NA",
        "phone_number": getattr(customer_user.customer_profile, 'whatsapp_number', '01000000000'),
        "shipping_method": "PKG",
        "postal_code": "NA",
        "city": "Cairo",
        "country": "EG",
        "last_name": customer_user.last_name or "Enshrly",
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
        return redirect('payments:checkout', package_id=transaction.package.id)
        
    paymob_order_id = register_paymob_order(auth_token, amount_cents, transaction.transaction_id)
    if not paymob_order_id:
        messages.error(request, "فشل تسجيل المعاملة في Paymob.")
        return redirect('payments:checkout', package_id=transaction.package.id)
        
    integration_id = getattr(settings, 'PAYMOB_CARD_INTEGRATION_ID', '5792603')
    payment_key = get_paymob_payment_key(auth_token, amount_cents, paymob_order_id, integration_id, request.user)
    
    if not payment_key:
        messages.error(request, "فشل توليد مفتاح الدفع لـ Paymob.")
        return redirect('payments:checkout', package_id=transaction.package.id)
        
    iframe_id = getattr(settings, 'PAYMOB_IFRAME_ID', '150')
    paymob_url = f"https://accept.paymob.com/api/acceptance/iframes/{iframe_id}?payment_token={payment_key}"
    return redirect(paymob_url)

@login_required
def paymob_callback_view(request):
    success = request.GET.get('success')
    merchant_order_id = request.GET.get('merchant_order_id')
    paymob_tx_id = request.GET.get('id')
    
    if success == 'true' and merchant_order_id:
        try:
            transaction = Transaction.objects.get(transaction_id=merchant_order_id, status='pending')
            transaction.status = 'completed'
            transaction.gateway_transaction_id = paymob_tx_id
            transaction.verified_transaction_id = f"PAYMOB-{paymob_tx_id}"
            transaction.save()
            
            token_str = str(uuid.uuid4())
            WPConnectionToken.objects.create(
                token=token_str,
                client_name=transaction.customer.user.first_name or transaction.customer.user.username,
                package_daily_limit=transaction.package.daily_limit,
                expires_at=timezone.now() + timedelta(days=30),
            )
            
            send_whatsapp_payment_success(
                phone_number=transaction.customer.whatsapp_number,
                client_name=transaction.customer.user.first_name or transaction.customer.user.username,
                package_name=transaction.package.name,
                token_code=token_str
            )
            
            return redirect('payments:payment_success', transaction_id=transaction.transaction_id)
        except Transaction.DoesNotExist:
            try:
                transaction = Transaction.objects.get(transaction_id=merchant_order_id, status='completed')
                return redirect('payments:payment_success', transaction_id=transaction.transaction_id)
            except Transaction.DoesNotExist:
                pass
                
    messages.error(request, "لم تكتمل عملية الدفع أو تم إلغاؤها.")
    return redirect('payments:packages')

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
    if hmac_key and hmac_received:
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
        
        if calculated_hmac != hmac_received:
            return JsonResponse({"success": False, "message": "Invalid HMAC signature"}, status=401)
            
    success = obj.get('success')
    order = obj.get('order', {})
    merchant_order_id = order.get('merchant_order_id')
    paymob_tx_id = obj.get('id')
    
    if success is True and merchant_order_id:
        try:
            transaction = Transaction.objects.get(transaction_id=merchant_order_id, status='pending')
            transaction.status = 'completed'
            transaction.gateway_transaction_id = paymob_tx_id
            transaction.verified_transaction_id = f"PAYMOB-{paymob_tx_id}"
            transaction.save()
            
            token_str = str(uuid.uuid4())
            WPConnectionToken.objects.create(
                token=token_str,
                client_name=transaction.customer.user.first_name or transaction.customer.user.username,
                package_daily_limit=transaction.package.daily_limit,
                expires_at=timezone.now() + timedelta(days=30),
            )
            
            send_whatsapp_payment_success(
                phone_number=transaction.customer.whatsapp_number,
                client_name=transaction.customer.user.first_name or transaction.customer.user.username,
                package_name=transaction.package.name,
                token_code=token_str
            )
        except Transaction.DoesNotExist:
            pass
            
    return JsonResponse({"success": True})





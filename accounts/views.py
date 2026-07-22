from django.shortcuts import render, redirect
from django.contrib.auth.models import User
from django.contrib.auth import login, authenticate
from django.contrib import messages
from .models import CustomerProfile, WhatsAppOTP
from .utils import send_whatsapp_otp
from django.contrib.auth.decorators import login_required

def signup_view(request):
    if request.method == 'POST':
        name = request.POST.get('name')
        whatsapp = request.POST.get('whatsapp')
        password = request.POST.get('password')

        if User.objects.filter(username=whatsapp).exists():
            messages.error(request, "رقم الواتساب مسجل مسبقاً.")
            return redirect('accounts:signup')

        user = User.objects.create_user(username=whatsapp, password=password, first_name=name)
        profile = CustomerProfile.objects.create(user=user, whatsapp_number=whatsapp)

        # Generate and send OTP
        otp = WhatsAppOTP.objects.create(customer=profile)
        send_whatsapp_otp(whatsapp, otp.otp_code)

        # Log the user in to continue to verification
        login(request, user)
        return redirect('accounts:verify_otp')

    return render(request, 'accounts/signup.html')

@login_required
def verify_otp_view(request):
    if not hasattr(request.user, 'customer_profile'):
        if request.user.is_staff:
            return redirect('payments:packages')
        profile = CustomerProfile.objects.create(user=request.user, whatsapp_number=request.user.username)
        otp = WhatsAppOTP.objects.create(customer=profile)
        send_whatsapp_otp(profile.whatsapp_number, otp.otp_code)
    else:
        profile = request.user.customer_profile

    if profile.is_whatsapp_verified:
        return redirect('accounts:dashboard')

    if request.method == 'POST':
        code = request.POST.get('otp_code')
        otp = profile.otps.filter(otp_code=code, is_used=False).last()

        if otp and otp.is_valid():
            otp.is_used = True
            otp.save()
            profile.is_whatsapp_verified = True
            profile.save()
            messages.success(request, "تم تفعيل الحساب بنجاح!")
            return redirect('accounts:dashboard')
        else:
            messages.error(request, "الكود غير صحيح أو منتهي الصلاحية.")

    return render(request, 'accounts/verify_otp.html')

def login_view(request):
    if request.method == 'POST':
        whatsapp = request.POST.get('whatsapp')
        password = request.POST.get('password')
        user = authenticate(request, username=whatsapp, password=password)
        if user is not None:
            login(request, user)
            profile = getattr(user, 'customer_profile', None)
            if profile and not profile.is_whatsapp_verified:
                return redirect('accounts:verify_otp')
            return redirect('accounts:dashboard')
        else:
            messages.error(request, "رقم الواتساب أو كلمة المرور غير صحيحة.")

    return render(request, 'accounts/login.html')

from syndicator.models import WPConnectionToken

@login_required
def dashboard_view(request):
    profile = getattr(request.user, 'customer_profile', None)
    
    # Get user's tokens
    tokens = WPConnectionToken.objects.filter(client_name=request.user.first_name).order_by('-created_at')
    
    context = {
        'tokens': tokens,
        'profile': profile,
    }
    return render(request, 'accounts/dashboard.html', context)

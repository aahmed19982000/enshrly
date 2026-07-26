from django.shortcuts import render, redirect
from django.contrib.auth.models import User
from django.contrib.auth import login, authenticate
from django.contrib import messages
from .models import CustomerProfile, WhatsAppOTP, PasswordResetOTP
from .utils import send_otp_email, send_whatsapp_welcome, get_client_ip, check_rate_limit
from django.contrib.auth.decorators import login_required

def signup_view(request):
    if request.method == 'POST':
        name = request.POST.get('name')
        whatsapp = request.POST.get('whatsapp')
        email = request.POST.get('email')
        password = request.POST.get('password')

        # Throttle signups: caps how many OTP emails one visitor can trigger
        # (per IP) and how many times one phone number can be targeted (per
        # number) — signup is unauthenticated and otherwise lets anyone
        # send unsolicited "OTP" emails to any address for free.
        client_ip = get_client_ip(request)
        if check_rate_limit(f'signup:ip:{client_ip}', limit=5, window_seconds=3600):
            messages.error(request, "عدد محاولات كبير جداً من جهازك، حاول مرة أخرى بعد قليل.")
            return redirect('accounts:signup')
        if whatsapp and check_rate_limit(f'signup:phone:{whatsapp}', limit=3, window_seconds=3600):
            messages.error(request, "تم إرسال عدد كافٍ من رسائل التفعيل لهذا الرقم، حاول مرة أخرى لاحقاً.")
            return redirect('accounts:signup')

        user_qs = User.objects.filter(username=whatsapp)
        if user_qs.exists():
            user = user_qs.first()
            profile = getattr(user, 'customer_profile', None)
            if profile and profile.is_whatsapp_verified:
                messages.error(request, "رقم الواتساب مسجل ومفعل مسبقاً، يرجى تسجيل الدخول.")
                return redirect('accounts:signup')
            else:
                # User exists but not verified. Update info and resend OTP.
                user.set_password(password)
                user.first_name = name
                user.email = email
                user.save()

                if not profile:
                    profile = CustomerProfile.objects.create(user=user, whatsapp_number=whatsapp)

                # Generate and send OTP
                otp = WhatsAppOTP.objects.create(customer=profile)
                send_otp_email(email, otp.otp_code)
                
                # Log the user in to continue to verification
                login(request, user, backend='django.contrib.auth.backends.ModelBackend')
                return redirect('accounts:verify_otp')

        user = User.objects.create_user(username=whatsapp, password=password, first_name=name, email=email)
        profile = CustomerProfile.objects.create(user=user, whatsapp_number=whatsapp)
        send_whatsapp_welcome(whatsapp, name)

        # Generate and send OTP
        otp = WhatsAppOTP.objects.create(customer=profile)
        send_otp_email(email, otp.otp_code)

        # Log the user in to continue to verification
        login(request, user, backend='django.contrib.auth.backends.ModelBackend')
        return redirect('accounts:verify_otp')

    return render(request, 'accounts/signup.html')

@login_required
def verify_otp_view(request):
    if not hasattr(request.user, 'customer_profile'):
        if request.user.is_staff:
            return redirect('payments:packages')
        profile = CustomerProfile.objects.create(user=request.user, whatsapp_number=request.user.username)
        otp = WhatsAppOTP.objects.create(customer=profile)
        send_otp_email(request.user.email, otp.otp_code)
    else:
        profile = request.user.customer_profile

    if profile.is_whatsapp_verified:
        return redirect('accounts:dashboard')

    if request.method == 'POST':
        # A 4-digit code is only 10,000 combinations — without this, it's
        # brute-forceable well within its 10-minute validity window.
        if check_rate_limit(f'otp_verify:{profile.id}', limit=5, window_seconds=600):
            messages.error(request, "تجاوزت عدد المحاولات المسموح، يرجى الانتظار قليلاً ثم إعادة المحاولة.")
            return render(request, 'accounts/verify_otp.html')

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

@login_required
def resend_otp_view(request):
    if request.method != 'POST' or not hasattr(request.user, 'customer_profile'):
        return redirect('accounts:verify_otp')

    profile = request.user.customer_profile
    if profile.is_whatsapp_verified:
        return redirect('accounts:dashboard')

    # Cap resends per profile — otherwise this endpoint lets a logged-in user
    # spam themselves (or anyone whose email they know) with free emails.
    if check_rate_limit(f'otp_resend:{profile.id}', limit=3, window_seconds=600):
        messages.error(request, "لقد تجاوزت عدد مرات إعادة الإرسال المسموحة، يرجى الانتظار قليلاً.")
        return redirect('accounts:verify_otp')

    otp = WhatsAppOTP.objects.create(customer=profile)
    send_otp_email(profile.user.email, otp.otp_code)
    messages.success(request, "تم إرسال كود جديد إلى بريدك الإلكتروني.")
    return redirect('accounts:verify_otp')

def login_view(request):
    if request.method == 'POST':
        whatsapp = request.POST.get('whatsapp')
        password = request.POST.get('password')

        # Throttle password guessing per (IP, phone number) pair — a wrong
        # password only counts against this specific target, so it can't be
        # used to lock a victim out by spamming failed attempts against them.
        client_ip = get_client_ip(request)
        if check_rate_limit(f'login:{client_ip}:{whatsapp}', limit=5, window_seconds=600):
            messages.error(request, "عدد محاولات كبير جداً، يرجى الانتظار قليلاً ثم إعادة المحاولة.")
            return render(request, 'accounts/login.html')

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

def forgot_password_view(request):
    if request.method == 'POST':
        email = request.POST.get('email')
        client_ip = get_client_ip(request)

        # Throttled the same way as signup — this endpoint is unauthenticated.
        if check_rate_limit(f'forgot_pw:ip:{client_ip}', limit=5, window_seconds=3600):
            messages.error(request, "عدد محاولات كبير جداً من جهازك، حاول مرة أخرى بعد قليل.")
            return redirect('accounts:forgot_password')

        user = User.objects.filter(email=email).first()
        if user and hasattr(user, 'customer_profile'):
            if check_rate_limit(f'forgot_pw:email:{email}', limit=3, window_seconds=3600):
                messages.error(request, "تم إرسال عدد كافٍ من الأكواد لهذا البريد، حاول مرة أخرى لاحقاً.")
                return redirect('accounts:forgot_password')
            otp = PasswordResetOTP.objects.create(customer=user.customer_profile)
            send_otp_email(email, otp.otp_code)

        request.session['reset_email'] = email
        # Same message whether or not the email is registered — otherwise this
        # endpoint lets anyone check which emails have accounts.
        messages.success(request, "لو البريد ده مسجل عندنا، هيوصلك كود لإعادة تعيين كلمة السر.")
        return redirect('accounts:reset_password')

    return render(request, 'accounts/forgot_password.html')

def reset_password_view(request):
    email = request.session.get('reset_email')
    if not email:
        return redirect('accounts:forgot_password')

    if request.method == 'POST':
        code = request.POST.get('otp_code')
        new_password = request.POST.get('new_password')

        user = User.objects.filter(email=email).first()
        otp = None
        if user and hasattr(user, 'customer_profile'):
            otp = user.customer_profile.password_reset_otps.filter(otp_code=code, is_used=False).last()

        if otp and otp.is_valid():
            otp.is_used = True
            otp.save()
            user.set_password(new_password)
            user.save()
            del request.session['reset_email']
            messages.success(request, "تم تغيير كلمة السر بنجاح، يمكنك تسجيل الدخول الآن.")
            return redirect('accounts:login')
        else:
            messages.error(request, "الكود غير صحيح أو منتهي الصلاحية.")

    return render(request, 'accounts/reset_password.html')

from syndicator.models import WPConnectionToken, AIImportLog
from django.utils import timezone

@login_required
def dashboard_view(request):
    profile = getattr(request.user, 'customer_profile', None)
    
    # Get user's tokens (ownership is via the customer FK, not client_name — two
    # customers can share a first name, and client_name is a display label only)
    tokens = WPConnectionToken.objects.filter(customer=profile).order_by('-created_at')
    
    # Extract connected sites and calculate stats
    sites_stats = []
    total_published_count = 0
    total_failed_count = 0
    total_today_count = 0
    
    today = timezone.now().date()
    
    now = timezone.now()
    
    # Annotate tokens with is_expired flag
    for token in tokens:
        token.is_expired = token.expires_at is not None and token.expires_at < now
    
    for token in tokens:
        if token.is_used and token.wp_site:
            site = token.wp_site
            
            # Calculate stats for this site
            success_count = AIImportLog.objects.filter(wp_site=site, status='success').count()
            failed_count = AIImportLog.objects.filter(wp_site=site, status='failed').count()
            today_count = AIImportLog.objects.filter(wp_site=site, status='success', created_at__date=today).count()
            
            total_count = success_count + failed_count
            success_rate = round((success_count / total_count) * 100, 1) if total_count > 0 else 100.0
            
            # Get latest 5 articles
            latest_logs = AIImportLog.objects.filter(
                wp_site=site, status='success'
            ).exclude(published_url='').select_related('article').order_by('-created_at')[:5]
            
            latest_articles = []
            for log in latest_logs:
                latest_articles.append({
                    'title': log.title or (log.article.title if log.article else "مقال بدون عنوان"),
                    'url': log.published_url,
                    'created_at': log.created_at,
                })
            
            # Daily limit: prefer wp_site.daily_limit, fallback to token.package_daily_limit
            daily_limit = site.daily_limit or token.package_daily_limit
            
            sites_stats.append({
                'site': site,
                'token': token,
                'success_count': success_count,
                'failed_count': failed_count,
                'today_count': today_count,
                'daily_limit': daily_limit,
                'success_rate': success_rate,
                'latest_articles': latest_articles,
                'today_progress_percent': min(int((today_count / daily_limit) * 100), 100) if daily_limit > 0 else 0,
                'is_expired': token.is_expired,
            })
            
            total_published_count += success_count
            total_failed_count += failed_count
            total_today_count += today_count
            
    context = {
        'tokens': tokens,
        'profile': profile,
        'sites_stats': sites_stats,
        'summary_stats': {
            'total_sites': len(sites_stats),
            'total_published': total_published_count,
            'total_failed': total_failed_count,
            'total_today': total_today_count,
        }
    }
    return render(request, 'accounts/dashboard.html', context)


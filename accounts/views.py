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

from syndicator.models import WPConnectionToken, AIImportLog
from django.utils import timezone

@login_required
def dashboard_view(request):
    profile = getattr(request.user, 'customer_profile', None)
    
    # Get user's tokens
    tokens = WPConnectionToken.objects.filter(client_name=request.user.first_name).order_by('-created_at')
    
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


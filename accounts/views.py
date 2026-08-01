from django.shortcuts import render, redirect
from django.contrib.auth.models import User
from django.contrib.auth import login, authenticate
from django.contrib import messages
from django.utils.http import url_has_allowed_host_and_scheme
from .models import CustomerProfile, WhatsAppOTP, PasswordResetOTP
from .utils import send_otp_email, send_whatsapp_welcome, get_client_ip, check_rate_limit
from django.contrib.auth.decorators import login_required

def _stash_post_auth_redirect(request):
    """
    Remembers a `?next=` target (e.g. a Facebook add-on standalone checkout
    page) across the signup->OTP or login->OTP hops, so the customer lands
    back where they meant to go instead of always at the generic dashboard.
    Only overwrites the stashed value when `next` is actually present on
    this request, so it survives the follow-up POST (which re-submits to a
    bare URL with no query string) after being captured on the initial GET.
    """
    next_url = request.GET.get('next') or request.POST.get('next')
    if next_url and url_has_allowed_host_and_scheme(
        next_url, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        request.session['post_verify_redirect'] = next_url

def signup_view(request):
    _stash_post_auth_redirect(request)
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
    _stash_post_auth_redirect(request)
    if not hasattr(request.user, 'customer_profile'):
        if request.user.is_staff:
            return redirect('payments:packages')
        profile = CustomerProfile.objects.create(user=request.user, whatsapp_number=request.user.username)
        otp = WhatsAppOTP.objects.create(customer=profile)
        send_otp_email(request.user.email, otp.otp_code)
    else:
        profile = request.user.customer_profile

    if profile.is_whatsapp_verified:
        return redirect(request.session.pop('post_verify_redirect', None) or 'accounts:dashboard')

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
            return redirect(request.session.pop('post_verify_redirect', None) or 'accounts:dashboard')
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
    _stash_post_auth_redirect(request)
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
            return redirect(request.session.pop('post_verify_redirect', None) or 'accounts:dashboard')
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


from django.http import Http404
from django.shortcuts import get_object_or_404
from django.urls import reverse
from syndicator.models import WordPressSite, SocialSharePost


def _customer_site_ids(profile):
    """Sites the logged-in customer actually owns, via their used connection
    tokens — mirrors dashboard_view's ownership check (no direct customer FK
    on WordPressSite)."""
    return WPConnectionToken.objects.filter(
        customer=profile, is_used=True, wp_site__isnull=False
    ).values_list('wp_site_id', flat=True)


@login_required
def facebook_dashboard_view(request):
    profile = getattr(request.user, 'customer_profile', None)
    sites = WordPressSite.objects.filter(id__in=_customer_site_ids(profile))

    sites_data = []
    for site in sites:
        sites_data.append({
            'site': site,
            'is_connected': site.facebook_auto_publish_enabled,
            'addon_active': site.facebook_addon_is_active,
            'posts': SocialSharePost.objects.filter(wp_site=site).order_by('-created_at')[:20],
        })

    # Standalone Facebook-addon tokens (package_daily_limit=0 marks them as
    # such - see payments/views.py::_complete_transaction) bought but not
    # linked to a site yet, so the empty state can point the customer at
    # "go connect it" instead of "go buy a package" when one exists.
    pending_standalone_tokens = WPConnectionToken.objects.filter(
        customer=profile, is_used=False, package_daily_limit=0,
        included_facebook_addon_plan__isnull=False,
    )

    return render(request, 'accounts/facebook_dashboard.html', {
        'sites_data': sites_data,
        'pending_standalone_tokens': pending_standalone_tokens,
    })


@login_required
def social_design_edit_view(request, wp_site_id):
    """
    Self-serve editor for the customer's own social-card branding
    (template, badge text, logo, logo position, colors) - deliberately
    excludes billing fields (social_image_enabled, trial date) and the
    Facebook page connection, which stay staff/self-connect-link controlled.
    """
    import re

    profile = getattr(request.user, 'customer_profile', None)
    if wp_site_id not in _customer_site_ids(profile):
        raise Http404
    site = get_object_or_404(WordPressSite, pk=wp_site_id)

    if request.method == 'POST':
        template_choices = dict(WordPressSite.SOCIAL_TEMPLATE_CHOICES)
        position_choices = dict(WordPressSite.SOCIAL_LOGO_POSITION_CHOICES)

        template = request.POST.get('social_template', '')
        if template in template_choices:
            site.social_template = template

        position = request.POST.get('social_logo_position', '')
        if position in position_choices:
            site.social_logo_position = position

        site.social_badge_text = request.POST.get('social_badge_text', '').strip()[:30]

        for field in ('social_primary_color', 'social_secondary_color'):
            value = request.POST.get(field, '')
            if re.match(r'^#[0-9A-Fa-f]{6}$', value):
                setattr(site, field, value)

        if request.FILES.get('social_logo'):
            site.social_logo = request.FILES['social_logo']

        site.save()
        messages.success(request, "تم حفظ تصميم صورة السوشال ميديا بنجاح.")
        return redirect('accounts:social_design_edit', wp_site_id=site.pk)

    return render(request, 'accounts/social_design_form.html', {
        'site': site,
        'template_choices': WordPressSite.SOCIAL_TEMPLATE_CHOICES,
        'position_choices': WordPressSite.SOCIAL_LOGO_POSITION_CHOICES,
    })


@login_required
def facebook_connect_redirect_view(request, wp_site_id):
    profile = getattr(request.user, 'customer_profile', None)
    if wp_site_id not in _customer_site_ids(profile):
        raise Http404
    site = get_object_or_404(WordPressSite, pk=wp_site_id)

    if not site.facebook_addon_is_active:
        messages.error(request, "خدمة النشر التلقائي على فيسبوك غير مفعّلة لهذا الموقع.")
        return redirect('accounts:facebook_dashboard')

    from syndicator.views_facebook_connect import make_facebook_connect_token
    token = make_facebook_connect_token(site.pk)
    return redirect(reverse('news_ai:facebook_connect_start', kwargs={'token': token}))


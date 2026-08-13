"""
Self-serve Facebook Page connect flow for the Facebook auto-posting add-on.

Staff generates a per-site link (make_facebook_connect_token, called from
WordPressSiteUpdateView) and sends it to the customer. The customer clicks
through Facebook's OAuth dialog themselves - no manual token copy/paste,
and staff never sees the customer's Facebook credentials.

Requires FACEBOOK_APP_ID/FACEBOOK_APP_SECRET to be configured (a Meta app
registered at developers.facebook.com) and, for pages the developer account
doesn't itself manage, that app to have passed Meta's App Review for the
pages_show_list / pages_read_engagement / pages_manage_posts permissions.
"""
import logging

import requests
from django.conf import settings
from django.core import signing
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.http import require_POST

from .models import WordPressSite

logger = logging.getLogger(__name__)

GRAPH_VERSION = settings.FACEBOOK_GRAPH_VERSION
TOKEN_SALT = 'facebook_connect'
TOKEN_MAX_AGE = 60 * 60 * 24 * 30  # 30 days, matches the copy already shown in wp_site_form.html


def make_facebook_connect_token(wp_site_pk):
    return signing.dumps(wp_site_pk, salt=TOKEN_SALT)


def _resolve_wp_site(token):
    """Returns (wp_site, None) or (None, error_message)."""
    try:
        wp_site_id = signing.loads(token, salt=TOKEN_SALT, max_age=TOKEN_MAX_AGE)
        return WordPressSite.objects.get(pk=wp_site_id), None
    except signing.SignatureExpired:
        return None, 'انتهت صلاحية رابط الربط (صالح لمدة 30 يوماً). يرجى طلب رابط جديد.'
    except (signing.BadSignature, WordPressSite.DoesNotExist) as e:
        logger.error(f"Facebook connect token resolve failed: token={token!r} error={type(e).__name__}: {e}")
        return None, 'رابط الربط غير صالح.'


def _callback_redirect_uri():
    return settings.SITE_BASE_URL.rstrip('/') + reverse('news_ai:facebook_connect_callback')


def _save_page(wp_site, page):
    wp_site.facebook_page_id = page['id']
    wp_site.facebook_access_token = page['access_token']
    wp_site.save(update_fields=['facebook_page_id', 'facebook_access_token'])


def facebook_connect_start(request, token):
    wp_site, error = _resolve_wp_site(token)
    if error:
        return render(request, 'facebook_connect/error.html', {'message': error}, status=400)

    if not settings.FACEBOOK_APP_ID:
        return render(request, 'facebook_connect/error.html', {
            'message': 'خدمة ربط فيسبوك غير مُفعّلة على الخادم حالياً.',
        }, status=503)

    oauth_url = (
        f'https://www.facebook.com/{GRAPH_VERSION}/dialog/oauth'
        f'?client_id={settings.FACEBOOK_APP_ID}'
        f'&redirect_uri={requests.utils.quote(_callback_redirect_uri(), safe="")}'
        f'&state={requests.utils.quote(token, safe="")}'
        f'&scope=pages_show_list,pages_read_engagement,pages_manage_posts,pages_read_user_content'
    )
    return render(request, 'facebook_connect/start.html', {'wp_site': wp_site, 'oauth_url': oauth_url})


def facebook_connect_callback(request):
    error = request.GET.get('error_description') or request.GET.get('error')
    if error:
        return render(request, 'facebook_connect/error.html', {'message': f'تم إلغاء الربط من طرف فيسبوك: {error}'})

    token = request.GET.get('state', '')
    code = request.GET.get('code')
    wp_site, resolve_error = _resolve_wp_site(token)
    if resolve_error:
        return render(request, 'facebook_connect/error.html', {'message': resolve_error}, status=400)

    try:
        short_lived = requests.get(f'https://graph.facebook.com/{GRAPH_VERSION}/oauth/access_token', params={
            'client_id': settings.FACEBOOK_APP_ID,
            'client_secret': settings.FACEBOOK_APP_SECRET,
            'redirect_uri': _callback_redirect_uri(),
            'code': code,
        }, timeout=15).json()['access_token']

        long_lived = requests.get(f'https://graph.facebook.com/{GRAPH_VERSION}/oauth/access_token', params={
            'grant_type': 'fb_exchange_token',
            'client_id': settings.FACEBOOK_APP_ID,
            'client_secret': settings.FACEBOOK_APP_SECRET,
            'fb_exchange_token': short_lived,
        }, timeout=15).json()['access_token']

        pages = requests.get(f'https://graph.facebook.com/{GRAPH_VERSION}/me/accounts', params={
            'access_token': long_lived,
        }, timeout=15).json().get('data', [])
    except Exception as e:
        return render(request, 'facebook_connect/error.html', {'message': f'تعذر إتمام الربط مع فيسبوك: {e}'})

    if not pages:
        return render(request, 'facebook_connect/error.html', {
            'message': 'لم يتم العثور على أي صفحة فيسبوك يديرها هذا الحساب.',
        })

    if len(pages) == 1:
        _save_page(wp_site, pages[0])
        return render(request, 'facebook_connect/success.html', {'wp_site': wp_site, 'page_name': pages[0]['name']})

    request.session[f'fb_connect_pages_{token}'] = pages
    return render(request, 'facebook_connect/choose_page.html', {'wp_site': wp_site, 'pages': pages, 'token': token})


@require_POST
def facebook_connect_choose_page(request, token):
    wp_site, error = _resolve_wp_site(token)
    if error:
        return render(request, 'facebook_connect/error.html', {'message': error}, status=400)

    pages = request.session.get(f'fb_connect_pages_{token}', [])
    page = next((p for p in pages if p['id'] == request.POST.get('page_id')), None)
    if not page:
        return render(request, 'facebook_connect/error.html', {'message': 'اختيار غير صالح.'}, status=400)

    _save_page(wp_site, page)
    request.session.pop(f'fb_connect_pages_{token}', None)
    return render(request, 'facebook_connect/success.html', {'wp_site': wp_site, 'page_name': page['name']})

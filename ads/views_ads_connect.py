"""
Self-serve Facebook Ad Account connect flow for the ads management add-on.

Mirrors syndicator/views_facebook_connect.py's Page-connect flow almost
exactly (stateless signed token, short-lived -> long-lived token exchange,
session-stashed picker for multiple items) but requests the
ads_management/ads_read/business_management scopes instead of the posting
scopes, and saves an AdAccountConnection instead of WordPressSite fields.

Kept as a separate flow (own token salt, own OAuth dialog) rather than
folded into the existing Page-connect flow: it's a separate Meta App Review
submission and a separate consent screen, and bundling them would force a
customer who only bought the Facebook posting add-on to also consent to ads
permissions they never asked for.
"""
import logging

import requests
from django.conf import settings
from django.core import signing
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from syndicator.models import WordPressSite
from . import marketing_api_client
from .models import AdAccountConnection

logger = logging.getLogger(__name__)

GRAPH_VERSION = settings.FACEBOOK_GRAPH_VERSION
TOKEN_SALT = 'ads_connect'
TOKEN_MAX_AGE = 60 * 60 * 24 * 30  # 30 days, matches the Page-connect flow

ADS_SCOPES = 'ads_management,ads_read,business_management'


def make_ads_connect_token(wp_site_pk):
    return signing.dumps(wp_site_pk, salt=TOKEN_SALT)


def _resolve_wp_site(token):
    """Returns (wp_site, None) or (None, error_message)."""
    try:
        wp_site_id = signing.loads(token, salt=TOKEN_SALT, max_age=TOKEN_MAX_AGE)
        return WordPressSite.objects.get(pk=wp_site_id), None
    except signing.SignatureExpired:
        return None, 'انتهت صلاحية رابط الربط (صالح لمدة 30 يوماً). يرجى طلب رابط جديد.'
    except (signing.BadSignature, WordPressSite.DoesNotExist) as e:
        logger.error(f"Ads connect token resolve failed: token={token!r} error={type(e).__name__}: {e}")
        return None, 'رابط الربط غير صالح.'


def _callback_redirect_uri():
    return settings.SITE_BASE_URL.rstrip('/') + reverse('ads:ads_connect_callback')


def _save_ad_account(wp_site, account, access_token):
    connection, _ = AdAccountConnection.objects.get_or_create(wp_site=wp_site)
    connection.facebook_ad_account_id = account['id']
    connection.facebook_business_id = (account.get('business') or {}).get('id', '') if isinstance(account.get('business'), dict) else ''
    connection.ad_account_name = account.get('name', '')
    connection.currency = account.get('currency', '')
    connection.access_token = access_token
    connection.status = 'connected'
    connection.error_message = ''
    connection.connected_at = timezone.now()
    connection.last_verified_at = timezone.now()
    connection.save()
    return connection


def ads_connect_start(request, token):
    wp_site, error = _resolve_wp_site(token)
    if error:
        return render(request, 'ads_connect/error.html', {'message': error}, status=400)

    if not settings.FACEBOOK_APP_ID:
        return render(request, 'ads_connect/error.html', {
            'message': 'خدمة ربط إعلانات فيسبوك غير مُفعّلة على الخادم حالياً.',
        }, status=503)

    oauth_url = (
        f'https://www.facebook.com/{GRAPH_VERSION}/dialog/oauth'
        f'?client_id={settings.FACEBOOK_APP_ID}'
        f'&redirect_uri={requests.utils.quote(_callback_redirect_uri(), safe="")}'
        f'&state={requests.utils.quote(token, safe="")}'
        f'&scope={ADS_SCOPES}'
    )
    return render(request, 'ads_connect/start.html', {'wp_site': wp_site, 'oauth_url': oauth_url})


def ads_connect_callback(request):
    error = request.GET.get('error_description') or request.GET.get('error')
    if error:
        return render(request, 'ads_connect/error.html', {'message': f'تم إلغاء الربط من طرف فيسبوك: {error}'})

    token = request.GET.get('state', '')
    code = request.GET.get('code')
    wp_site, resolve_error = _resolve_wp_site(token)
    if resolve_error:
        return render(request, 'ads_connect/error.html', {'message': resolve_error}, status=400)

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
    except Exception as e:
        return render(request, 'ads_connect/error.html', {'message': f'تعذر إتمام الربط مع فيسبوك: {e}'})

    accounts, fetch_error = marketing_api_client.get_ad_accounts(long_lived)
    if fetch_error:
        return render(request, 'ads_connect/error.html', {'message': f'تعذر جلب حسابات الإعلانات: {fetch_error}'})

    if not accounts:
        return render(request, 'ads_connect/error.html', {
            'message': 'لم يتم العثور على أي حساب إعلانات يديره هذا الحساب على فيسبوك.',
        })

    if len(accounts) == 1:
        connection = _save_ad_account(wp_site, accounts[0], long_lived)
        return render(request, 'ads_connect/success.html', {'wp_site': wp_site, 'account_name': connection.ad_account_name})

    request.session[f'ads_connect_accounts_{token}'] = accounts
    request.session[f'ads_connect_token_{token}'] = long_lived
    return render(request, 'ads_connect/choose_account.html', {'wp_site': wp_site, 'accounts': accounts, 'token': token})


@require_POST
def ads_connect_choose_account(request, token):
    wp_site, error = _resolve_wp_site(token)
    if error:
        return render(request, 'ads_connect/error.html', {'message': error}, status=400)

    accounts = request.session.get(f'ads_connect_accounts_{token}', [])
    long_lived = request.session.get(f'ads_connect_token_{token}')
    account = next((a for a in accounts if a['id'] == request.POST.get('account_id')), None)
    if not account or not long_lived:
        return render(request, 'ads_connect/error.html', {'message': 'اختيار غير صالح.'}, status=400)

    connection = _save_ad_account(wp_site, account, long_lived)
    request.session.pop(f'ads_connect_accounts_{token}', None)
    request.session.pop(f'ads_connect_token_{token}', None)
    return render(request, 'ads_connect/success.html', {'wp_site': wp_site, 'account_name': connection.ad_account_name})

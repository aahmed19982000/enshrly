"""
Thin wrapper around the Facebook Marketing API (Graph API's act_<id> ad
account endpoints). Plain `requests`, no facebook-business SDK - consistent
with syndicator/social_image_utils.py's post_to_facebook_page, which talks
to the (non-ads) Graph API the same way.

Every public function returns a (result, error) tuple and never raises for
*expected* Graph API errors (bad params, disapproved creative, etc.) - the
caller (ads/tasks.py) records those on the Campaign/AdSet/etc. row, mirroring
social_image_utils.py's "never raises - every failure is recorded on the
model itself" convention.

Unlike post_to_facebook_page (fire-and-forget from the request/response
cycle, so it can't afford to block), every call here only ever runs inside a
Celery task - so _graph_request is allowed to block on retry/backoff for
rate limits, which ad-account endpoints hit far more readily than the Page
posting endpoint ever did.
"""
import logging
import time

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

GRAPH_VERSION = settings.FACEBOOK_GRAPH_VERSION
GRAPH_BASE = f'https://graph.facebook.com/{GRAPH_VERSION}'

# Graph API error codes worth retrying in-process: 4/17/32/613 are
# app/user/ad-account rate limits, 80000/80003/80004 are the Marketing
# API's own ad-account-level rate limits.
RETRYABLE_ERROR_CODES = {4, 17, 32, 613, 80000, 80003, 80004}
MAX_RETRIES = 3


class GraphAPIError(Exception):
    """Raised only when a call fails in a way the caller can't sensibly
    record as a normal business-logic failure: retries exhausted on a rate
    limit, or a network-level failure. Expected API errors (bad targeting,
    disapproved creative, etc.) are returned as (None, message) instead."""
    def __init__(self, message, code=None, subcode=None):
        super().__init__(message)
        self.code = code
        self.subcode = subcode


def _graph_request(method, path, access_token, params=None, files=None):
    """Low-level call: retries with exponential backoff on rate-limit
    errors, raises GraphAPIError if retries are exhausted or the request
    fails outright, otherwise returns the parsed JSON body as-is (including
    any {'error': ...} payload - callers decide what counts as failure)."""
    url = f'{GRAPH_BASE}/{path}'
    body_params = dict(params or {})
    body_params['access_token'] = access_token

    last_error = None
    for attempt in range(MAX_RETRIES):
        try:
            if method == 'GET':
                response = requests.get(url, params=body_params, timeout=30)
            else:
                response = requests.post(url, data=body_params, files=files, timeout=30)
        except requests.RequestException as e:
            last_error = str(e)
            time.sleep(2 ** attempt)
            continue

        try:
            data = response.json()
        except ValueError:
            raise GraphAPIError(f'استجابة غير صالحة من فيسبوك (HTTP {response.status_code}): {response.text[:300]}')

        error = data.get('error') if isinstance(data, dict) else None
        error_code = error.get('code') if error else None

        if response.status_code == 429 or error_code in RETRYABLE_ERROR_CODES:
            last_error = error.get('message') if error else f'HTTP {response.status_code}'
            logger.warning(f"Graph API rate limit hit (code={error_code}) on {path}, attempt {attempt + 1}/{MAX_RETRIES}")
            time.sleep(2 ** attempt)
            continue

        return data

    raise GraphAPIError(f'تجاوز عدد محاولات إعادة الاتصال بفيسبوك: {last_error}', code=None)


def _extract_error_message(data):
    error = data.get('error') if isinstance(data, dict) else None
    if not error:
        return None
    return error.get('error_user_msg') or error.get('message') or str(error)


def get_ad_accounts(access_token):
    """Returns (list_of_accounts, error). Each account dict has id
    ('act_...'), name, currency, account_status, business_name (if any)."""
    try:
        data = _graph_request('GET', 'me/adaccounts', access_token, params={
            'fields': 'id,name,currency,account_status,business_name',
            'limit': 100,
        })
    except GraphAPIError as e:
        return None, str(e)

    error_message = _extract_error_message(data)
    if error_message:
        return None, error_message
    return data.get('data', []), None


def get_page_posts(page_id, access_token, limit=15):
    """Returns (list_of_posts, error). Each post dict has bare id (page_id
    prefix stripped, to match SocialSharePost.facebook_post_id's
    convention), message, created_time, full_picture, permalink_url."""
    try:
        data = _graph_request('GET', f'{page_id}/posts', access_token, params={
            'fields': 'id,message,created_time,full_picture,permalink_url',
            'limit': limit,
        })
    except GraphAPIError as e:
        return None, str(e)

    error_message = _extract_error_message(data)
    if error_message:
        return None, error_message

    posts = data.get('data', [])
    prefix = f'{page_id}_'
    for post in posts:
        if post.get('id', '').startswith(prefix):
            post['id'] = post['id'][len(prefix):]
    return posts, None


def get_page_post(page_id, post_id, access_token):
    """Returns (post_dict, error) for one post - post_id passed bare."""
    try:
        data = _graph_request('GET', f'{page_id}_{post_id}', access_token, params={
            'fields': 'message,permalink_url',
        })
    except GraphAPIError as e:
        return None, str(e)

    error_message = _extract_error_message(data)
    if error_message:
        return None, error_message
    return data, None


def verify_access_token(access_token):
    """Lightweight check that the stored user token is still valid -
    used by ads.tasks.poll_campaign_health_task to detect revoked access."""
    try:
        data = _graph_request('GET', 'me', access_token, params={'fields': 'id'})
    except GraphAPIError as e:
        return False, str(e)
    error_message = _extract_error_message(data)
    if error_message:
        return False, error_message
    return True, None


def create_campaign(ad_account_id, access_token, name, objective, status='ACTIVE'):
    """Returns (facebook_campaign_id, error)."""
    try:
        data = _graph_request('POST', f'{ad_account_id}/campaigns', access_token, params={
            'name': name,
            'objective': objective,
            'status': status,
            # Required by Meta for every campaign since the 2022 Special Ad
            # Category rollout - enshrly's use case (promoting news articles)
            # never falls under housing/employment/credit/social issues.
            'special_ad_categories': '[]',
        })
    except GraphAPIError as e:
        return None, str(e)

    error_message = _extract_error_message(data)
    if error_message:
        return None, error_message
    return data.get('id'), None


def create_ad_set(ad_account_id, access_token, campaign_id, name, daily_budget_cents,
                   optimization_goal, billing_event, targeting_spec, status='ACTIVE',
                   start_time=None, end_time=None):
    """daily_budget_cents: integer, smallest currency unit (e.g. cents for
    USD) - Graph API always expects budgets in that form, not decimal units.
    Returns (facebook_adset_id, error)."""
    params = {
        'name': name,
        'campaign_id': campaign_id,
        'daily_budget': daily_budget_cents,
        'billing_event': billing_event,
        'optimization_goal': optimization_goal,
        'targeting': _json_dumps(targeting_spec),
        'status': status,
    }
    if start_time:
        params['start_time'] = start_time
    if end_time:
        params['end_time'] = end_time

    try:
        data = _graph_request('POST', f'{ad_account_id}/adsets', access_token, params=params)
    except GraphAPIError as e:
        return None, str(e)

    error_message = _extract_error_message(data)
    if error_message:
        return None, error_message
    return data.get('id'), None


def upload_ad_image(ad_account_id, access_token, image_bytes, filename='creative.jpg'):
    """Uploads raw image bytes (e.g. Article.cover_image or
    SocialSharePost.generated_image's file content) to the ad account's
    image library. Returns (image_hash, error)."""
    try:
        data = _graph_request('POST', f'{ad_account_id}/adimages', access_token,
                               files={'filename': (filename, image_bytes)})
    except GraphAPIError as e:
        return None, str(e)

    error_message = _extract_error_message(data)
    if error_message:
        return None, error_message

    images = data.get('images', {})
    first = next(iter(images.values()), None)
    if not first or not first.get('hash'):
        return None, 'لم يُرجع فيسبوك بيانات الصورة المرفوعة.'
    return first['hash'], None


def create_ad_creative_from_existing_post(ad_account_id, access_token, page_id, facebook_post_id, name=''):
    """Boosts an already-published organic Page post - no image upload or
    link data needed, Graph reuses the post's own content. Returns
    (facebook_creative_id, error)."""
    try:
        data = _graph_request('POST', f'{ad_account_id}/adcreatives', access_token, params={
            'name': name or f'Creative for post {facebook_post_id}',
            'object_story_id': f'{page_id}_{facebook_post_id}',
        })
    except GraphAPIError as e:
        return None, str(e)

    error_message = _extract_error_message(data)
    if error_message:
        return None, error_message
    return data.get('id'), None


def create_ad_creative_from_link(ad_account_id, access_token, page_id, image_hash, link_url,
                                  message, headline, call_to_action='LEARN_MORE', name=''):
    """Builds a fresh creative (not boosting an existing post) - used as the
    article_cover fallback when no facebook_post_id is available. Returns
    (facebook_creative_id, error)."""
    object_story_spec = {
        'page_id': page_id,
        'link_data': {
            'image_hash': image_hash,
            'link': link_url,
            'message': message,
            'name': headline,
            'call_to_action': {'type': call_to_action, 'value': {'link': link_url}},
        },
    }
    try:
        data = _graph_request('POST', f'{ad_account_id}/adcreatives', access_token, params={
            'name': name or headline,
            'object_story_spec': _json_dumps(object_story_spec),
        })
    except GraphAPIError as e:
        return None, str(e)

    error_message = _extract_error_message(data)
    if error_message:
        return None, error_message
    return data.get('id'), None


def create_ad(ad_account_id, access_token, ad_set_id, creative_id, name, status='ACTIVE'):
    """Returns (facebook_ad_id, error)."""
    try:
        data = _graph_request('POST', f'{ad_account_id}/ads', access_token, params={
            'name': name,
            'adset_id': ad_set_id,
            'creative': _json_dumps({'creative_id': creative_id}),
            'status': status,
        })
    except GraphAPIError as e:
        return None, str(e)

    error_message = _extract_error_message(data)
    if error_message:
        return None, error_message
    return data.get('id'), None


def set_status(object_id, access_token, status):
    """Pauses/resumes any campaign/adset/ad object_id. Returns (success_bool, error)."""
    try:
        data = _graph_request('POST', object_id, access_token, params={'status': status})
    except GraphAPIError as e:
        return False, str(e)

    error_message = _extract_error_message(data)
    if error_message:
        return False, error_message
    return bool(data.get('success', True)), None


INSIGHT_FIELDS = 'impressions,reach,clicks,spend,ctr,cpc,cpm'


def fetch_insights(object_id, access_token, since, until):
    """Daily-broken-down insights for a campaign between since/until (date
    objects or 'YYYY-MM-DD' strings). Returns (list_of_daily_rows, error),
    where each row has date_start/date_stop plus the INSIGHT_FIELDS above."""
    try:
        data = _graph_request('GET', f'{object_id}/insights', access_token, params={
            'fields': INSIGHT_FIELDS,
            'time_range': _json_dumps({'since': str(since), 'until': str(until)}),
            'time_increment': 1,
        })
    except GraphAPIError as e:
        return None, str(e)

    error_message = _extract_error_message(data)
    if error_message:
        return None, error_message
    return data.get('data', []), None


def _json_dumps(value):
    import json
    return json.dumps(value)

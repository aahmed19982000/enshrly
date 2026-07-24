"""
Facebook auto-posting add-on: turns any published WordPress post (regardless
of whether Sahafi Hub or a human editor published it - see the plugin's
transition_post_status hook) into a Youm7-style share image (article photo +
headline baked onto it + site logo) and posts it to the site's connected
Facebook Page.

Billed/gated separately from the core syndication service via
WordPressSite.facebook_addon_is_active (see models.py) - callers must check
that before invoking generate_and_publish_social_share_from_wp_payload.
"""
import io
import logging

import requests
from django.conf import settings
from django.core.files.base import ContentFile
from django.utils import timezone
from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)

FONT_PATH = settings.BASE_DIR / 'static' / 'fonts' / 'Amiri-Bold.ttf'
CANVAS_WIDTH = 1200
WHITE = (255, 255, 255)
FACEBOOK_GRAPH_VERSION = 'v21.0'

# This Pillow build ships with libraqm (HarfBuzz + FriBidi), so draw.text()
# already shapes/reorders Arabic correctly on its own - text must be passed
# in raw logical order. Do NOT run it through arabic_reshaper/python-bidi
# first: that was written for basic-layout Pillow builds without raqm, and
# pre-shaping text that raqm then shapes *again* produces garbled glyphs.


def _hex_to_rgb(hex_color, fallback=(15, 23, 42)):
    try:
        hex_color = hex_color.lstrip('#')
        return tuple(int(hex_color[i:i + 2], 16) for i in (0, 2, 4))
    except Exception:
        return fallback


def _font(size):
    return ImageFont.truetype(str(FONT_PATH), size)


def _wrap_title(title, font, max_width, max_lines=3):
    """Word-wraps the logical Arabic string, measuring each candidate line's
    rendered (shaped) width via raqm-aware font.getlength()."""
    words = title.split()
    lines = []
    current = ''
    for word in words:
        candidate = f'{current} {word}'.strip()
        if font.getlength(candidate) <= max_width or not current:
            current = candidate
        else:
            lines.append(current)
            current = word
        if len(lines) == max_lines - 1:
            break
    if current:
        lines.append(current)
    # Whatever words didn't fit get folded into the last line with an ellipsis
    remaining = words[sum(len(l.split()) for l in lines):]
    if remaining:
        last = lines[-1]
        while font.getlength(last + ' …') > max_width and ' ' in last:
            last = last.rsplit(' ', 1)[0]
        lines[-1] = last + ' …'
    return lines[:max_lines]


def _draw_centered_lines(draw, lines, font, canvas_width, y_start, line_height, fill):
    y = y_start
    for line in lines:
        width = font.getlength(line)
        draw.text(((canvas_width - width) / 2, y), line, font=font, fill=fill)
        y += line_height
    return y


def _cover_resize(img, target_w, target_h):
    """CSS object-fit: cover equivalent - fills the target box, cropping any
    overflow, so the source photo's aspect ratio never gets distorted."""
    src_ratio = img.width / img.height
    dst_ratio = target_w / target_h
    if src_ratio > dst_ratio:
        new_height = target_h
        new_width = int(new_height * src_ratio)
    else:
        new_width = target_w
        new_height = int(new_width / src_ratio)
    img = img.resize((new_width, new_height), Image.LANCZOS)
    left = (new_width - target_w) // 2
    top = (new_height - target_h) // 2
    return img.crop((left, top, left + target_w, top + target_h))


def _paste_logo(canvas, wp_site, bottom, right=40, max_height=70):
    """Pastes the site's logo (if configured) bottom-right of the given
    baseline, alpha-composited so transparent PNG logos blend cleanly."""
    if not wp_site.social_logo:
        return
    try:
        with wp_site.social_logo.open('rb') as f:
            logo = Image.open(io.BytesIO(f.read())).convert('RGBA')
        ratio = max_height / logo.height
        logo = logo.resize((int(logo.width * ratio), max_height), Image.LANCZOS)
        x = canvas.width - right - logo.width
        y = bottom - logo.height - 24
        canvas.paste(logo, (x, y), logo)
    except Exception as e:
        logger.warning(f"Could not paste social_logo for {wp_site.name}: {e}")


def _render_bottom_banner(photo, title, wp_site):
    banner_h = 220
    canvas = Image.new('RGB', (CANVAS_WIDTH, 630 + banner_h), _hex_to_rgb(wp_site.social_secondary_color))
    canvas.paste(_cover_resize(photo, CANVAS_WIDTH, 630), (0, 0))
    draw = ImageDraw.Draw(canvas)
    draw.rectangle([0, 630, CANVAS_WIDTH, 630 + banner_h], fill=_hex_to_rgb(wp_site.social_primary_color))
    font = _font(52)
    lines = _wrap_title(title, font, CANVAS_WIDTH - 100)
    line_h = 64
    y_start = 630 + (banner_h - line_h * len(lines)) / 2
    _draw_centered_lines(draw, lines, font, CANVAS_WIDTH, y_start, line_h, WHITE)
    _paste_logo(canvas, wp_site, bottom=630 + banner_h)
    return canvas


def _render_boxed_card(photo, title, wp_site):
    border = 16
    canvas = Image.new('RGB', (CANVAS_WIDTH, 750), _hex_to_rgb(wp_site.social_primary_color))
    inner = _cover_resize(photo, CANVAS_WIDTH - border * 2, 750 - border * 2)
    canvas.paste(inner, (border, border))

    overlay_h = 240
    overlay = Image.new('RGBA', (CANVAS_WIDTH - border * 2, overlay_h), (*_hex_to_rgb(wp_site.social_secondary_color), 210))
    canvas.paste(Image.alpha_composite(canvas.crop((border, 750 - border - overlay_h, CANVAS_WIDTH - border, 750 - border)).convert('RGBA'), overlay).convert('RGB'), (border, 750 - border - overlay_h))

    draw = ImageDraw.Draw(canvas)
    font = _font(48)
    lines = _wrap_title(title, font, CANVAS_WIDTH - 160)
    line_h = 60
    y_start = 750 - border - overlay_h + (overlay_h - line_h * len(lines)) / 2
    _draw_centered_lines(draw, lines, font, CANVAS_WIDTH, y_start, line_h, WHITE)
    _paste_logo(canvas, wp_site, bottom=750 - border)
    return canvas


def _render_split_block(photo, title, wp_site):
    block_h = 230
    canvas = Image.new('RGB', (CANVAS_WIDTH, 630 + block_h), _hex_to_rgb(wp_site.social_secondary_color))
    canvas.paste(_cover_resize(photo, CANVAS_WIDTH, 630), (0, 0))
    draw = ImageDraw.Draw(canvas)
    draw.rectangle([0, 630, CANVAS_WIDTH, 636], fill=_hex_to_rgb(wp_site.social_primary_color))
    draw.rectangle([0, 636, CANVAS_WIDTH, 630 + block_h], fill=_hex_to_rgb(wp_site.social_secondary_color))
    font = _font(50)
    lines = _wrap_title(title, font, CANVAS_WIDTH - 120)
    line_h = 62
    y_start = 636 + (block_h - 6 - line_h * len(lines)) / 2
    _draw_centered_lines(draw, lines, font, CANVAS_WIDTH, y_start, line_h, WHITE)
    _paste_logo(canvas, wp_site, bottom=630 + block_h)
    return canvas


_TEMPLATE_RENDERERS = {
    'bottom_banner': _render_bottom_banner,
    'boxed_card': _render_boxed_card,
    'split_block': _render_split_block,
}


def generate_social_card_image(wp_site, title, photo_bytes):
    """Returns a PIL RGB Image with the given title composited over the given
    source photo, per wp_site's chosen template/colors/logo."""
    photo = Image.open(io.BytesIO(photo_bytes)).convert('RGB')
    renderer = _TEMPLATE_RENDERERS.get(wp_site.social_template, _render_bottom_banner)
    return renderer(photo, title, wp_site)


def post_to_facebook_page(wp_site, image_absolute_url, message):
    """Publishes the already-hosted image as a photo post on wp_site's
    connected Facebook Page. Returns (facebook_post_id, error_message) -
    exactly one of the two is set."""
    url = f'https://graph.facebook.com/{FACEBOOK_GRAPH_VERSION}/{wp_site.facebook_page_id}/photos'
    try:
        response = requests.post(url, data={
            'url': image_absolute_url,
            'caption': message,
            'access_token': wp_site.facebook_access_token,
        }, timeout=30)
    except requests.RequestException as e:
        return None, str(e)

    data = response.json() if response.content else {}
    if response.status_code == 200 and data.get('post_id'):
        return data['post_id'], None
    if response.status_code == 200 and data.get('id'):
        return data['id'], None
    return None, data.get('error', {}).get('message') or response.text[:500]


def _build_and_maybe_post(wp_site, title, image_bytes, link, article=None):
    """Shared core used by both entry points below: composites the card,
    saves a SocialSharePost row, and posts to Facebook if the site has a
    connected Page. Never raises - every failure is recorded on the
    SocialSharePost itself instead."""
    from .models import SocialSharePost

    social_post = SocialSharePost.objects.create(
        wp_site=wp_site,
        article=article,
        article_title=title or '',
        template_used=wp_site.social_template,
        status='failed',
    )

    if not title or not image_bytes:
        social_post.error_message = 'Missing title or source image.'
        social_post.save(update_fields=['error_message'])
        return social_post

    try:
        card = generate_social_card_image(wp_site, title, image_bytes)
    except Exception as e:
        logger.error(f"Error generating social card for {wp_site.name}: {e}")
        social_post.error_message = f'Image generation failed: {e}'
        social_post.save(update_fields=['error_message'])
        return social_post

    buffer = io.BytesIO()
    card.save(buffer, format='JPEG', quality=88)
    social_post.generated_image.save(f'{social_post.pk}.jpg', ContentFile(buffer.getvalue()), save=False)
    social_post.status = 'generated'
    social_post.save()

    if not wp_site.facebook_auto_publish_enabled:
        return social_post

    image_absolute_url = settings.SITE_BASE_URL.rstrip('/') + social_post.generated_image.url
    message = f'{title}\n\n{link}'.strip() if link else title
    post_id, error = post_to_facebook_page(wp_site, image_absolute_url, message)

    if post_id:
        social_post.status = 'posted'
        social_post.facebook_post_id = post_id
        social_post.posted_at = timezone.now()
        social_post.save(update_fields=['status', 'facebook_post_id', 'posted_at'])
    else:
        logger.error(f"Facebook post failed for {wp_site.name}: {error}")
        social_post.status = 'failed'
        social_post.error_message = error or 'Unknown Facebook API error.'
        social_post.save(update_fields=['status', 'error_message'])

    return social_post


def generate_and_publish_social_share_from_wp_payload(wp_site, payload):
    """Entry point called from the /api/wp-post-published/ webhook view for
    every post published on a customer WordPress site - by a human editor in
    wp-admin, or by Sahafi Hub's own REST push. Best-effort by design: any
    failure here must never surface back to WordPress/the caller as an error
    - see the try/except in views.wp_post_published_api_view."""
    from .models import AIImportLog

    title = (payload.get('title') or '').strip()
    image_url = payload.get('image_url') or ''
    link = payload.get('link') or ''

    # Best-effort link back to the Django-authored Article, if this post
    # happens to be one Sahafi Hub itself pushed to WordPress.
    article = None
    if link:
        log = AIImportLog.objects.filter(wp_site=wp_site, published_url=link).select_related('article').first()
        if log and log.article_id:
            article = log.article

    image_bytes = None
    if image_url:
        try:
            response = requests.get(image_url, timeout=20)
            response.raise_for_status()
            image_bytes = response.content
        except Exception as e:
            logger.error(f"Error downloading source image for {wp_site.name}: {e}")

    return _build_and_maybe_post(wp_site, title, image_bytes, link, article=article)


def generate_and_publish_social_share(article, wp_site, force=False):
    """Entry point used by the staff "regenerate social image" button
    (RegenerateSocialImageView) for a specific already-published Article/site
    pair. `force` is accepted for interface compatibility with that caller -
    every call here always creates a fresh SocialSharePost regardless, so
    there is no existing-post state to bypass."""
    from .models import AIImportLog

    log = AIImportLog.objects.filter(article=article, wp_site=wp_site).order_by('-id').first()
    link = log.published_url if log else ''

    image_bytes = None
    if article.cover_image:
        try:
            with article.cover_image.open('rb') as f:
                image_bytes = f.read()
        except Exception as e:
            logger.error(f"Error reading cover_image for article {article.pk}: {e}")

    return _build_and_maybe_post(wp_site, article.title, image_bytes, link, article=article)

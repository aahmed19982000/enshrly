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
CANVAS_WIDTH = 1080
CANVAS_HEIGHT = 1350  # 4:5 - Facebook/Instagram's mobile-optimized feed ratio
WHITE = (255, 255, 255)
FACEBOOK_GRAPH_VERSION = 'v21.0'
DEFAULT_BADGE_TEXT = 'خبر'

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


def _draw_right_aligned_lines(draw, lines, font, x_right, y_start, line_height, fill):
    """RTL-appropriate ragged-left block: every line's right edge lines up on
    x_right, same look as Al Jazeera-style caption blocks."""
    y = y_start
    for line in lines:
        width = font.getlength(line)
        draw.text((x_right - width, y), line, font=font, fill=fill)
        y += line_height
    return y


def _bottom_gradient(canvas, height, rgb_color, max_alpha=230, curve=1.3):
    """Composites a transparent-to-rgb_color vertical gradient over the
    bottom `height` px of canvas, for legible text over a busy photo."""
    grad = Image.linear_gradient('L').resize((canvas.width, height))
    grad = grad.point(lambda p: int((p / 255) ** curve * max_alpha))
    overlay = Image.new('RGBA', (canvas.width, height), rgb_color + (0,))
    overlay.putalpha(grad)
    region = canvas.crop((0, canvas.height - height, canvas.width, canvas.height)).convert('RGBA')
    blended = Image.alpha_composite(region, overlay).convert('RGB')
    canvas.paste(blended, (0, canvas.height - height))


def _draw_badge(draw, text, font, y, bg_color, x_left=None, x_right=None, text_color=WHITE, pad_x=22, pad_y=14, radius=10):
    """Draws a rounded, filled label (e.g. 'عاجل' / 'تقرير') anchored either
    from its left edge (x_left) or right edge (x_right) - exactly one must be
    given. Returns the label's bottom y coordinate."""
    ascent, descent = font.getmetrics()
    box_h = ascent + descent + pad_y * 2
    width = font.getlength(text) + pad_x * 2
    x0 = x_left if x_left is not None else x_right - width
    draw.rounded_rectangle([x0, y, x0 + width, y + box_h], radius=radius, fill=bg_color)
    draw.text((x0 + pad_x, y + pad_y), text, font=font, fill=text_color)
    return y + box_h


def _logo_badge_top_left(canvas, wp_site, x=44, y=44, box_h=88):
    """Pastes the site's logo (or, lacking one, its name as text) inside a
    solid white rounded card top-left - the masthead mark seen in
    news-agency style share cards."""
    draw = ImageDraw.Draw(canvas)
    pad_x, pad_y = 22, 14
    if wp_site.social_logo:
        try:
            with wp_site.social_logo.open('rb') as f:
                logo = Image.open(io.BytesIO(f.read())).convert('RGBA')
            inner_h = box_h - pad_y * 2
            ratio = inner_h / logo.height
            logo = logo.resize((int(logo.width * ratio), inner_h), Image.LANCZOS)
            box_w = logo.width + pad_x * 2
            draw.rounded_rectangle([x, y, x + box_w, y + box_h], radius=16, fill=WHITE)
            canvas.paste(logo, (x + pad_x, y + pad_y), logo)
            return
        except Exception as e:
            logger.warning(f"Could not paste social_logo for {wp_site.name}: {e}")

    font = _font(30)
    name = wp_site.name[:18]
    box_w = font.getlength(name) + pad_x * 2
    draw.rounded_rectangle([x, y, x + box_w, y + box_h], radius=16, fill=WHITE)
    ascent, descent = font.getmetrics()
    text_y = y + (box_h - ascent - descent) / 2
    draw.text((x + pad_x, text_y), name, font=font, fill=_hex_to_rgb(wp_site.social_secondary_color))


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
    banner_h = 360
    photo_h = CANVAS_HEIGHT - banner_h
    canvas = Image.new('RGB', (CANVAS_WIDTH, CANVAS_HEIGHT), _hex_to_rgb(wp_site.social_secondary_color))
    canvas.paste(_cover_resize(photo, CANVAS_WIDTH, photo_h), (0, 0))
    draw = ImageDraw.Draw(canvas)
    draw.rectangle([0, photo_h, CANVAS_WIDTH, CANVAS_HEIGHT], fill=_hex_to_rgb(wp_site.social_primary_color))
    font = _font(56)
    lines = _wrap_title(title, font, CANVAS_WIDTH - 120, max_lines=4)
    line_h = 70
    y_start = photo_h + (banner_h - line_h * len(lines)) / 2
    _draw_centered_lines(draw, lines, font, CANVAS_WIDTH, y_start, line_h, WHITE)
    _paste_logo(canvas, wp_site, bottom=CANVAS_HEIGHT)
    return canvas


def _render_boxed_card(photo, title, wp_site):
    border = 18
    canvas = Image.new('RGB', (CANVAS_WIDTH, CANVAS_HEIGHT), _hex_to_rgb(wp_site.social_primary_color))
    inner = _cover_resize(photo, CANVAS_WIDTH - border * 2, CANVAS_HEIGHT - border * 2)
    canvas.paste(inner, (border, border))

    overlay_h = 420
    overlay = Image.new('RGBA', (CANVAS_WIDTH - border * 2, overlay_h), (*_hex_to_rgb(wp_site.social_secondary_color), 215))
    region = canvas.crop((border, CANVAS_HEIGHT - border - overlay_h, CANVAS_WIDTH - border, CANVAS_HEIGHT - border)).convert('RGBA')
    canvas.paste(Image.alpha_composite(region, overlay).convert('RGB'), (border, CANVAS_HEIGHT - border - overlay_h))

    draw = ImageDraw.Draw(canvas)
    font = _font(52)
    lines = _wrap_title(title, font, CANVAS_WIDTH - 180, max_lines=4)
    line_h = 66
    y_start = CANVAS_HEIGHT - border - overlay_h + (overlay_h - line_h * len(lines)) / 2
    _draw_centered_lines(draw, lines, font, CANVAS_WIDTH, y_start, line_h, WHITE)
    _paste_logo(canvas, wp_site, bottom=CANVAS_HEIGHT - border)
    return canvas


def _render_split_block(photo, title, wp_site):
    block_h = 370
    photo_h = CANVAS_HEIGHT - block_h
    canvas = Image.new('RGB', (CANVAS_WIDTH, CANVAS_HEIGHT), _hex_to_rgb(wp_site.social_secondary_color))
    canvas.paste(_cover_resize(photo, CANVAS_WIDTH, photo_h), (0, 0))
    draw = ImageDraw.Draw(canvas)
    draw.rectangle([0, photo_h, CANVAS_WIDTH, photo_h + 6], fill=_hex_to_rgb(wp_site.social_primary_color))
    draw.rectangle([0, photo_h + 6, CANVAS_WIDTH, CANVAS_HEIGHT], fill=_hex_to_rgb(wp_site.social_secondary_color))
    font = _font(54)
    lines = _wrap_title(title, font, CANVAS_WIDTH - 140, max_lines=4)
    line_h = 68
    y_start = photo_h + 6 + (block_h - 6 - line_h * len(lines)) / 2
    _draw_centered_lines(draw, lines, font, CANVAS_WIDTH, y_start, line_h, WHITE)
    _paste_logo(canvas, wp_site, bottom=CANVAS_HEIGHT)
    return canvas


def _render_news_ribbon(photo, title, wp_site):
    """General-news style card (Youm7-esque): full-bleed photo, masthead
    logo top-left, a colored category ribbon, and the headline set over a
    bottom gradient for legibility."""
    canvas = Image.new('RGB', (CANVAS_WIDTH, CANVAS_HEIGHT), _hex_to_rgb(wp_site.social_secondary_color))
    canvas.paste(_cover_resize(photo, CANVAS_WIDTH, CANVAS_HEIGHT), (0, 0))

    gradient_h = 640
    _bottom_gradient(canvas, gradient_h, _hex_to_rgb(wp_site.social_secondary_color), max_alpha=235)

    draw = ImageDraw.Draw(canvas)
    _logo_badge_top_left(canvas, wp_site)

    badge_font = _font(32)
    badge_text = (wp_site.social_badge_text or DEFAULT_BADGE_TEXT).strip()
    badge_top = CANVAS_HEIGHT - gradient_h + 60
    badge_bottom = _draw_badge(draw, badge_text, badge_font, badge_top, _hex_to_rgb(wp_site.social_primary_color), x_right=CANVAS_WIDTH - 56)

    title_font = _font(56)
    lines = _wrap_title(title, title_font, CANVAS_WIDTH - 112, max_lines=4)
    line_h = 70
    y_start = badge_bottom + 36
    _draw_centered_lines(draw, lines, title_font, CANVAS_WIDTH, y_start, line_h, WHITE)
    return canvas


def _render_breaking_news(photo, title, wp_site):
    """Breaking-news style card (Al Jazeera-esque): photo on top, a solid
    color block below carrying an 'عاجل'-style badge and a right-aligned
    ragged headline."""
    block_h = 480
    photo_h = CANVAS_HEIGHT - block_h
    canvas = Image.new('RGB', (CANVAS_WIDTH, CANVAS_HEIGHT), _hex_to_rgb(wp_site.social_secondary_color))
    canvas.paste(_cover_resize(photo, CANVAS_WIDTH, photo_h), (0, 0))

    draw = ImageDraw.Draw(canvas)
    draw.rectangle([0, photo_h, CANVAS_WIDTH, photo_h + 6], fill=_hex_to_rgb(wp_site.social_primary_color))
    draw.rectangle([0, photo_h + 6, CANVAS_WIDTH, CANVAS_HEIGHT], fill=_hex_to_rgb(wp_site.social_secondary_color))

    _logo_badge_top_left(canvas, wp_site)

    badge_font = _font(34)
    badge_text = (wp_site.social_badge_text or 'عاجل').strip()
    badge_top = photo_h + 40
    badge_bottom = _draw_badge(draw, badge_text, badge_font, badge_top, _hex_to_rgb(wp_site.social_primary_color), x_right=CANVAS_WIDTH - 56)

    title_font = _font(52)
    lines = _wrap_title(title, title_font, CANVAS_WIDTH - 112, max_lines=4)
    line_h = 66
    y_start = badge_bottom + 30
    _draw_right_aligned_lines(draw, lines, title_font, CANVAS_WIDTH - 56, y_start, line_h, WHITE)
    return canvas


_TEMPLATE_RENDERERS = {
    'bottom_banner': _render_bottom_banner,
    'boxed_card': _render_boxed_card,
    'split_block': _render_split_block,
    'news_ribbon': _render_news_ribbon,
    'breaking_news': _render_breaking_news,
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

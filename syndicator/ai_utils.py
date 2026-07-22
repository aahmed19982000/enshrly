import os
import re
import json
import time
import random
import logging
import bleach
import requests
from datetime import timedelta
from zoneinfo import ZoneInfo
from urllib.parse import urlparse, urljoin
from bs4 import BeautifulSoup
from django.utils import timezone
from django.core.files.base import ContentFile
from django.contrib.auth.models import User
from decimal import Decimal
from django.db.models import Count, Q, Sum
from django.conf import settings
from .models import Article, Category, AISettings, AISource, AIImportLog, WordPressSite, WordPressScheduleSlot

logger = logging.getLogger(__name__)

CAIRO_TZ = ZoneInfo("Africa/Cairo")

# Every official/live price topic is excluded from the regular RSS-rewrite
# pipeline - the dedicated generators (generate_gold_price_article_for_site,
# generate_official_commodity_article_for_site, generate_arab_currencies_article_for_site,
# etc.) are the only source of truth for these topics, using real official
# data, to avoid duplicate/conflicting/possibly-fabricated articles.
# Iron/fish use narrower "price of X" phrases rather than the bare word
# ("حديد"/"سمك" alone are too common in unrelated news - e.g. "السكة الحديد"
# railway news - and would wrongly get blocked otherwise).
EXCLUDED_PRICE_TOPIC_KEYWORDS = [
    'ذهب', 'فضة', 'دولار',
    'إسمنت', 'الإسمنت',
    'سعر الحديد', 'أسعار الحديد', 'حديد عز',
    'دواجن',
    'سعر السمك', 'أسعار السمك',
    'أسعار الخضار',
    'الريال السعودي', 'الدينار الكويتي', 'الدرهم الإماراتي',
]


def is_excluded_price_topic(title, description=""):
    """Returns True if the RSS item is about a topic covered by a dedicated live/official price generator."""
    text = f"{title or ''} {description or ''}"
    return any(keyword in text for keyword in EXCLUDED_PRICE_TOPIC_KEYWORDS)


# Article body is rendered with the `|safe` template filter, so AI output must be
# restricted to a small safe subset before it's ever saved.
ALLOWED_BODY_TAGS = ['p', 'br', 'strong', 'em', 'b', 'i']
# First subheading is h2, then each subsequent one steps down a level (h3, h4, ...).
HEADING_TAGS = ['h2', 'h3', 'h4', 'h5', 'h6']

HEADING_STRUCTURE_INSTRUCTION = (
    "فقرة تمهيدية واحدة بوسم <p>، ثم عناوين فرعية متتالية بحيث يكون أول عنوان فرعي بوسم <h2>، "
    "والعنوان الفرعي الذي يليه بوسم <h3>، والذي يليه بوسم <h4>، وهكذا بحيث ينزل مستوى العنوان درجة "
    "واحدة مع كل عنوان فرعي جديد (لا تستخدم نفس مستوى العنوان مرتين). كل فقرة نصية توضع داخل وسم "
    "<p> تحت عنوانها الفرعي المناسب. لا تستخدم أي وسوم أو خصائص (attributes) أخرى غير <p> والعناوين "
    "الفرعية المذكورة."
)

# Shared writing-style instruction added to every generation prompt, aimed at
# Yoast's readability checks (short sentences/paragraphs, varied sentence
# openings, transition words, active voice) - all structural checks that
# apply regardless of Yoast's language support level for Arabic.
READABILITY_INSTRUCTION = (
    "اكتب بأسلوب سهل القراءة ومتوافق مع تحليل يوست (Yoast Readability): استخدم جملاً قصيرة "
    "(لا تتجاوز 20 كلمة للجملة الواحدة)، وفقرات قصيرة (2-3 جمل كحد أقصى لكل فقرة)، ونوّع بداية "
    "الجمل المتتالية ولا تبدأ جملتين متتاليتين بنفس الكلمة.\n"
    "- الكلمات الانتقالية: هذا الشرط الأكثر رسوباً - طبّقه كقاعدة ميكانيكية صارمة وليس كهدف عام: "
    "كل جملة ثانية على الأقل (أي جملة من كل جملتين متتاليتين) يجب أن تبدأ أو تحتوي على كلمة/عبارة ربط "
    "من هذه القائمة: (بالإضافة إلى ذلك، ومع ذلك، على سبيل المثال، في المقابل، وبالتالي، علاوة على ذلك، "
    "من ناحية أخرى، كما، لكن، إذ، بينما، نتيجة لذلك، وفي السياق ذاته، وتجدر الإشارة إلى أن، فضلاً عن ذلك، "
    "وعلى الرغم من ذلك، وفي هذا الإطار، ومن جهة أخرى، وهو ما، مما يعني أن). لا يكفي وضعها في جملة واحدة "
    "من كل خمس جمل - إن لم تكن نصف جمل النص تقريباً تبدأ أو تتضمن إحدى هذه الكلمات فالنص سيرسب في يوست. "
    "راجع نصك قبل التسليم واحسب النسبة ذهنياً.\n"
    "- المبني للمجهول: يوست يرفض أي نص تتجاوز فيه نسبة جمل المبني للمجهول 10% من إجمالي الجمل. تجنّبه "
    "بشكل شبه كامل: بدل 'تم رفع السعر' اكتب 'رفع التجار السعر' أو 'ارتفع السعر'، وبدل 'شهدت الأسواق "
    "ارتفاعاً' فضّل 'ارتفعت الأسواق' مباشرة. استخدم فاعلاً واضحاً (السوق، الحكومة، المستثمرون، البنك "
    "المركزي...) في كل جملة تقريباً بدل حذفه."
)

# Shared headline-writing instruction, referenced by every article generator
# so titles are punchy and specific instead of generic/flat, without crossing
# into false clickbait (every claim in the title must be backed by the real
# numbers/facts given in the prompt - never invent urgency or numbers).
STRONG_TITLE_INSTRUCTION = (
    "اكتب عنواناً قوياً وجذاباً، وليس مجرد وصف عام للموضوع. طبّق ما يلي:\n"
    "- ابدأ العنوان بأقوى عنصر إخباري في الخبر (الرقم الأكبر تغيّراً، القرار الأهم، النتيجة الأكثر إثارة) "
    "بدلاً من صيغة عامة مثل \"تحديث أسعار كذا اليوم\".\n"
    "- إن كان هناك رقم أو نسبة أو مقارنة حقيقية واردة في المعطيات (ارتفاع، انخفاض، مقدار بالجنيه أو بالنسبة "
    "المئوية)، ضمّنها في العنوان نفسه بدلاً من تركها للنص فقط - الأرقام تجذب القارئ.\n"
    "- استخدم فعلاً قوياً ومباشراً (يقفز، ينهار، يتراجع، يتخطى، يسجل، يفاجئ) بدل الأفعال الباهتة (يتغير، يتحرك).\n"
    "- اجعل العنوان محدداً وقصيراً (8-12 كلمة تقريباً)، بلا حشو أو عبارات عامة مثل \"تفاصيل\" أو \"كل ما تريد معرفته\".\n"
    "- ممنوع المبالغة أو اختراع أي رقم أو تفصيل غير مذكور في المعطيات الفعلية - قوة العنوان تأتي من صياغة "
    "الحقيقة بشكل مؤثر، وليس من تضخيمها أو تزييفها."
)

# Shared instruction for the image's alt text - deliberately separate from
# the headline: alt text should describe what the photo actually shows
# (professional accessibility/SEO practice), not just repeat the title.
IMAGE_ALT_INSTRUCTION = (
    "اكتب نصاً بديلاً مختصراً لصورة الغلاف (Alt Text) من 3 إلى 6 كلمات فقط، يصف محتوى الصورة بصرياً بعبارة "
    "بحثية طبيعية يستخدمها القارئ فعلاً عند البحث عن هذا الموضوع في جوجل (مثال: \"سبائك الذهب - سعر الذهب "
    "اليوم\"، \"عملات مصرية وأجنبية - أسعار العملات\") - وليس تكراراً حرفياً لعنوان الخبر الكامل. اجعله "
    "مختصراً ومباشراً يخدم السيو (SEO) وقارئ الشاشة معاً."
)

# Shared instruction added to every generation prompt that could otherwise
# echo the original outlet's name back (either because it's given as context,
# or because the source article/master text already mentions it) - covers
# both the title AND the body, since Gemini has been observed slipping the
# outlet name into a body sentence (e.g. "وبحسب موقع كذا...") even when the
# title itself is clean. Paired with a post-generation body check further
# down using title_contains_source_name() as a safety net.
NO_SOURCE_NAME_INSTRUCTION = (
    "لا تذكر اسم الموقع أو الوكالة الإخبارية أو الصحيفة التي نُقل عنها هذا الخبر إطلاقاً - لا في العنوان ولا في أي "
    "جملة من نص الخبر (تجنّب عبارات مثل \"بحسب موقع...\" أو \"ونقلت وكالة...\" أو \"وفقاً لما ذكرته منصة...\" "
    "أو \"وبحسب ما نشره...\"). تأكد من حذف أي أسماء مثل (اليوم السابع، الجزيرة، العربية، بلومبرغ، رويترز، بانكرز توداي... الخ) "
    "من النص المكتوب. اكتب الخبر مباشرة كحقيقة مستقلة وموثوقة دون الإشارة إلى أي جهة إعلامية بعينها."
)


def build_seo_keyphrase_instruction(use_rich_formatting):
    """
    Shared instruction ensuring the focus_keyword Gemini picks actually meets
    Yoast's own SEO analysis checks (keyphrase density, keyphrase in intro
    sentence, keyphrase in subheading, keyphrase in title/slug) - without this,
    Gemini tends to pick a focus_keyword post-hoc that barely appears in the
    body it already wrote.
    """
    instruction = (
        "اختر العبارة المفتاحية (focus_keyword) بما يناسب موضوع الخبر تحديداً، ثم تأكد أن هذه العبارة نفسها - أو "
        "صياغة قريبة جداً منها - تظهر حرفياً في: (1) العنوان، (2) الجملة الأولى من النص، (3) مرتين على الأقل "
        "إجمالاً ضمن النص الكامل"
    )
    if use_rich_formatting:
        instruction += "، (4) عنوان فرعي واحد على الأقل"
    instruction += ". اكتب حول العبارة بأسلوب طبيعي ومتدفق دون حشو مصطنع أو تكرار غير مبرر."
    return instruction


def sanitize_ai_body(html, allow_headings=False, allow_links=False, link_base_url=None):
    """
    Strips any tag/attribute outside a safe allowlist from AI-generated article HTML.
    When allow_links is set, <a href> is kept only if its host matches link_base_url -
    the AI is never trusted to place a link to an arbitrary/external domain.
    """
    tags = list(ALLOWED_BODY_TAGS)
    attributes = {}
    if allow_headings:
        tags += HEADING_TAGS
    if allow_links:
        tags += ['a']
        attributes['a'] = ['href']

    cleaned = bleach.clean(html or '', tags=tags, attributes=attributes, protocols=['http', 'https'], strip=True)

    if allow_links and link_base_url:
        allowed_host = urlparse(link_base_url).netloc
        soup = BeautifulSoup(cleaned, 'html.parser')
        for a_tag in soup.find_all('a'):
            href = a_tag.get('href', '')
            if urlparse(href).netloc != allowed_host:
                a_tag.unwrap()
        cleaned = str(soup)

    return cleaned


def sanitize_ai_text(text):
    """Strips all HTML from AI-generated plain-text fields (title, excerpt)."""
    return bleach.clean(text or '', tags=[], attributes={}, strip=True)


_ARABIC_DIACRITICS_RE = re.compile(r'[ً-ٰٟۖ-ۭ]')


def _normalize_arabic_for_match(text):
    """
    Loosely normalizes Arabic (and Latin) text for substring comparison:
    strips tashkeel/diacritics, unifies alef/yeh/teh-marbuta spelling
    variants, collapses whitespace, and lowercases. Not a full Arabic
    normalization library - just enough to catch minor spelling variants
    Gemini might introduce when echoing a source name back.
    """
    if not text:
        return ''
    text = _ARABIC_DIACRITICS_RE.sub('', text)
    text = text.replace('أ', 'ا').replace('إ', 'ا').replace('آ', 'ا')
    text = text.replace('ى', 'ي').replace('ة', 'ه')
    text = re.sub(r'\s+', ' ', text)
    return text.strip().lower()


def title_contains_source_name(title, source_name):
    """
    Returns True if `source_name` (e.g. "الجزيرة", "سكاي نيوز عربية") appears
    to be present inside `title` as a substring, using loose Arabic-aware
    normalization so minor spelling variants still match.

    Used to reject AI-generated regular-news titles that leak the source's
    name back in (Gemini is told the source name as context via the prompt
    and sometimes echoes it into the generated title, which client sites
    don't want attributed to a specific news outlet).
    """
    norm_title = _normalize_arabic_for_match(title)
    norm_source = _normalize_arabic_for_match(source_name)
    if not norm_title or not norm_source:
        return False
    return norm_source in norm_title


def apply_heading_color(html, color):
    """
    Applies the WordPress site's configured heading_color to every subheading tag.
    Done server-side (never trusts a color/style coming from the AI response).
    """
    if not html or not color or not re.match(r'^#[0-9A-Fa-f]{6}$', color):
        return html
    soup = BeautifulSoup(html, 'html.parser')
    for tag in soup.find_all(HEADING_TAGS):
        tag['style'] = f'color: {color};'
    return str(soup)

def call_gemini_api(prompt, api_key=None):
    """
    Calls the Gemini API directly using requests REST call.
    Uses Gemini 2.5 Flash. Returns a (text, usage) tuple, where usage is a
    dict with the real 'input_tokens'/'output_tokens' counts reported by
    Gemini's usageMetadata (used to compute the real cost instead of a
    guess), or (None, {}) on failure.
    """
    if not api_key:
        ai_settings = AISettings.get_settings()
        api_key = ai_settings.gemini_api_key or getattr(settings, 'GEMINI_API_KEY', None)

    if not api_key:
        logger.error("Gemini API key is not configured.")
        raise ValueError("Gemini API Key missing.")

    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={api_key}"
    headers = {"Content-Type": "application/json"}

    # Request JSON to output standard structured data
    payload = {
        "contents": [
            {
                "parts": [
                    {
                        "text": prompt
                    }
                ]
            }
        ],
        "generationConfig": {
            "responseMimeType": "application/json"
        }
    }

    try:
        response = requests.post(url, headers=headers, json=payload, timeout=30)
        response.raise_for_status()
        data = response.json()

        usage_meta = data.get("usageMetadata", {}) or {}
        usage = {
            "input_tokens": usage_meta.get("promptTokenCount"),
            # candidatesTokenCount is the visible output; thoughtsTokenCount
            # (Gemini 2.5 "thinking" tokens) is billed at the output rate too.
            "output_tokens": (usage_meta.get("candidatesTokenCount") or 0) + (usage_meta.get("thoughtsTokenCount") or 0) or None,
        }

        # Extract response text
        candidates = data.get("candidates", [])
        if candidates:
            text_response = candidates[0].get("content", {}).get("parts", [{}])[0].get("text", "")
            return text_response, usage
    except Exception as e:
        logger.error(f"Error calling Gemini API: {e}")
        if 'response' in locals():
            logger.error(f"Response: {response.text}")
    return None, {}


def fetch_full_article_text(url):
    """
    Fetches the full article body from a given URL to provide better context for the AI.
    """
    try:
        headers = {'User-Agent': 'Mozilla/5.0'}
        res = requests.get(url, headers=headers, timeout=10)
        if res.status_code != 200:
            return ""
        soup = BeautifulSoup(res.content, 'html.parser')
        
        # Look for main article body
        article = soup.find('article') or soup.find('div', id='articleBody') or soup.find('div', class_=re.compile(r'article.*body|content|story', re.I))
        if not article:
            article = soup
            
        paragraphs = article.find_all(['p', 'li', 'h2', 'h3'])
        text_parts = []
        for p in paragraphs:
            text = p.get_text(separator=' ', strip=True)
            if len(text) > 30 and text not in text_parts:
                text_parts.append(text)
        return '\n\n'.join(text_parts)
    except Exception as e:
        logger.warning(f"Could not fetch full article body from {url}: {e}")
        return ""


def fetch_google_trends_items(source_url):
    """
    Parses Google's daily trending-searches RSS feed. Unlike a normal news
    feed, each <item> is just a trending keyword with an empty description
    and a link back to the trends page itself - the actual real article
    explaining the trend is nested inside <ht:news_item>. This pulls that
    nested article out (the top one per trend) and returns it in the same
    shape as fetch_news_items_from_source.
    """
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'}
    items = []
    try:
        response = requests.get(source_url, headers=headers, timeout=15)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'lxml-xml')

        for trend in soup.find_all('item'):
            trend_title_tag = trend.find('title')
            trend_title = trend_title_tag.text.strip() if trend_title_tag else ""

            news_item = trend.find('ht:news_item')
            if not news_item:
                continue

            title_tag = news_item.find('ht:news_item_title')
            url_tag = news_item.find('ht:news_item_url')
            snippet_tag = news_item.find('ht:news_item_snippet')
            picture_tag = news_item.find('ht:news_item_picture')

            title_text = title_tag.text.strip() if title_tag else trend_title
            link_text = url_tag.text.strip() if url_tag else ""
            snippet_text = snippet_tag.text.strip() if snippet_tag else ""
            image_url = picture_tag.text.strip() if picture_tag else ""

            if not title_text or not link_text:
                continue

            description = f"{snippet_text} (الموضوع الرائج على جوجل: {trend_title})" if snippet_text else f"موضوع رائج على جوجل: {trend_title}"
            items.append({
                'title': title_text,
                'link': link_text,
                'description': description,
                'image_url': image_url,
                'guid': link_text,
            })
    except Exception as e:
        logger.error(f"Error fetching Google Trends items from {source_url}: {e}")
    return items


_IMAGE_META_SELECTORS = [
    ('meta', {'property': 'og:image'}),
    ('meta', {'property': 'og:image:secure_url'}),
    ('meta', {'name': 'twitter:image'}),
    ('meta', {'name': 'twitter:image:src'}),
    ('meta', {'itemprop': 'image'}),
    ('meta', {'name': 'thumbnail'}),
    ('link', {'rel': 'image_src'}),
]

_SKIP_IMAGE_HINTS = ('logo', 'icon', 'avatar', 'sprite', 'placeholder', '.svg')


_COMMON_LEADING_WORDS = {
    'the', 'a', 'an', 'official', 'in', 'on', 'at', 'for', 'to', 'of', 'and', 'new', 'during', 'amid',
}

# Generic office/event nouns that are far too broad on their own - "The
# President" alone matches almost anything famous (Lincoln, Biden, whoever)
# regardless of the actual article's subject. A candidate phrase is only
# useful if it also contains a genuine specific qualifier (a country, an
# org name, a proper noun) beyond just one of these.
_GENERIC_TITLE_WORDS = {
    'president', 'minister', 'prime', 'government', 'parliament', 'chairman',
    'king', 'queen', 'ambassador', 'official', 'delegation', 'committee',
    'meeting', 'visit', 'cooperation', 'ceremony', 'conference', 'session',
    'secretary', 'council', 'assembly', 'summit', 'agreement', 'deal', 'forces',
    'authority', 'union', 'organization', 'republic', 'state', 'nation',
}


def _extract_search_phrases(text):
    """
    Pulls likely proper-noun phrases (consecutive capitalized words, e.g.
    "Arab Parliament" or "Belarus") out of an English sentence. A full news
    headline almost never matches anything on Commons verbatim, but the
    country/organization/person names inside it usually do - see
    _search_commons_image below. Phrases made entirely of generic office/
    event words ("The President", "The Committee") are dropped since those
    alone match Commons' huge archive almost at random rather than anything
    relevant to this article. Longest, most specific phrases come first.
    """
    # [-\s]+ (not just \s+) so hyphenated names like "Al-Shabaab" are kept as
    # one phrase instead of splitting into a useless lone "Al" + "Shabaab".
    phrases = re.findall(r'\b[A-Z][a-zA-Z]*(?:[-\s]+[A-Z][a-zA-Z]*)*\b', text or '')
    seen = []
    for p in phrases:
        words = [w for w in re.split(r'[-\s]+', p) if w.lower() not in _COMMON_LEADING_WORDS]
        if not words:
            continue
        if all(w.lower() in _GENERIC_TITLE_WORDS for w in words):
            continue
        # A single short word (<=3 chars, e.g. a stray "Al") is too generic a
        # fragment to search on its own and too easily matches unrelated
        # files by coincidence - only keep it if paired with another word.
        if len(words) == 1 and len(words[0]) <= 3:
            continue
        cleaned = ' '.join(words)
        if cleaned not in seen:
            seen.append(cleaned)
    seen.sort(key=lambda p: -len(p.split()))
    return seen


def _title_is_relevant(title, q):
    title_words = set(re.findall(r"[a-zA-Z]+", title.lower()))
    query_words = [w.lower() for w in re.split(r'[-\s]+', q) if len(w) > 3]
    return any(w in title_words for w in query_words) if query_words else True


def _run_commons_search(q, min_width=500, min_height=300, limit=4):
    """Free, keyless Commons search for one phrase. Returns up to `limit` (url, title) candidates."""
    found = []
    try:
        resp = requests.get(
            'https://commons.wikimedia.org/w/api.php',
            params={
                'action': 'query',
                'generator': 'search',
                # Quoted so Commons requires the words to appear together as a
                # phrase, not just anywhere in a file's metadata - unquoted
                # multi-word searches matched wildly unrelated files
                # surprisingly often (verified live: "Qatar Stock Exchange"
                # unquoted returned a Swiss chemical plant).
                'gsrsearch': f'filetype:bitmap "{q}"',
                'gsrnamespace': 6,
                'gsrlimit': 8,
                'prop': 'imageinfo',
                'iiprop': 'url|size|mime',
                'format': 'json',
            },
            headers={'User-Agent': 'AlmaghribNewsBot/1.0 (https://almaghrib.online)'},
            timeout=8,
        )
        resp.raise_for_status()
        pages = (resp.json().get('query') or {}).get('pages') or {}
        for page in pages.values():
            info = (page.get('imageinfo') or [{}])[0]
            url = info.get('url') or ''
            mime = info.get('mime') or ''
            title = page.get('title') or ''
            if not url or not mime.startswith('image/') or mime == 'image/svg+xml':
                continue
            if (info.get('width') or 0) < min_width or (info.get('height') or 0) < min_height:
                continue
            if any(hint in url.lower() for hint in _SKIP_IMAGE_HINTS):
                continue
            # Verified live that Commons' own relevance ranking isn't
            # trustworthy on its own: quoted phrase search still top-ranked a
            # 1939 US Navy cruiser for "Ministry Information" and a maize
            # crop for "Africa Day", scored via categories/descriptions that
            # have nothing to do with the actual file. Requiring the search
            # phrase's own words to appear in the *title* reliably rejects
            # those coincidental mismatches.
            if not _title_is_relevant(title, q):
                continue
            found.append((url, title))
            if len(found) >= limit:
                break
    except Exception as e:
        logger.warning(f"Commons image search failed for '{q}': {e}")
    return found


def _gather_image_candidates(query, max_total=8):
    """
    Casts a wide net across every proper-noun phrase extracted from the
    (translated) query - not just the first one that hits - collecting real
    Commons candidates for _ai_pick_best_image to choose from below. Trying
    harder here means fewer articles fall back to the generic default image.
    """
    if not query or not query.strip():
        return []
    candidates = []
    seen_urls = set()
    for phrase in _extract_search_phrases(query):
        for url, title in _run_commons_search(phrase):
            if url not in seen_urls:
                candidates.append((url, title))
                seen_urls.add(url)
        if len(candidates) >= max_total:
            break
    return candidates[:max_total]


def _ai_pick_best_image(article_title, candidates):
    """
    One cheap Gemini text call (candidate *file titles* only - no image
    bytes sent, so this stays a fraction of a cent) that picks whichever
    Commons candidate is actually the best real match for this article, or
    rejects all of them if none genuinely fit or one looks graphic/
    unsuitable (e.g. a real attack/casualty photo) - a plain keyword search
    has no way to judge either of those, but a cheap AI read of the
    candidates' own titles does. Returns the chosen URL, or '' to fall
    through to the next tier (never raises - a failure here just means no
    AI-picked image, same as finding no candidates at all).
    """
    if not candidates:
        return ''
    listing = '\n'.join(f"{i + 1}. {title}" for i, (_, title) in enumerate(candidates))
    prompt = (
        f"مقال إخباري بعنوان (مترجم للإنجليزية): \"{article_title}\"\n\n"
        f"القائمة التالية أسماء ملفات صور حقيقية من أرشيف Wikimedia Commons، اختر منها الأنسب "
        f"لاستخدامها كصورة غلاف لهذا الخبر تحديداً:\n{listing}\n\n"
        f"اختر رقم الصورة الأكثر صلة موضوعية حقيقية بمحتوى الخبر. ارفض الاختيار (اختر null) إذا: "
        f"لا يوجد أي خيار له صلة حقيقية بالموضوع، أو كان الخيار الأنسب يبدو أنه صورة صادمة أو عنيفة "
        f"(هجوم، انفجار، سلاح، مصابين أو قتلى) حتى لو كانت مرتبطة بموضوع الخبر - هذه الحالات يُفضّل "
        f"فيها عدم اختيار أي صورة.\n\n"
        f"أرجع الإجابة بتنسيق JSON فقط دون أي نص إضافي: {{\"choice\": الرقم أو null}}"
    )
    try:
        text, _usage = call_gemini_api(prompt)
        if not text:
            return ''
        cleaned = text.strip()
        if cleaned.startswith("```json"):
            cleaned = cleaned[7:]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        choice = json.loads(cleaned.strip()).get('choice')
        if isinstance(choice, int) and 1 <= choice <= len(candidates):
            return candidates[choice - 1][0]
    except Exception as e:
        logger.warning(f"AI image pick failed for '{article_title}': {e}")
    return ''


def _find_topical_image(article_title, translated_query):
    """
    Full free-search-then-cheap-AI-review chain used wherever an article has
    no usable photo yet: gather real Commons candidates across every
    extracted phrase, then let Gemini pick the best (or correctly reject all
    of them). Returns a direct image URL, or '' if nothing suitable exists
    at all - only then should a caller fall back to the generic default.
    """
    candidates = _gather_image_candidates(translated_query)
    if not candidates:
        return ''
    return _ai_pick_best_image(article_title, candidates)


def _scrape_image_from_article_page(link_url, headers):
    """
    Best-effort fallback for RSS items with no usable image in the feed
    itself: fetches the linked article page and looks for a real photo on
    it, since many sources omit <enclosure>/<media:content> from their feed
    even though the article page itself has a perfectly good cover image.
    Tries every common "social preview" meta tag first (og:image and its
    common variants across sites), then falls back to the first
    reasonably-named <img> inside the page's main content area. Returns ''
    if nothing usable is found - callers fall back further from there.
    """
    try:
        page_res = requests.get(link_url, headers=headers, timeout=8)
        if page_res.status_code != 200:
            return ""
        page_soup = BeautifulSoup(page_res.content, 'html.parser')

        for tag_name, attrs in _IMAGE_META_SELECTORS:
            tag = page_soup.find(tag_name, attrs=attrs)
            value = tag.get('content') or tag.get('href') if tag else None
            if value:
                return value

        content_root = (
            page_soup.find('article')
            or page_soup.find('main')
            or page_soup.find(class_=re.compile(r'article-body|post-content|entry-content'))
        )
        if content_root:
            for img in content_root.find_all('img'):
                src = img.get('src') or img.get('data-src') or ''
                if src and not any(hint in src.lower() for hint in _SKIP_IMAGE_HINTS):
                    return urljoin(link_url, src)
    except Exception as pe:
        logger.warning(f"Failed to scrape a cover image from {link_url}: {pe}")
    return ""


def fetch_news_items_from_source(source_url):
    """
    Fetches news items from an RSS feed or webpage.
    Returns a list of dictionaries with keys: 'title', 'link', 'description', 'image_url', 'guid'.
    """
    if 'trends.google.com' in source_url:
        return fetch_google_trends_items(source_url)

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
    }
    items = []
    
    try:
        response = requests.get(source_url, headers=headers, timeout=15)
        response.raise_for_status()
        content = response.content
        
        # Try parsing as RSS/XML with fallback
        try:
            soup = BeautifulSoup(content, 'lxml-xml')
        except Exception:
            try:
                soup = BeautifulSoup(content, 'xml')
            except Exception:
                soup = BeautifulSoup(content, 'html.parser')
        channel_items = soup.find_all('item')
        
        if channel_items:
            # RSS format
            for item in channel_items:
                title = item.find('title')
                link = item.find('link')
                desc = item.find('description')
                guid = item.find('guid')
                
                title_text = title.text.strip() if title else ""
                link_text = link.text.strip() if link else ""
                desc_text = desc.text.strip() if desc else ""
                guid_text = guid.text.strip() if guid else link_text
                
                # Try to find image URL in RSS media tags
                image_url = ""
                enclosure = item.find('enclosure')
                if enclosure and enclosure.get('url'):
                    image_url = enclosure.get('url')
                else:
                    # Look for media:content
                    media_content = item.find('media:content') or item.find('content')
                    if media_content and media_content.get('url'):
                        image_url = media_content.get('url')
                    else:
                        # Extract image from description HTML
                        if desc_text:
                            img_soup = BeautifulSoup(desc_text, 'html.parser')
                            img = img_soup.find('img')
                            if img and img.get('src'):
                                image_url = img.get('src')
                                
                if not image_url and link_text:
                    image_url = _scrape_image_from_article_page(link_text, headers)

                # NOTE: deliberately NOT calling the AI-reviewed Commons search
                # (_find_topical_image) here - this function runs for every
                # single item in the feed on every scheduled poll (every 10
                # minutes, see scrape_and_generate_news_task's crontab),
                # regardless of whether an item ends up actually being
                # generated (most are skipped later as duplicates or excluded
                # topics). Doing a Gemini call per item here silently multiplied
                # into a real, unbounded cost spike. The AI search only runs
                # later, in generate_regular_article_for_site/reword_regular_
                # article_for_site, once an item has already survived the
                # duplicate/exclusion checks and is about to actually publish.

                items.append({
                    'title': title_text,
                    'link': link_text,
                    'description': BeautifulSoup(desc_text, 'html.parser').get_text(),
                    'image_url': image_url,
                    'guid': guid_text
                })
        else:
            # Standard Webpage format (fallback HTML parsing)
            html_soup = BeautifulSoup(content, 'html.parser')
            # Look for article links or common news containers
            articles = html_soup.find_all('article') or html_soup.find_all('div', class_=re.compile(r'post|article|news-item'))
            for idx, art in enumerate(articles[:10]):
                link_tag = art.find('a', href=True)
                title_tag = art.find(['h1', 'h2', 'h3', 'h4']) or art.find(class_=re.compile(r'title'))
                img_tag = art.find('img')
                
                if link_tag and title_tag:
                    title_text = title_tag.get_text().strip()
                    link_text = link_tag['href']
                    if not link_text.startswith('http'):
                        # Resolve relative links
                        from urllib.parse import urljoin
                        link_text = urljoin(source_url, link_text)
                    
                    image_url = img_tag.get('src') if img_tag else ""
                    if image_url and not image_url.startswith('http'):
                        from urllib.parse import urljoin
                        image_url = urljoin(source_url, image_url)
                        
                    items.append({
                        'title': title_text,
                        'link': link_text,
                        'description': title_text,
                        'image_url': image_url,
                        'guid': link_text
                    })
    except Exception as e:
        logger.error(f"Error fetching news from source {source_url}: {e}")
        
    return items


MAX_COVER_IMAGE_SIZE = (900, 600)
# Facebook's Sharing Debugger rejects og:image outright below 200x200 ("did
# not meet the minimum size constraint of 200px by 200px") - that hard cutoff
# is the actual bar to clear, not Facebook's separate "recommended" size
# (600x315+) for the large-card layout. Targeting only the real minimum (with
# a small safety margin) keeps the upscale factor small for already-decent
# small photos (e.g. many Egyptian news sites' RSS images sit right around
# 380x200) instead of stretching them 2-3x and visibly softening them.
MIN_COVER_IMAGE_SIZE = (220, 220)


def _process_cover_image_bytes(raw_bytes, filename):
    """
    Crops the bottom 10% (source watermarks), caps dimensions to
    MAX_COVER_IMAGE_SIZE (shrink only), upscales up to MIN_COVER_IMAGE_SIZE
    if the source photo is smaller than that, flattens transparency, and
    re-encodes as JPEG. Returns a Django ContentFile, falling back to the raw
    bytes unprocessed if Pillow can't handle this particular image.
    """
    filename = filename.rsplit('.', 1)[0] + '.jpg' if '.' in filename else (filename or 'cover') + '.jpg'
    try:
        from PIL import Image
        import io

        img = Image.open(io.BytesIO(raw_bytes))
        width, height = img.size
        # Crop bottom 10% (source watermarks) at full resolution first.
        cropped_img = img.crop((0, 0, width, int(height * 0.90)))
        # Cap dimensions to MAX_COVER_IMAGE_SIZE (shrink only).
        cropped_img.thumbnail(MAX_COVER_IMAGE_SIZE, Image.LANCZOS)

        # If the source photo was tiny, upscale up to MIN_COVER_IMAGE_SIZE so
        # Facebook doesn't reject it outright for being under its own minimum.
        cw, ch = cropped_img.size
        min_w, min_h = MIN_COVER_IMAGE_SIZE
        if cw < min_w or ch < min_h:
            scale = max(min_w / cw, min_h / ch)
            max_w, max_h = MAX_COVER_IMAGE_SIZE
            new_size = (min(round(cw * scale), max_w), min(round(ch * scale), max_h))
            cropped_img = cropped_img.resize(new_size, Image.LANCZOS)

        # JPEG has no alpha channel - flatten transparency onto white first,
        # otherwise Pillow raises and the except branch below would skip
        # cropping/resizing entirely, silently publishing an oversized image.
        if cropped_img.mode in ('RGBA', 'LA', 'P'):
            background = Image.new('RGB', cropped_img.size, (255, 255, 255))
            background.paste(cropped_img, mask=cropped_img.convert('RGBA').split()[-1])
            cropped_img = background
        elif cropped_img.mode != 'RGB':
            cropped_img = cropped_img.convert('RGB')

        img_io = io.BytesIO()
        cropped_img.save(img_io, format='JPEG', quality=92, optimize=True)
        img_io.seek(0)

        return ContentFile(img_io.read(), name=filename)
    except Exception as pe:
        logger.warning(f"Failed to process cover image, using original bytes as-is: {pe}")
        return ContentFile(raw_bytes, name=filename)


def _strip_wp_thumbnail_suffix(url):
    """
    WordPress (and several other CMSs) name auto-generated thumbnails like
    "photo-300x200.jpg" alongside the real full-size "photo.jpg". RSS feeds
    often link the small thumbnail. Returns the guessed full-size URL, or the
    original URL unchanged if it doesn't match this naming pattern.
    """
    return re.sub(r'-\d+x\d+(\.\w+)(\?.*)?$', r'\1\2', url)


def fetch_image_file(image_url):
    """
    Downloads an image from a URL and returns it processed via
    _process_cover_image_bytes(), or None on failure. If the URL looks like a
    WordPress-style resized thumbnail, the guessed full-size original is
    tried first (a real higher-resolution photo beats upscaling a small
    thumbnail), falling back to the given URL if that guess doesn't exist.
    """
    if not image_url:
        return None

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
    }
    full_size_guess = _strip_wp_thumbnail_suffix(image_url)
    urls_to_try = [full_size_guess, image_url] if full_size_guess != image_url else [image_url]

    last_error = None
    for url in urls_to_try:
        try:
            res = requests.get(url, headers=headers, timeout=10)
            res.raise_for_status()

            filename = url.split('/')[-1]
            if '?' in filename:
                filename = filename.split('?')[0]

            return _process_cover_image_bytes(res.content, filename)
        except Exception as e:
            last_error = e
            continue

    logger.error(f"Error downloading image {image_url}: {last_error}")
    return None


# Price/commodity articles have no per-item source photo the way RSS-rewritten
# news does (there's no "photo of a price"), so they published with no cover
# image at all. These are generic, appropriately-licensed illustrative photos
# (originally sourced from Wikimedia Commons, verified real photos) bundled
# locally under static/images/price_covers/ - purely visual, never used as a
# data source; every number in these articles still comes exclusively from
# the official price APIs. Bundled locally (rather than hotlinked) because
# Wikimedia's own file host rate-limits repeated direct requests.
DEFAULT_COVER_IMAGE_DIR = os.path.join(settings.BASE_DIR, 'static', 'images', 'price_covers')
DEFAULT_COVER_IMAGE_TYPES = {
    'gold', 'silver', 'dollar', 'iron', 'cement', 'poultry', 'fish', 'vegetable', 'arab_currencies',
    # Last-resort fallback for regular RSS-sourced articles when neither the
    # feed nor the linked article page (see _scrape_image_from_article_page)
    # has any usable photo - keeps every published post carrying a real
    # featured image instead of none at all (see push_article_to_wordpress's
    # Facebook/Jetpack og:image handling for why that matters).
    'general_news',
}


def attach_default_cover_image(article, content_type):
    """Attaches the generic illustrative cover image for this price-article content type, if one is configured."""
    if content_type not in DEFAULT_COVER_IMAGE_TYPES:
        return
    path = os.path.join(DEFAULT_COVER_IMAGE_DIR, f'{content_type}.jpg')
    try:
        with open(path, 'rb') as f:
            raw_bytes = f.read()
    except OSError as e:
        logger.warning(f"Default cover image missing for '{content_type}': {e}")
        return
    article.cover_image = _process_cover_image_bytes(raw_bytes, f'{content_type}.jpg')


def generate_slug_for_title(title):
    """
    Generates a unique, clean slug for an article title.
    """
    from django.utils.text import slugify
    import uuid
    # Standard slugify handles ASCII, but allow unicode for Arabic
    slug = re.sub(r'[^\w\s-]', '', title).strip().lower()
    slug = re.sub(r'[-\s]+', '-', slug)
    # Check uniqueness
    if not slug:
        slug = f"article-{uuid.uuid4().hex[:6]}"
    
    orig_slug = slug
    counter = 1
    while Article.all_objects.filter(slug=slug).exists():
        slug = f"{orig_slug}-{counter}"
        counter += 1
    return slug


def get_or_create_ai_author():
    """
    Gets or creates a default system author for AI-generated articles.
    """
    user, created = User.objects.get_or_create(
        username='ai_writer',
        defaults={
            'first_name': 'الذكاء',
            'last_name': 'الاصطناعي',
            'email': 'ai@almaghrib.com',
            'is_staff': True,
            'is_active': True
        }
    )
    if created:
        user.set_unusable_password()
        user.save()
    return user


def pick_default_author(ai_settings):
    """
    Picks one of the configured default authors at random, to vary attributed
    authorship across generated articles. Falls back to the system AI author
    when none are configured.
    """
    authors = list(ai_settings.default_authors.all())
    if authors:
        return random.choice(authors)
    return get_or_create_ai_author()


def fetch_recent_wp_posts(wp_site, limit=5):
    """
    Fetches a small list of recently published posts from the target WordPress
    site's own public REST API, to offer as internal-link candidates. Read-only
    and unauthenticated - these are just the site's normal public posts.
    """
    base_url = wp_site.url.rstrip('/')
    try:
        resp = requests.get(
            f"{base_url}/wp-json/wp/v2/posts",
            params={'per_page': limit, '_fields': 'title,link'},
            timeout=10
        )
        resp.raise_for_status()
        posts = []
        for p in resp.json():
            link = p.get('link', '')
            title = p.get('title', {}).get('rendered', '')
            if link and title:
                posts.append({'title': title, 'link': link})
        return posts
    except Exception as e:
        logger.warning(f"Failed to fetch recent posts from {wp_site.name} for internal linking: {e}")
        return []


GOLD_SPOT_API_URL = 'https://api.gold-api.com/price/XAU'
GOLD_FX_API_URL = 'https://open.er-api.com/v6/latest/USD'
GRAMS_PER_TROY_OUNCE = 31.1034768


def fetch_live_gold_prices():
    """
    Fetches the live gold spot price (USD/troy ounce, gold-api.com) and the
    USD->EGP exchange rate (open.er-api.com) - both free, keyless, public
    APIs - and computes per-gram Egyptian prices for common karats.
    Returns None if either request fails.
    """
    try:
        gold_resp = requests.get(GOLD_SPOT_API_URL, timeout=10)
        gold_resp.raise_for_status()
        spot_usd_per_oz = float(gold_resp.json()['price'])

        fx_resp = requests.get(GOLD_FX_API_URL, timeout=10)
        fx_resp.raise_for_status()
        usd_to_egp = float(fx_resp.json()['rates']['EGP'])
    except Exception as e:
        logger.error(f"Failed to fetch live gold price data: {e}")
        return None

    price_24k_egp = (spot_usd_per_oz / GRAMS_PER_TROY_OUNCE) * usd_to_egp
    price_21k_egp = price_24k_egp * 0.875
    return {
        'spot_usd_per_oz': round(spot_usd_per_oz, 2),
        'usd_to_egp': round(usd_to_egp, 2),
        'price_24k_egp': round(price_24k_egp, 2),
        'price_22k_egp': round(price_24k_egp * 0.916, 2),
        'price_21k_egp': round(price_21k_egp, 2),
        'price_18k_egp': round(price_24k_egp * 0.75, 2),
        'price_14k_egp': round(price_24k_egp * 0.585, 2),
        # The Egyptian gold pound (جنيه الذهب) is traditionally minted at 21k, ~8 grams.
        'gold_pound_egp': round(price_21k_egp * 8, 2),
        'timestamp': timezone.now(),
    }


SILVER_SPOT_API_URL = 'https://api.gold-api.com/price/XAG'


def fetch_live_silver_prices():
    """
    Fetches the live silver spot price (USD/troy ounce, gold-api.com) and the
    USD->EGP exchange rate, and computes the per-gram Egyptian price for pure
    (999) silver. Returns None if either request fails.
    """
    try:
        silver_resp = requests.get(SILVER_SPOT_API_URL, timeout=10)
        silver_resp.raise_for_status()
        spot_usd_per_oz = float(silver_resp.json()['price'])

        fx_resp = requests.get(GOLD_FX_API_URL, timeout=10)
        fx_resp.raise_for_status()
        usd_to_egp = float(fx_resp.json()['rates']['EGP'])
    except Exception as e:
        logger.error(f"Failed to fetch live silver price data: {e}")
        return None

    price_999_egp = (spot_usd_per_oz / GRAMS_PER_TROY_OUNCE) * usd_to_egp
    return {
        'spot_usd_per_oz': round(spot_usd_per_oz, 2),
        'usd_to_egp': round(usd_to_egp, 2),
        'price_999_egp': round(price_999_egp, 2),
        'price_925_egp': round(price_999_egp * 0.925, 2),
        'timestamp': timezone.now(),
    }


def fetch_live_dollar_price():
    """
    Fetches the live USD->EGP exchange rate (open.er-api.com, free/keyless).
    Returns None if the request fails.
    """
    try:
        fx_resp = requests.get(GOLD_FX_API_URL, timeout=10)
        fx_resp.raise_for_status()
        usd_to_egp = float(fx_resp.json()['rates']['EGP'])
    except Exception as e:
        logger.error(f"Failed to fetch live dollar price data: {e}")
        return None

    return {
        'usd_to_egp': round(usd_to_egp, 2),
        'timestamp': timezone.now(),
    }


# Official commodity price data from Egypt's Cabinet Information and Decision
# Support Center (IDSC) - the same backend that powers agriprice.gov.eg.
# Free, keyless, and includes a real day-over-day comparison already computed.
IDSC_API_BASE = 'http://app.prices.idsc.gov.eg/api'
IDSC_INDICATOR_IDS = {
    'iron': 7,              # حديد عز
    'iron_investment': 8,   # حديد إستثماري
    'cement': 92,           # الأسمنت الرمادي
    'poultry': 880,         # الدواجن الطازجة
    'red_meat': 877,        # اللحوم الطازجة
    'fish': 827,            # السمك (المتوسط العام)
    'fish_tilapia': 326,    # البلطي (ممتاز)
    'fish_shrimp': 331,     # الجمبري (وسط)
    'fish_sardine': 338,    # السردين المجمد
    'tomatoes': 2,
    'potatoes': 1,
    'onions': 824,
}


def fetch_idsc_indicator(indicator_key):
    """
    Fetches official real-time retail price data (with built-in comparison to
    yesterday/last week) for a single commodity from the IDSC price API.
    Returns None if the request fails.
    """
    indicator_id = IDSC_INDICATOR_IDS[indicator_key]
    try:
        resp = requests.get(
            f"{IDSC_API_BASE}/PricesData/GetMainIndicatorData/{indicator_id}",
            headers={'User-Agent': 'Mozilla/5.0'},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        return {
            'retail_price': data.get('retailAvgPrice'),
            'change_yesterday': data.get('retailComYest'),
            'change_week': data.get('retailComWeek'),
            'date': data.get('insertionDate'),
        }
    except Exception as e:
        logger.error(f"Failed to fetch IDSC indicator '{indicator_key}': {e}")
        return None


# Arab currencies plus the other non-Arab foreign currencies the same IDSC
# endpoint tracks (Euro, British Pound, Swiss Franc) - the US Dollar is
# deliberately excluded here since it already has its own dedicated article.
ARAB_CURRENCY_NAMES = ['ريال سعودي', 'دينار كويتي', 'درهم إماراتي', 'يورو', 'جنيه استرليني', 'فرنك سويسري']


def fetch_arab_currency_rates():
    """
    Fetches official real buy/sell exchange rates (with built-in comparison to
    yesterday) for Arab and other foreign currencies against the Egyptian
    pound, from the same IDSC price API used for gold/commodities. Returns
    None if the request fails or none of the expected currencies are present
    in the response.
    """
    try:
        resp = requests.get(
            f"{IDSC_API_BASE}/PricesData/GetCurrencyExchange",
            headers={'User-Agent': 'Mozilla/5.0'},
            timeout=10,
        )
        resp.raise_for_status()
        all_rates = resp.json()
    except Exception as e:
        logger.error(f"Failed to fetch currency exchange rates: {e}")
        return None

    result = []
    for currency in all_rates:
        if currency.get('name') in ARAB_CURRENCY_NAMES:
            result.append({
                'name': currency['name'],
                'buy_rate': currency.get('buyRate'),
                'sell_rate': currency.get('sellRate'),
                'change_yesterday': currency.get('sellRateDially'),
            })
    return result if result else None


def _is_due(last_run_at, min_hours):
    """True if last_run_at is empty or old enough that min_hours have elapsed since."""
    if not last_run_at:
        return True
    return (timezone.now() - last_run_at) >= timedelta(hours=min_hours)


# Must stay in sync with the Celery Beat interval for scrape_and_generate_news_task
# (every 10 minutes) - wide enough that a slot's window is always caught by at
# least one cycle tick, even if a tick runs a little late.
SLOT_TOLERANCE_MINUTES = 12


def get_due_slot(wp_site, content_type, tolerance_minutes=SLOT_TOLERANCE_MINUTES):
    """
    Returns the active WordPressScheduleSlot on this site that lists
    `content_type` among its content types, whose configured time is within
    `tolerance_minutes` of the current Cairo-local time, and where THIS
    content type specifically hasn't already run today (Cairo date). Tracked
    per-type (not per-slot) so a slot listing several types (e.g. iron +
    cement together) runs each of them independently - one type running
    doesn't block the others in the same slot for the rest of the day.
    Returns None if nothing matches right now.
    """
    now_cairo = timezone.now().astimezone(CAIRO_TZ)
    today_cairo = now_cairo.date().isoformat()
    for slot in wp_site.schedule_slots.filter(is_active=True):
        if content_type not in slot.get_content_types_list():
            continue
        if slot.get_last_run_date_for_type(content_type) == today_cairo:
            continue
        slot_dt = now_cairo.replace(hour=slot.time_of_day.hour, minute=slot.time_of_day.minute, second=0, microsecond=0)
        if abs((now_cairo - slot_dt).total_seconds()) <= tolerance_minutes * 60:
            return slot
    return None


def mark_slot_run(slot, content_type):
    """Marks a specific content type on this slot as having run today (Cairo date)."""
    slot.set_last_run_date_for_type(content_type, timezone.now().astimezone(CAIRO_TZ).date())


def get_regular_news_run_cap(wp_site, force=False):
    """
    Returns (cap, due_slot) for how many regular RSS/Trends articles this site
    may receive this cycle:
    - Sites with no schedule slots configured keep the legacy fixed
      `articles_per_run` cap, applied every cycle (unchanged behavior).
    - Sites with schedule slots only get "regular" articles when one of their
      slots is due right now, capped by that slot's own `regular_news_count`.
    - `force=True` (manual "generate now" trigger for one specific site)
      bypasses the slot-timing check entirely and always allows up to
      `articles_per_run`, without returning a due_slot - so no schedule
      bookkeeping is touched and the site's normal scheduled slot still fires
      later today as usual.
    """
    if force:
        return wp_site.articles_per_run, None
    if not wp_site.schedule_slots.filter(is_active=True).exists():
        return wp_site.articles_per_run, None
    due_slot = get_due_slot(wp_site, 'regular')
    if due_slot:
        return due_slot.regular_news_count, due_slot
    return 0, None


def sites_due_for_type(content_type, legacy_bool_field, ai_settings=None, last_at_field=None, min_hours=20, force_site_id=None):
    """
    Returns (list_of_wp_sites, due_slots_dict, legacy_used) of which active
    WordPress sites should generate a `content_type` price article this cycle:
    - Sites with schedule slots configured: only included if one of their
      slots lists this content type and is due right now (Cairo time). The
      slot's own per-site `last_run_date` is the sole gate; the legacy global
      once-daily gate below does not apply to these sites.
    - Sites without any schedule slots: fall back to the legacy behavior -
      included if their `legacy_bool_field` toggle is on, gated by the shared
      global `_is_due(ai_settings.<last_at_field>, min_hours)` check (same as
      before this feature existed). Pass `last_at_field=None` for content
      types (like gold) that have always fired every cycle with no gate.
    `legacy_used` is True if at least one non-slot site was included via the
    legacy gate - callers should only bump the shared `ai_settings.<last_at_field>`
    timestamp in that case, so a slot-only fetch doesn't skew the legacy gate
    for sites that aren't using slots.

    `force_site_id`: manual "generate now" trigger for one specific site.
    Restricts the result to that single site and bypasses both the slot-timing
    check and the legacy cooldown gate - but a site is still only included if
    this content type is actually configured for it (listed in one of its
    active slots, or its legacy boolean toggle is on for sites without slots).
    Deliberately never populates due_slots/legacy_used in this mode, so the
    caller never marks schedule bookkeeping as run and the site's normal
    automatic schedule for this type is left completely undisturbed.
    """
    result = []
    due_slots = {}
    legacy_used = False
    legacy_gate_open = True
    if last_at_field is not None and ai_settings is not None:
        legacy_gate_open = _is_due(getattr(ai_settings, last_at_field), min_hours)

    sites_qs = WordPressSite.objects.filter(is_active=True)
    if force_site_id:
        sites_qs = sites_qs.filter(id=force_site_id)

    for wp_site in sites_qs:
        has_slots = wp_site.schedule_slots.filter(is_active=True).exists()
        if force_site_id:
            if has_slots:
                configured = any(
                    content_type in slot.get_content_types_list()
                    for slot in wp_site.schedule_slots.filter(is_active=True)
                )
            else:
                configured = bool(getattr(wp_site, legacy_bool_field))
            if configured:
                result.append(wp_site)
            continue
        if has_slots:
            slot = get_due_slot(wp_site, content_type)
            if slot:
                result.append(wp_site)
                due_slots[wp_site.id] = slot
        elif getattr(wp_site, legacy_bool_field) and legacy_gate_open:
            result.append(wp_site)
            legacy_used = True

    return result, due_slots, legacy_used


def generate_official_commodity_article_for_site(wp_site, topic_title, items, source_url, ai_settings, api_key, allowed_cats, categories_list_str, content_type=None, wp_category_id=None):
    """
    Writes and publishes a fresh price-update article to a single WordPress site
    using one or more official IDSC price items, e.g. a single commodity (iron,
    cement, poultry, fish) or a small basket (vegetables). `items` is a list of
    (arabic_label, idsc_data_dict) tuples. Numbers come straight from the
    official source, including its own day-over-day comparison - nothing here
    is computed or invented by the AI.
    Returns True on a successful publish, False otherwise.
    """
    if wp_site.use_rich_formatting:
        body_format_instruction = f"محتوى الخبر الكامل مقسماً بأسلوب متوافق مع السيو (SEO): {HEADING_STRUCTURE_INSTRUCTION}"
    else:
        body_format_instruction = "محتوى الخبر الكامل بالتنسيق الصحفي مقسماً إلى فقرات باستخدام وسوم HTML للفقرات <p>...</p> حصراً."

    internal_link_instruction = ""
    if wp_site.use_internal_links:
        candidate_posts = fetch_recent_wp_posts(wp_site)
        if candidate_posts:
            links_list_str = "\n".join([f"- {p['title']}: {p['link']}" for p in candidate_posts])
            internal_link_instruction = (
                f"\nإن أمكن بشكل طبيعي، ضمّن رابطاً داخلياً واحداً أو رابطين على الأكثر باستخدام وسم "
                f"<a href=\"...\">نص الرابط</a> داخل فقرات الخبر، يشيران فقط إلى أحد الروابط التالية "
                f"لمقالات أخرى على نفس الموقع (لا تخترع أي رابط جديد، استخدم الروابط أدناه حرفياً):\n{links_list_str}"
            )

    numbers_lines = []
    for label, data in items:
        line = f"- {label}: {data['retail_price']} جنيه"
        if data.get('change_yesterday'):
            direction = "ارتفاع" if data['change_yesterday'] > 0 else "انخفاض"
            line += f" (بـ{direction} {abs(round(data['change_yesterday'], 2))} جنيه مقارنة بالأمس)"
        numbers_lines.append(line)
    numbers_block = "\n".join(numbers_lines)

    prompt = (
        f"بصفتك محررًا اقتصاديًا محترفًا باللغة العربية، اكتب خبرًا صحفيًا محدَّثًا عن {topic_title} في مصر، "
        f"معتمداً حصرياً على الأرقام الرسمية التالية الصادرة عن مركز معلومات مجلس الوزراء المصري لحظة كتابة "
        f"الخبر - اذكرها كما هي تماماً دون تقريب أو اختراع أي رقم بديل:\n"
        f"{numbers_block}\n\n"
        f"الرجاء الالتزام التام بالتعليمات التالية:\n"
        f"1. اكتب بأسلوب صحفي اقتصادي مباشر وواضح، بين 200 و350 كلمة. {READABILITY_INSTRUCTION}\n"
        f"2. {STRONG_TITLE_INSTRUCTION}\n"
        f"3. اكتب ملخصًا قصيرًا وموجزًا للخبر (Excerpt) مكون من سطرين إلى ثلاثة أسطر.\n"
        f"4. اذكر المقارنة بالأمس فقط إن وردت في الأرقام أعلاه، ولا تخترع أي مقارنة أو نسبة غير مذكورة.\n"
        f"5. {build_seo_keyphrase_instruction(wp_site.use_rich_formatting)}\n"
        f"6. {IMAGE_ALT_INSTRUCTION}\n"
        f"7. قم بإرجاع الإجابة بتنسيق JSON حصريًا دون أي علامات markdown أو علامات برمجية إضافية مثل ```json. "
        f"يجب أن يكون ملف الـ JSON يحتوي على المفاتيح التالية تماماً باللغة الإنجليزية:\n"
        f"- \"title\": عنوان الخبر\n"
        f"- \"excerpt\": ملخص الخبر\n"
        f"- \"body\": {body_format_instruction}\n"
        f"- \"category_id\": الرقم التعريفي (ID) للقسم المختار من القائمة المتاحة أدناه.\n"
        f"- \"focus_keyword\": عبارة مفتاحية قصيرة (2-4 كلمات) تلخص موضوع الخبر، لاستخدامها في تحليل السيو (SEO).\n"
        f"- \"meta_description\": وصف تعريفي (Meta Description) لمحركات البحث لا يتجاوز 155 حرفاً.\n"
        f"- \"image_alt\": النص البديل لصورة الغلاف كما هو موضح أعلاه.\n"
        f"- \"tags\": قائمة (array) من 3 إلى 5 وسوم؛ يجب أن يكون كل وسم مرتبطاً مباشرة بمحتوى هذا الخبر تحديداً "
        f"(وليس عاماً)، وأن يكون عبارة بحثية واقعية يستخدمها القارئ فعلاً عند البحث في جوجل عن هذا الموضوع بالذات.\n\n"
        f"8. اختر القسم الأنسب لهذا الخبر من قائمة الأقسام المتاحة التالية حصرياً:\n{categories_list_str}\n"
        f"{internal_link_instruction}"
    )

    ai_response, ai_usage = call_gemini_api(prompt, api_key=api_key)
    if not ai_response:
        AIImportLog.objects.create(
            source=None,
            source_url=source_url,
            wp_site=wp_site,
            title=f"تحديث {topic_title}",
            status='failed',
            error_message="لم يستجب الـ API الخاص بـ Gemini أو فشل استخراج النص."
        )
        return False

    try:
        cleaned_response = ai_response.strip()
        if cleaned_response.startswith("```json"):
            cleaned_response = cleaned_response[7:]
        if cleaned_response.endswith("```"):
            cleaned_response = cleaned_response[:-3]
        cleaned_response = cleaned_response.strip()

        data = json.loads(cleaned_response)
        new_title = sanitize_ai_text(data.get("title", "").strip())
        new_excerpt = sanitize_ai_text(data.get("excerpt", "").strip())
        new_body = sanitize_ai_body(
            data.get("body", "").strip(),
            allow_headings=wp_site.use_rich_formatting,
            allow_links=wp_site.use_internal_links,
            link_base_url=wp_site.url,
        )
        if wp_site.use_rich_formatting:
            new_body = apply_heading_color(new_body, wp_site.heading_color)
        focus_keyword = sanitize_ai_text(data.get("focus_keyword", "").strip())
        meta_description = sanitize_ai_text(data.get("meta_description", "").strip())
        image_alt = sanitize_ai_text(data.get("image_alt", "").strip())
        raw_tags = data.get("tags") or []
        if not isinstance(raw_tags, list):
            raw_tags = []
        ai_tags = [sanitize_ai_text(str(t).strip()) for t in raw_tags[:5] if str(t).strip()]

        try:
            chosen_cat_id = int(data.get("category_id"))
        except (ValueError, TypeError):
            chosen_cat_id = None

        if not new_title or not new_body:
            raise ValueError("بيانات العنوان أو المحتوى فارغة.")

        category = None
        if chosen_cat_id:
            category = Category.objects.filter(id=chosen_cat_id, is_active=True).first()
        if not category and allowed_cats:
            category = allowed_cats[0]

        from .core_utils import translate_text
        title_en = translate_text(new_title)
        body_en = translate_text(new_body)
        excerpt_en = translate_text(new_excerpt)

        author = pick_default_author(ai_settings)
        article = Article(
            title=new_title,
            title_ar=new_title,
            title_en=title_en,
            slug=generate_slug_for_title(new_title),
            body=new_body,
            body_ar=new_body,
            body_en=body_en,
            excerpt=new_excerpt,
            excerpt_ar=new_excerpt,
            excerpt_en=excerpt_en,
            author=author,
            category=category,
            cover_image_alt=image_alt,
            status='draft',
            published_at=timezone.now(),
            is_featured=False,
            is_breaking=False,
            auto_translate=False
        )
        if content_type:
            attach_default_cover_image(article, content_type)
        article.save()

        tag_names = (ai_tags if ai_tags else ([category.name] if category else [])) + wp_site.get_site_tags_list()
        published_url = None
        wp_error_detail = None
        try:
            published_url = push_article_to_wordpress(
                wp_site, article, extra_tag_names=tag_names,
                focus_keyword=focus_keyword, meta_description=meta_description, wp_category_id=wp_category_id
            )
        except Exception as wpe:
            logger.error(f"Error syndicating {topic_title} article to WP site {wp_site.name}: {wpe}")
            wp_error_detail = str(wpe)

        AIImportLog.objects.create(
            source=None,
            article=article,
            wp_site=wp_site,
            source_url=source_url,
            published_url=published_url or '',
            title=new_title,
            status='success' if published_url else 'failed',
            error_message='' if published_url else (wp_error_detail or 'فشل النشر على ووردبريس'),
            wp_category_id=wp_category_id,
            wp_category_name='أسعار',
            focus_keyword=focus_keyword,
            tag_names=','.join(tag_names) if tag_names else '',
            input_tokens=ai_usage.get('input_tokens'),
            output_tokens=ai_usage.get('output_tokens'),
        )
        return bool(published_url)
    except Exception as ex:
        logger.error(f"Failed to generate {topic_title} article for {wp_site.name}: {ex}")
        AIImportLog.objects.create(
            source=None,
            source_url=source_url,
            wp_site=wp_site,
            title=f"تحديث {topic_title}",
            status='failed',
            error_message=f"فشل صياغة خبر {topic_title} لـ {wp_site.name}: {str(ex)}",
            input_tokens=ai_usage.get('input_tokens'),
            output_tokens=ai_usage.get('output_tokens'),
        )
        return False


def generate_arab_currencies_article_for_site(wp_site, currency_items, source_url, ai_settings, api_key, allowed_cats, categories_list_str, wp_category_id=None):
    """
    Writes and publishes a fresh article covering Arab currencies' exchange
    rates (buy/sell) against the Egyptian pound to a single WordPress site,
    using the exact official numbers in currency_items. Mirrors the gold/
    dollar article style. Returns True on a successful publish, False otherwise.
    """
    if wp_site.use_rich_formatting:
        body_format_instruction = f"محتوى الخبر الكامل مقسماً بأسلوب متوافق مع السيو (SEO): {HEADING_STRUCTURE_INSTRUCTION}"
    else:
        body_format_instruction = "محتوى الخبر الكامل بالتنسيق الصحفي مقسماً إلى فقرات باستخدام وسوم HTML للفقرات <p>...</p> حصراً."

    internal_link_instruction = ""
    if wp_site.use_internal_links:
        candidate_posts = fetch_recent_wp_posts(wp_site)
        if candidate_posts:
            links_list_str = "\n".join([f"- {p['title']}: {p['link']}" for p in candidate_posts])
            internal_link_instruction = (
                f"\nإن أمكن بشكل طبيعي، ضمّن رابطاً داخلياً واحداً أو رابطين على الأكثر باستخدام وسم "
                f"<a href=\"...\">نص الرابط</a> داخل فقرات الخبر، يشيران فقط إلى أحد الروابط التالية "
                f"لمقالات أخرى على نفس الموقع (لا تخترع أي رابط جديد، استخدم الروابط أدناه حرفياً):\n{links_list_str}"
            )

    numbers_lines = []
    for currency in currency_items:
        line = f"- {currency['name']}: شراء {currency['buy_rate']} جنيه، بيع {currency['sell_rate']} جنيه"
        if currency.get('change_yesterday'):
            direction = "ارتفاع" if currency['change_yesterday'] > 0 else "انخفاض"
            line += f" (بـ{direction} {abs(round(currency['change_yesterday'], 3))} جنيه مقارنة بالأمس)"
        numbers_lines.append(line)
    numbers_block = "\n".join(numbers_lines)

    prompt = (
        f"بصفتك محررًا اقتصاديًا محترفًا باللغة العربية، اكتب خبرًا صحفيًا محدَّثًا عن أسعار صرف العملات العربية "
        f"والأجنبية مقابل الجنيه المصري اليوم، معتمداً حصرياً على الأرقام الرسمية التالية الصادرة عن مركز معلومات "
        f"مجلس الوزراء المصري لحظة كتابة الخبر - اذكرها كما هي تماماً دون تقريب أو اختراع أي رقم بديل:\n"
        f"{numbers_block}\n\n"
        f"الرجاء الالتزام التام بالتعليمات التالية:\n"
        f"1. اكتب بأسلوب صحفي اقتصادي مباشر وواضح، بين 200 و350 كلمة. {READABILITY_INSTRUCTION}\n"
        f"2. {STRONG_TITLE_INSTRUCTION}\n"
        f"3. اكتب ملخصًا قصيرًا وموجزًا للخبر (Excerpt) مكون من سطرين إلى ثلاثة أسطر.\n"
        f"4. اذكر سعري الشراء والبيع لكل عملة كما وردا أعلاه بدقة، واذكر المقارنة بالأمس فقط إن وردت في الأرقام، "
        f"ولا تخترع أي مقارنة أو نسبة غير مذكورة.\n"
        f"5. {build_seo_keyphrase_instruction(wp_site.use_rich_formatting)}\n"
        f"6. {IMAGE_ALT_INSTRUCTION}\n"
        f"7. قم بإرجاع الإجابة بتنسيق JSON حصريًا دون أي علامات markdown أو علامات برمجية إضافية مثل ```json. "
        f"يجب أن يكون ملف الـ JSON يحتوي على المفاتيح التالية تماماً باللغة الإنجليزية:\n"
        f"- \"title\": عنوان الخبر\n"
        f"- \"excerpt\": ملخص الخبر\n"
        f"- \"body\": {body_format_instruction}\n"
        f"- \"category_id\": الرقم التعريفي (ID) للقسم المختار من القائمة المتاحة أدناه.\n"
        f"- \"focus_keyword\": عبارة مفتاحية قصيرة (2-4 كلمات) تلخص موضوع الخبر، لاستخدامها في تحليل السيو (SEO).\n"
        f"- \"meta_description\": وصف تعريفي (Meta Description) لمحركات البحث لا يتجاوز 155 حرفاً.\n"
        f"- \"image_alt\": النص البديل لصورة الغلاف كما هو موضح أعلاه.\n"
        f"- \"tags\": قائمة (array) من 3 إلى 5 وسوم؛ يجب أن يكون كل وسم مرتبطاً مباشرة بمحتوى هذا الخبر تحديداً "
        f"(وليس عاماً)، وأن يكون عبارة بحثية واقعية يستخدمها القارئ فعلاً عند البحث في جوجل عن هذا الموضوع بالذات "
        f"(مثال: \"سعر الريال السعودي اليوم\"، \"سعر الدرهم الإماراتي مقابل الجنيه المصري\").\n\n"
        f"8. اختر القسم الأنسب لهذا الخبر من قائمة الأقسام المتاحة التالية حصرياً:\n{categories_list_str}\n"
        f"{internal_link_instruction}"
    )

    ai_response, ai_usage = call_gemini_api(prompt, api_key=api_key)
    if not ai_response:
        AIImportLog.objects.create(
            source=None,
            source_url=source_url,
            wp_site=wp_site,
            title="تحديث أسعار العملات العربية والأجنبية",
            status='failed',
            error_message="لم يستجب الـ API الخاص بـ Gemini أو فشل استخراج النص."
        )
        return False

    try:
        cleaned_response = ai_response.strip()
        if cleaned_response.startswith("```json"):
            cleaned_response = cleaned_response[7:]
        if cleaned_response.endswith("```"):
            cleaned_response = cleaned_response[:-3]
        cleaned_response = cleaned_response.strip()

        data = json.loads(cleaned_response)
        new_title = sanitize_ai_text(data.get("title", "").strip())
        new_excerpt = sanitize_ai_text(data.get("excerpt", "").strip())
        new_body = sanitize_ai_body(
            data.get("body", "").strip(),
            allow_headings=wp_site.use_rich_formatting,
            allow_links=wp_site.use_internal_links,
            link_base_url=wp_site.url,
        )
        if wp_site.use_rich_formatting:
            new_body = apply_heading_color(new_body, wp_site.heading_color)
        focus_keyword = sanitize_ai_text(data.get("focus_keyword", "").strip())
        meta_description = sanitize_ai_text(data.get("meta_description", "").strip())
        image_alt = sanitize_ai_text(data.get("image_alt", "").strip())
        raw_tags = data.get("tags") or []
        if not isinstance(raw_tags, list):
            raw_tags = []
        ai_tags = [sanitize_ai_text(str(t).strip()) for t in raw_tags[:5] if str(t).strip()]

        try:
            chosen_cat_id = int(data.get("category_id"))
        except (ValueError, TypeError):
            chosen_cat_id = None

        if not new_title or not new_body:
            raise ValueError("بيانات العنوان أو المحتوى فارغة.")

        category = None
        if chosen_cat_id:
            category = Category.objects.filter(id=chosen_cat_id, is_active=True).first()
        if not category and allowed_cats:
            category = allowed_cats[0]

        from .core_utils import translate_text
        title_en = translate_text(new_title)
        body_en = translate_text(new_body)
        excerpt_en = translate_text(new_excerpt)

        author = pick_default_author(ai_settings)
        article = Article(
            title=new_title,
            title_ar=new_title,
            title_en=title_en,
            slug=generate_slug_for_title(new_title),
            body=new_body,
            body_ar=new_body,
            body_en=body_en,
            excerpt=new_excerpt,
            excerpt_ar=new_excerpt,
            excerpt_en=excerpt_en,
            author=author,
            category=category,
            cover_image_alt=image_alt,
            status='draft',
            published_at=timezone.now(),
            is_featured=False,
            is_breaking=False,
            auto_translate=False
        )
        attach_default_cover_image(article, 'arab_currencies')
        article.save()

        tag_names = (ai_tags if ai_tags else ([category.name] if category else [])) + wp_site.get_site_tags_list()
        published_url = None
        wp_error_detail = None
        try:
            published_url = push_article_to_wordpress(
                wp_site, article, extra_tag_names=tag_names,
                focus_keyword=focus_keyword, meta_description=meta_description, wp_category_id=wp_category_id
            )
        except Exception as wpe:
            logger.error(f"Error syndicating Arab currencies article to WP site {wp_site.name}: {wpe}")
            wp_error_detail = str(wpe)

        AIImportLog.objects.create(
            source=None,
            article=article,
            wp_site=wp_site,
            source_url=source_url,
            published_url=published_url or '',
            title=new_title,
            status='success' if published_url else 'failed',
            error_message='' if published_url else (wp_error_detail or 'فشل النشر على ووردبريس'),
            wp_category_id=wp_category_id,
            wp_category_name='أسعار',
            focus_keyword=focus_keyword,
            tag_names=','.join(tag_names) if tag_names else '',
            input_tokens=ai_usage.get('input_tokens'),
            output_tokens=ai_usage.get('output_tokens'),
        )
        return bool(published_url)
    except Exception as ex:
        logger.error(f"Failed to generate Arab currencies article for {wp_site.name}: {ex}")
        AIImportLog.objects.create(
            source=None,
            source_url=source_url,
            wp_site=wp_site,
            title="تحديث أسعار العملات العربية والأجنبية",
            status='failed',
            error_message=f"فشل صياغة خبر أسعار العملات العربية لـ {wp_site.name}: {str(ex)}",
            input_tokens=ai_usage.get('input_tokens'),
            output_tokens=ai_usage.get('output_tokens'),
        )
        return False


def get_or_create_wp_tag_ids(wp_site, tag_names, auth):
    """
    Looks up each tag name via the WordPress REST API and creates it if missing.
    Returns the list of resolved WordPress tag IDs.
    """
    base_url = wp_site.url.rstrip('/')
    tags_url = f"{base_url}/wp-json/wp/v2/tags"
    tag_ids = []
    seen = set()
    for name in tag_names:
        name = (name or '').strip()
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        try:
            resp = requests.get(tags_url, auth=auth, params={'search': name}, timeout=10)
            resp.raise_for_status()
            match = next((t for t in resp.json() if t.get('name', '').strip().lower() == name.lower()), None)
            if match:
                tag_ids.append(match['id'])
                continue
            create_resp = requests.post(tags_url, auth=auth, json={'name': name}, timeout=10)
            if create_resp.status_code in (200, 201):
                tag_ids.append(create_resp.json()['id'])
            else:
                logger.warning(f"Failed to create WP tag '{name}' on {wp_site.name}: {create_resp.text}")
        except Exception as e:
            logger.warning(f"Failed to get/create WP tag '{name}' on {wp_site.name}: {e}")
    return tag_ids


def fetch_wp_primary_categories(wp_site):
    """
    Fetches this WordPress site's real categories from the AI News Controller
    plugin's own REST endpoint, filtered to the ones marked "primary" in the
    plugin's admin UI. Returns a list of {'id': int, 'name': str} dicts, or []
    if the request fails (e.g. an older plugin version without this route) -
    callers should fall back to the legacy Django category_mapping behavior.
    """
    from requests.auth import HTTPBasicAuth
    base_url = wp_site.url.rstrip('/')
    url = f"{base_url}/wp-json/ai-controller/v1/categories"
    auth = HTTPBasicAuth(wp_site.username, wp_site.application_password)
    try:
        resp = requests.get(url, auth=auth, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.warning(f"Failed to fetch primary categories from plugin on {wp_site.name}: {e}")
        return []
    return [{'id': c['id'], 'name': c['name']} for c in data if c.get('is_primary')]


def _wp_post_with_retry(url, auth, headers, payload, timeout, max_retries=2):
    """
    POSTs to a WordPress REST endpoint, retrying only failures that look
    transient (connection/timeout errors, or 5xx/429 responses) with a short
    backoff. Permanent-looking failures (4xx - bad auth, validation
    rejections, etc.) are returned immediately on the first attempt since
    retrying an identical request can't fix those and would only burn more
    time waiting on a doomed call.
    """
    attempt = 0
    while True:
        try:
            response = requests.post(url, auth=auth, headers=headers, json=payload, timeout=timeout)
        except requests.exceptions.RequestException as e:
            if attempt >= max_retries:
                raise
            attempt += 1
            logger.warning(f"Transient network error posting to {url} (attempt {attempt}/{max_retries}): {e}")
            time.sleep(3 * attempt)
            continue

        if response.status_code >= 500 or response.status_code == 429:
            if attempt >= max_retries:
                return response
            attempt += 1
            logger.warning(f"WP returned {response.status_code} from {url} (attempt {attempt}/{max_retries}), retrying")
            time.sleep(3 * attempt)
            continue

        return response


def push_article_to_wordpress(wp_site, article, extra_tag_names=None, focus_keyword=None, meta_description=None, wp_category_id=None):
    """
    Publishes an article to an external WordPress site via REST API.
    Handles uploading the cover image first and mapping categories.
    """
    from requests.auth import HTTPBasicAuth

    base_url = wp_site.url.rstrip('/')
    media_url = f"{base_url}/wp-json/wp/v2/media"
    posts_url = f"{base_url}/wp-json/wp/v2/posts"
    auth = HTTPBasicAuth(wp_site.username, wp_site.application_password)
    
    featured_media_id = None
    
    # 1. Upload cover image first if it exists
    if article.cover_image:
        try:
            image_name = article.cover_image.name.split('/')[-1]
            content_type = 'image/jpeg'
            if image_name.endswith('.webp'):
                content_type = 'image/webp'
            elif image_name.endswith('.png'):
                content_type = 'image/png'
                
            headers = {
                'Content-Disposition': f'attachment; filename={image_name}',
                'Content-Type': content_type
            }
            
            # Read binary data
            with article.cover_image.open('rb') as img_file:
                img_data = img_file.read()
                
            response = requests.post(media_url, auth=auth, headers=headers, data=img_data, timeout=20)

            if response.status_code == 201:
                media_data = response.json()
                featured_media_id = media_data.get('id')
                logger.info(f"Successfully uploaded media to WP site {wp_site.name}, ID: {featured_media_id}")

                # Set alt text (accessibility + SEO) - the upload above sends raw
                # binary data so alt_text can't ride along in the same request;
                # WordPress accepts it via a follow-up PATCH to the same media item.
                try:
                    requests.post(
                        f"{media_url}/{featured_media_id}",
                        auth=auth,
                        json={'alt_text': article.get_cover_image_alt()},
                        timeout=10,
                    )
                except Exception as alt_e:
                    logger.warning(f"Failed to set alt text for media {featured_media_id} on {wp_site.name}: {alt_e}")
            else:
                logger.error(f"Failed to upload media to WP site {wp_site.name}: {response.text}")
        except Exception as e:
            logger.error(f"Error uploading media to WP: {e}")

    # 2. Determine the real WP category. If the caller already resolved a real
    # WP category id (e.g. via the plugin's own primary-categories list, or a
    # forced category like "أسعار" for price articles), use it directly - the
    # plugin's own rest_insert_post hook adds any configured secondary
    # categories (like "الرئيسية") on top of it automatically. Otherwise fall
    # back to the legacy Django category_mapping name lookup for sites that
    # haven't set up the newer plugin-driven flow.
    wp_categories = []
    primary_category_id = None
    if wp_category_id is not None:
        primary_category_id = int(wp_category_id)
        wp_categories.append(primary_category_id)
    else:
        # Legacy: a local category can map to a single WP category ID (e.g.
        # {"اقتصاد": 5}) or to a primary category plus extra secondary ones
        # (e.g. {"اقتصاد": {"primary": 5, "secondary": [12, 20]}}).
        cat_mappings = wp_site.get_category_mappings()
        local_cat_name = article.category.name if article.category else ""

        mapping = cat_mappings.get(local_cat_name)
        if isinstance(mapping, dict):
            try:
                if mapping.get('primary') is not None:
                    primary_category_id = int(mapping['primary'])
                    wp_categories.append(primary_category_id)
            except (ValueError, TypeError):
                pass
            for secondary_id in mapping.get('secondary') or []:
                try:
                    wp_categories.append(int(secondary_id))
                except (ValueError, TypeError):
                    pass
        elif mapping is not None:
            try:
                primary_category_id = int(mapping)
                wp_categories.append(primary_category_id)
            except (ValueError, TypeError):
                pass


    # 3. Prepare post body. Created as a draft first (see step 4 below) so the
    # featured image is fully attached and processed on WordPress's side
    # before the post is actually published - mirrors how a human editor
    # publishes (set the image, then click Publish) and avoids Jetpack
    # Social/Publicize picking up a stale fallback share image when a post
    # is created and published in the exact same request.
    payload = {
        'title': article.title,
        'content': article.body,
        'excerpt': article.excerpt or '',
        'status': 'draft',
    }
    wp_author_ids = wp_site.get_wp_author_ids_list()
    if wp_author_ids:
        payload['author'] = random.choice(wp_author_ids)
    if featured_media_id:
        payload['featured_media'] = featured_media_id
    if wp_categories:
        payload['categories'] = wp_categories
    if extra_tag_names:
        tag_ids = get_or_create_wp_tag_ids(wp_site, extra_tag_names, auth)
        if tag_ids:
            payload['tags'] = tag_ids
    if focus_keyword or meta_description or primary_category_id:
        payload['meta'] = {}
        if focus_keyword:
            payload['meta']['_yoast_wpseo_focuskw'] = focus_keyword
        if meta_description:
            payload['meta']['_yoast_wpseo_metadesc'] = meta_description
        if primary_category_id:
            payload['meta']['_yoast_wpseo_primary_category'] = primary_category_id

    # 4. Push post as a draft, then transition it to published in a separate
    # follow-up request a few seconds later, giving WordPress time to finish
    # processing the featured image before Jetpack Social reads it for the
    # Facebook share preview.
    try:
        headers = {'Content-Type': 'application/json'}
        response = _wp_post_with_retry(posts_url, auth, headers, payload, timeout=20)
        if response.status_code != 201:
            logger.error(f"Failed to push post to WP site {wp_site.name}: {response.text}")
            raise Exception(f"رفض ووردبريس نشر المقال (كود {response.status_code}): {response.text[:300]}")

        post_data = response.json()
        post_id = post_data.get('id')
        published_url = post_data.get('link', '')

        if featured_media_id:
            time.sleep(5)

        try:
            publish_response = _wp_post_with_retry(
                f"{posts_url}/{post_id}", auth, headers, {'status': 'publish'}, timeout=20,
            )
            if publish_response.status_code == 200:
                published_url = publish_response.json().get('link', published_url)
            else:
                logger.error(f"Failed to publish (from draft) post {post_id} on WP site {wp_site.name}: {publish_response.text}")
        except Exception as pe:
            logger.error(f"Error publishing (from draft) post {post_id} on WP: {pe}")

        logger.info(f"Successfully syndicated article to WordPress site {wp_site.name}, URL: {published_url}")

        if wp_site.social_image_enabled:
            try:
                from .social_image_utils import generate_and_publish_social_share
                generate_and_publish_social_share(article, wp_site)
            except Exception as social_e:
                # Social-card generation/Facebook publishing is a best-effort
                # add-on - it must never affect the success of the actual
                # WordPress publish above.
                logger.error(f"Error generating social share image for {wp_site.name}: {social_e}")

        return published_url
    except Exception as e:
        logger.error(f"Error pushing post to WP: {e}")
        if str(e).startswith("رفض ووردبريس"):
            raise
        raise Exception(f"خطأ في الاتصال بووردبريس: {e}") from e


def _backfill_missing_cover_image(log):
    """
    Shared by republish_ai_log/redistribute: same free Commons search a
    fresh generation gets, but deliberately WITHOUT the extra Gemini review
    step (_ai_pick_best_image) - per feedback, failed-article republishing
    should just take the first real candidate that clears the (free) title-
    relevance filter rather than spend an extra AI call judging it. Falls
    back to the generic default only when the search finds nothing at all.
    """
    if log.article.cover_image:
        return
    search_query = log.focus_keyword or log.title or log.article.title
    from .core_utils import translate_text
    translated_query = translate_text(search_query) if search_query else ""
    candidates = _gather_image_candidates(translated_query) if translated_query else []
    commons_url = candidates[0][0] if candidates else ""
    img_file = fetch_image_file(commons_url) if commons_url else None
    if img_file:
        log.article.cover_image = img_file
    else:
        attach_default_cover_image(log.article, 'general_news')
    log.article.save()


def _push_saved_log_to_site(log, target_site, category_rotation=None):
    """
    Core of both republish_ai_log (same site) and the bulk redistribution
    tool (a chosen, possibly different site): re-pushes log.article using
    the focus keyword/tags already saved from the original generation - no
    new Gemini call, so no additional API cost either way.

    wp_category_id is only reused as-is when target_site is the log's
    original site - that id is a real WordPress term id specific to one
    site's own category taxonomy, so reusing it verbatim against a
    *different* site could tag the wrong category (or one that doesn't
    exist there at all). When redistributing, the category is re-resolved
    against target_site's own primary categories instead, matching by the
    real WordPress category name Gemini picked at generation time
    (wp_category_name) when target_site happens to offer a category with
    that same name too.

    When nothing matches, per feedback: don't dump every unmatched article
    into a single fallback category when a site has several primary
    categories configured in the WordPress plugin - that defeats the point
    of having more than one. `category_rotation` (a dict the caller keeps
    across a whole batch, keyed by site id) round-robins those unmatched
    articles across all of that site's primary categories instead. A fresh
    dict is used per call when the caller doesn't pass one (e.g. the
    single-row republish button), which just means "first primary category"
    for that one call - there's no batch to spread across anyway.

    Updates the log row in place (wp_site reassigned to wherever it actually
    landed; success clears error_message and fills published_url). Returns
    True on success, False otherwise.
    """
    if not log.article:
        log.error_message = "لا يمكن إعادة النشر: المقال غير موجود (ربما تم حذفه)."
        log.save(update_fields=['error_message'])
        return False

    _backfill_missing_cover_image(log)

    if log.wp_site_id == target_site.id:
        wp_category_id = log.wp_category_id
    else:
        wp_category_id = None
        site_primary_cats = fetch_wp_primary_categories(target_site)
        if site_primary_cats:
            # log.article.category is NOT a meaningful topic category on
            # sites using the plugin's real-category system - it's just a
            # placeholder (the site's first locally-allowed category) that
            # generate_regular_article_for_site sets purely because the
            # local Article row needs *something* there; these WP-bound
            # drafts are never shown publicly on this site. The actual
            # category Gemini picked only ever existed as wp_category_name
            # (a real WordPress category name, portable across sites, saved
            # alongside wp_category_id specifically for this reuse).
            wanted_name = (log.wp_category_name or '').strip()
            match = next((c for c in site_primary_cats if c['name'].strip() == wanted_name), None) if wanted_name else None
            if match:
                wp_category_id = match['id']
            else:
                rotation = category_rotation if category_rotation is not None else {}
                idx = rotation.get(target_site.id, 0)
                wp_category_id = site_primary_cats[idx % len(site_primary_cats)]['id']
                rotation[target_site.id] = idx + 1

    tag_names = [t for t in (log.tag_names or '').split(',') if t]
    try:
        published_url = push_article_to_wordpress(
            target_site, log.article, extra_tag_names=tag_names,
            focus_keyword=log.focus_keyword or None,
            meta_description=log.article.meta_desc or None,
            wp_category_id=wp_category_id,
        )
    except Exception as wpe:
        logger.error(f"Republish failed for AIImportLog {log.pk} on {target_site.name}: {wpe}")
        log.wp_site = target_site
        log.error_message = str(wpe)
        log.save(update_fields=['wp_site', 'error_message'])
        return False

    if published_url:
        log.wp_site = target_site
        log.status = 'success'
        log.published_url = published_url
        log.error_message = ''
        log.wp_category_id = wp_category_id
        log.save(update_fields=['wp_site', 'status', 'published_url', 'error_message', 'wp_category_id'])
        return True

    log.wp_site = target_site
    log.error_message = 'فشل النشر على ووردبريس'
    log.save(update_fields=['wp_site', 'error_message'])
    return False


def republish_ai_log(log):
    """
    Re-attempts the WordPress push for a previously failed AIImportLog entry
    on the same site it originally failed on. See _push_saved_log_to_site
    for the shared mechanics. Returns True on success, False otherwise.
    """
    if not log.wp_site:
        log.error_message = "لا يمكن إعادة النشر: الموقع المستهدف غير موجود (ربما تم حذفه)."
        log.save(update_fields=['error_message'])
        return False
    return _push_saved_log_to_site(log, log.wp_site)


def redistribute_and_republish_logs(log_ids, site_counts):
    """
    Bulk-redistributes a chosen set of failed AIImportLog entries according
    to an explicit admin-specified count per WordPress site (e.g. "Site A
    gets 5, Site B gets 3 of the selected articles"). Per request, this is a
    deliberate manual allocation and does NOT check any site's daily_limit -
    the admin is explicitly directing this batch, so the normal automatic-
    generation daily cap doesn't apply here. No new Gemini calls - reuses
    each article's already-paid-for content.

    `site_counts`: {site_id: count}. Sites with a count of 0 or less are
    ignored. Each count is consumed on every *attempt* at that site
    (success or failure) - it's an allocation of which articles go where,
    not a "keep retrying until N succeed" guarantee.

    Returns {'published': int, 'failed': int, 'skipped': int} - 'skipped'
    counts logs left over once every site's requested count is used up.
    """
    # Normalize keys to int - this crosses a Celery task boundary (see
    # redistribute_and_republish_logs_task), and JSON (Celery's default
    # serializer) always turns dict keys into strings.
    site_counts = {int(sid): count for sid, count in site_counts.items()}
    wanted_site_ids = [sid for sid, count in site_counts.items() if count and int(count) > 0]
    sites_by_id = {s.id: s for s in WordPressSite.objects.filter(id__in=wanted_site_ids, is_active=True)}
    results = {'published': 0, 'failed': 0, 'skipped': 0}
    if not sites_by_id:
        results['skipped'] = len(log_ids)
        return results

    remaining = {sid: int(site_counts[sid]) for sid in sites_by_id}
    sites_order = list(sites_by_id.values())

    logs = list(
        AIImportLog.objects.filter(id__in=log_ids, status='failed')
        .select_related('article', 'wp_site')
    )

    # Shared across the whole batch (not reset per-site) so unmatched
    # articles landing on the same site spread across all of that site's
    # primary categories instead of piling into just the first one.
    category_rotation = {}

    site_idx = 0
    for log in logs:
        target_site = None
        for _ in range(len(sites_order)):
            candidate = sites_order[site_idx % len(sites_order)]
            site_idx += 1
            if remaining.get(candidate.id, 0) > 0:
                target_site = candidate
                break
        if not target_site:
            results['skipped'] += 1
            continue

        remaining[target_site.id] -= 1
        if _push_saved_log_to_site(log, target_site, category_rotation=category_rotation):
            results['published'] += 1
        else:
            results['failed'] += 1
    return results


def generate_gold_price_article_for_site(wp_site, gold_data, comparison_text, ai_settings, api_key, allowed_cats, categories_list_str, wp_category_id=None):
    """
    Writes and publishes a fresh gold-price article to a single WordPress site,
    using the exact real numbers in gold_data rather than any AI-invented figures.
    Returns True on a successful publish, False otherwise.
    """
    if wp_site.use_rich_formatting:
        body_format_instruction = f"محتوى الخبر الكامل مقسماً بأسلوب متوافق مع السيو (SEO): {HEADING_STRUCTURE_INSTRUCTION}"
    else:
        body_format_instruction = "محتوى الخبر الكامل بالتنسيق الصحفي مقسماً إلى فقرات باستخدام وسوم HTML للفقرات <p>...</p> حصراً."

    internal_link_instruction = ""
    if wp_site.use_internal_links:
        candidate_posts = fetch_recent_wp_posts(wp_site)
        if candidate_posts:
            links_list_str = "\n".join([f"- {p['title']}: {p['link']}" for p in candidate_posts])
            internal_link_instruction = (
                f"\nإن أمكن بشكل طبيعي، ضمّن رابطاً داخلياً واحداً أو رابطين على الأكثر باستخدام وسم "
                f"<a href=\"...\">نص الرابط</a> داخل فقرات الخبر، يشيران فقط إلى أحد الروابط التالية "
                f"لمقالات أخرى على نفس الموقع (لا تخترع أي رابط جديد، استخدم الروابط أدناه حرفياً):\n{links_list_str}"
            )

    comparison_line = f"\n{comparison_text}" if comparison_text else "\nلا تتوفر بيانات مقارنة بتحديث سابق - لا تذكر أي مقارنة أو نسبة تغيير في هذه الحالة."

    prompt = (
        f"بصفتك محررًا اقتصاديًا محترفًا باللغة العربية، اكتب خبرًا صحفيًا محدَّثًا عن سعر الذهب اليوم في مصر، "
        f"معتمداً حصرياً على الأرقام الحقيقية التالية المأخوذة من السوق العالمية لحظة كتابة الخبر - "
        f"اذكرها كما هي تماماً دون تقريب أو اختراع أي رقم بديل:\n"
        f"- سعر أوقية الذهب عالمياً: {gold_data['spot_usd_per_oz']} دولار أمريكي\n"
        f"- سعر صرف الدولار: {gold_data['usd_to_egp']} جنيه مصري\n"
        f"- سعر جرام الذهب عيار 24: {gold_data['price_24k_egp']} جنيه مصري\n"
        f"- سعر جرام الذهب عيار 22: {gold_data['price_22k_egp']} جنيه مصري\n"
        f"- سعر جرام الذهب عيار 21: {gold_data['price_21k_egp']} جنيه مصري\n"
        f"- سعر جرام الذهب عيار 18: {gold_data['price_18k_egp']} جنيه مصري\n"
        f"- سعر جرام الذهب عيار 14: {gold_data['price_14k_egp']} جنيه مصري\n"
        f"- سعر جنيه الذهب (8 جرام عيار 21): {gold_data['gold_pound_egp']} جنيه مصري"
        f"{comparison_line}\n\n"
        f"الرجاء الالتزام التام بالتعليمات التالية:\n"
        f"1. اكتب بأسلوب صحفي اقتصادي مباشر وواضح، بين 250 و400 كلمة. {READABILITY_INSTRUCTION}\n"
        f"2. {STRONG_TITLE_INSTRUCTION}\n"
        f"3. اكتب ملخصًا قصيرًا وموجزًا للخبر (Excerpt) مكون من سطرين إلى ثلاثة أسطر.\n"
        f"4. أضف في نهاية الخبر فقرة قصيرة بعنوان \"نظرة عامة على السوق\" تصف الاتجاه العام لحركة الذهب عالمياً "
        f"بصياغة عامة ومتحفظة (مثل تأثير سعر الصرف أو حركة السوق العالمي)، على أن تنتهي الفقرة حرفياً بجملة توضيحية "
        f"مشابهة لـ: \"هذه قراءة عامة لحركة السوق ولا تُعد توصية استثمارية.\" لا تذكر أي أرقام أو مستويات أو نسب "
        f"مستقبلية مختلَقة، فقط وصف عام للاتجاه.\n"
        f"5. {build_seo_keyphrase_instruction(wp_site.use_rich_formatting)}\n"
        f"6. {IMAGE_ALT_INSTRUCTION}\n"
        f"7. قم بإرجاع الإجابة بتنسيق JSON حصريًا دون أي علامات markdown أو علامات برمجية إضافية مثل ```json. "
        f"يجب أن يكون ملف الـ JSON يحتوي على المفاتيح التالية تماماً باللغة الإنجليزية:\n"
        f"- \"title\": عنوان الخبر\n"
        f"- \"excerpt\": ملخص الخبر\n"
        f"- \"body\": {body_format_instruction}\n"
        f"- \"category_id\": الرقم التعريفي (ID) للقسم المختار من القائمة المتاحة أدناه.\n"
        f"- \"focus_keyword\": عبارة مفتاحية قصيرة (2-4 كلمات) تلخص موضوع الخبر، لاستخدامها في تحليل السيو (SEO).\n"
        f"- \"meta_description\": وصف تعريفي (Meta Description) لمحركات البحث لا يتجاوز 155 حرفاً.\n"
        f"- \"image_alt\": النص البديل لصورة الغلاف كما هو موضح أعلاه.\n"
        f"- \"tags\": قائمة (array) من 3 إلى 5 وسوم؛ يجب أن يكون كل وسم مرتبطاً مباشرة بمحتوى هذا الخبر تحديداً "
        f"(وليس عاماً)، وأن يكون عبارة بحثية واقعية يستخدمها القارئ فعلاً عند البحث في جوجل عن هذا الموضوع بالذات "
        f"(مثال: \"سعر الذهب اليوم\"، \"سعر جرام الذهب عيار 21\"، \"سعر جنيه الذهب في مصر\").\n\n"
        f"8. اختر القسم الأنسب لهذا الخبر من قائمة الأقسام المتاحة التالية حصرياً:\n{categories_list_str}\n"
        f"{internal_link_instruction}"
    )

    ai_response, ai_usage = call_gemini_api(prompt, api_key=api_key)
    if not ai_response:
        AIImportLog.objects.create(
            source=None,
            source_url=GOLD_SPOT_API_URL,
            wp_site=wp_site,
            title="تحديث سعر الذهب",
            status='failed',
            error_message="لم يستجب الـ API الخاص بـ Gemini أو فشل استخراج النص."
        )
        return False

    try:
        cleaned_response = ai_response.strip()
        if cleaned_response.startswith("```json"):
            cleaned_response = cleaned_response[7:]
        if cleaned_response.endswith("```"):
            cleaned_response = cleaned_response[:-3]
        cleaned_response = cleaned_response.strip()

        data = json.loads(cleaned_response)
        new_title = sanitize_ai_text(data.get("title", "").strip())
        new_excerpt = sanitize_ai_text(data.get("excerpt", "").strip())
        new_body = sanitize_ai_body(
            data.get("body", "").strip(),
            allow_headings=wp_site.use_rich_formatting,
            allow_links=wp_site.use_internal_links,
            link_base_url=wp_site.url,
        )
        if wp_site.use_rich_formatting:
            new_body = apply_heading_color(new_body, wp_site.heading_color)
        focus_keyword = sanitize_ai_text(data.get("focus_keyword", "").strip())
        meta_description = sanitize_ai_text(data.get("meta_description", "").strip())
        image_alt = sanitize_ai_text(data.get("image_alt", "").strip())
        raw_tags = data.get("tags") or []
        if not isinstance(raw_tags, list):
            raw_tags = []
        ai_tags = [sanitize_ai_text(str(t).strip()) for t in raw_tags[:5] if str(t).strip()]

        try:
            chosen_cat_id = int(data.get("category_id"))
        except (ValueError, TypeError):
            chosen_cat_id = None

        if not new_title or not new_body:
            raise ValueError("بيانات العنوان أو المحتوى فارغة.")

        category = None
        if chosen_cat_id:
            category = Category.objects.filter(id=chosen_cat_id, is_active=True).first()
        if not category and allowed_cats:
            category = allowed_cats[0]

        from .core_utils import translate_text
        title_en = translate_text(new_title)
        body_en = translate_text(new_body)
        excerpt_en = translate_text(new_excerpt)

        author = pick_default_author(ai_settings)
        article = Article(
            title=new_title,
            title_ar=new_title,
            title_en=title_en,
            slug=generate_slug_for_title(new_title),
            body=new_body,
            body_ar=new_body,
            body_en=body_en,
            excerpt=new_excerpt,
            excerpt_ar=new_excerpt,
            excerpt_en=excerpt_en,
            author=author,
            category=category,
            cover_image_alt=image_alt,
            status='draft',
            published_at=timezone.now(),
            is_featured=False,
            is_breaking=False,
            auto_translate=False
        )
        attach_default_cover_image(article, 'gold')
        article.save()

        tag_names = (ai_tags if ai_tags else ([category.name] if category else [])) + wp_site.get_site_tags_list()
        published_url = None
        wp_error_detail = None
        try:
            published_url = push_article_to_wordpress(
                wp_site, article, extra_tag_names=tag_names,
                focus_keyword=focus_keyword, meta_description=meta_description, wp_category_id=wp_category_id
            )
        except Exception as wpe:
            logger.error(f"Error syndicating gold price article to WP site {wp_site.name}: {wpe}")
            wp_error_detail = str(wpe)

        AIImportLog.objects.create(
            source=None,
            article=article,
            wp_site=wp_site,
            source_url=GOLD_SPOT_API_URL,
            published_url=published_url or '',
            title=new_title,
            status='success' if published_url else 'failed',
            error_message='' if published_url else (wp_error_detail or 'فشل النشر على ووردبريس'),
            wp_category_id=wp_category_id,
            wp_category_name='أسعار',
            focus_keyword=focus_keyword,
            tag_names=','.join(tag_names) if tag_names else '',
            input_tokens=ai_usage.get('input_tokens'),
            output_tokens=ai_usage.get('output_tokens'),
        )
        return bool(published_url)
    except Exception as ex:
        logger.error(f"Failed to generate gold price article for {wp_site.name}: {ex}")
        AIImportLog.objects.create(
            source=None,
            source_url=GOLD_SPOT_API_URL,
            wp_site=wp_site,
            title="تحديث سعر الذهب",
            status='failed',
            error_message=f"فشل صياغة خبر سعر الذهب لـ {wp_site.name}: {str(ex)}",
            input_tokens=ai_usage.get('input_tokens'),
            output_tokens=ai_usage.get('output_tokens'),
        )
        return False


def generate_silver_price_article_for_site(wp_site, silver_data, comparison_text, ai_settings, api_key, allowed_cats, categories_list_str, wp_category_id=None):
    """
    Writes and publishes a fresh silver-price article to a single WordPress site,
    using the exact real numbers in silver_data rather than any AI-invented figures.
    Returns True on a successful publish, False otherwise.
    """
    if wp_site.use_rich_formatting:
        body_format_instruction = f"محتوى الخبر الكامل مقسماً بأسلوب متوافق مع السيو (SEO): {HEADING_STRUCTURE_INSTRUCTION}"
    else:
        body_format_instruction = "محتوى الخبر الكامل بالتنسيق الصحفي مقسماً إلى فقرات باستخدام وسوم HTML للفقرات <p>...</p> حصراً."

    internal_link_instruction = ""
    if wp_site.use_internal_links:
        candidate_posts = fetch_recent_wp_posts(wp_site)
        if candidate_posts:
            links_list_str = "\n".join([f"- {p['title']}: {p['link']}" for p in candidate_posts])
            internal_link_instruction = (
                f"\nإن أمكن بشكل طبيعي، ضمّن رابطاً داخلياً واحداً أو رابطين على الأكثر باستخدام وسم "
                f"<a href=\"...\">نص الرابط</a> داخل فقرات الخبر، يشيران فقط إلى أحد الروابط التالية "
                f"لمقالات أخرى على نفس الموقع (لا تخترع أي رابط جديد، استخدم الروابط أدناه حرفياً):\n{links_list_str}"
            )

    comparison_line = f"\n{comparison_text}" if comparison_text else "\nلا تتوفر بيانات مقارنة بتحديث سابق - لا تذكر أي مقارنة أو نسبة تغيير في هذه الحالة."

    prompt = (
        f"بصفتك محررًا اقتصاديًا محترفًا باللغة العربية، اكتب خبرًا صحفيًا محدَّثًا عن سعر الفضة اليوم في مصر، "
        f"معتمداً حصرياً على الأرقام الحقيقية التالية المأخوذة من السوق العالمية لحظة كتابة الخبر - "
        f"اذكرها كما هي تماماً دون تقريب أو اختراع أي رقم بديل:\n"
        f"- سعر أوقية الفضة عالمياً: {silver_data['spot_usd_per_oz']} دولار أمريكي\n"
        f"- سعر صرف الدولار: {silver_data['usd_to_egp']} جنيه مصري\n"
        f"- سعر جرام الفضة الخالصة (عيار 999): {silver_data['price_999_egp']} جنيه مصري\n"
        f"- سعر جرام الفضة (عيار 925 -- إسترليني): {silver_data['price_925_egp']} جنيه مصري"
        f"{comparison_line}\n\n"
        f"الرجاء الالتزام التام بالتعليمات التالية:\n"
        f"1. اكتب بأسلوب صحفي اقتصادي مباشر وواضح، بين 250 و400 كلمة. {READABILITY_INSTRUCTION}\n"
        f"2. {STRONG_TITLE_INSTRUCTION}\n"
        f"3. اكتب ملخصًا قصيرًا وموجزًا للخبر (Excerpt) مكون من سطرين إلى ثلاثة أسطر.\n"
        f"4. أضف في نهاية الخبر فقرة قصيرة بعنوان \"نظرة عامة على السوق\" تصف الاتجاه العام لحركة الفضة عالمياً "
        f"بصياغة عامة ومتحفظة (مثل تأثير سعر الصرف أو حركة السوق العالمي)، على أن تنتهي الفقرة حرفياً بجملة توضيحية "
        f"مشابهة لـ: \"هذه قراءة عامة لحركة السوق ولا تُعد توصية استثمارية.\" لا تذكر أي أرقام أو مستويات أو نسب "
        f"مستقبلية مختلَقة، فقط وصف عام للاتجاه.\n"
        f"5. {build_seo_keyphrase_instruction(wp_site.use_rich_formatting)}\n"
        f"6. {IMAGE_ALT_INSTRUCTION}\n"
        f"7. قم بإرجاع الإجابة بتنسيق JSON حصريًا دون أي علامات markdown أو علامات برمجية إضافية مثل ```json. "
        f"يجب أن يكون ملف الـ JSON يحتوي على المفاتيح التالية تماماً باللغة الإنجليزية:\n"
        f"- \"title\": عنوان الخبر\n"
        f"- \"excerpt\": ملخص الخبر\n"
        f"- \"body\": {body_format_instruction}\n"
        f"- \"category_id\": الرقم التعريفي (ID) للقسم المختار من القائمة المتاحة أدناه.\n"
        f"- \"focus_keyword\": عبارة مفتاحية قصيرة (2-4 كلمات) تلخص موضوع الخبر، لاستخدامها في تحليل السيو (SEO).\n"
        f"- \"meta_description\": وصف تعريفي (Meta Description) لمحركات البحث لا يتجاوز 155 حرفاً.\n"
        f"- \"image_alt\": النص البديل لصورة الغلاف كما هو موضح أعلاه.\n"
        f"- \"tags\": قائمة (array) من 3 إلى 5 وسوم؛ يجب أن يكون كل وسم مرتبطاً مباشرة بمحتوى هذا الخبر تحديداً "
        f"(وليس عاماً)، وأن يكون عبارة بحثية واقعية يستخدمها القارئ فعلاً عند البحث في جوجل عن هذا الموضوع بالذات "
        f"(مثال: \"سعر الفضة اليوم\"، \"سعر جرام الفضة في مصر\").\n\n"
        f"8. اختر القسم الأنسب لهذا الخبر من قائمة الأقسام المتاحة التالية حصرياً:\n{categories_list_str}\n"
        f"{internal_link_instruction}"
    )

    ai_response, ai_usage = call_gemini_api(prompt, api_key=api_key)
    if not ai_response:
        AIImportLog.objects.create(
            source=None,
            source_url=SILVER_SPOT_API_URL,
            wp_site=wp_site,
            title="تحديث سعر الفضة",
            status='failed',
            error_message="لم يستجب الـ API الخاص بـ Gemini أو فشل استخراج النص."
        )
        return False

    try:
        cleaned_response = ai_response.strip()
        if cleaned_response.startswith("```json"):
            cleaned_response = cleaned_response[7:]
        if cleaned_response.endswith("```"):
            cleaned_response = cleaned_response[:-3]
        cleaned_response = cleaned_response.strip()

        data = json.loads(cleaned_response)
        new_title = sanitize_ai_text(data.get("title", "").strip())
        new_excerpt = sanitize_ai_text(data.get("excerpt", "").strip())
        new_body = sanitize_ai_body(
            data.get("body", "").strip(),
            allow_headings=wp_site.use_rich_formatting,
            allow_links=wp_site.use_internal_links,
            link_base_url=wp_site.url,
        )
        if wp_site.use_rich_formatting:
            new_body = apply_heading_color(new_body, wp_site.heading_color)
        focus_keyword = sanitize_ai_text(data.get("focus_keyword", "").strip())
        meta_description = sanitize_ai_text(data.get("meta_description", "").strip())
        image_alt = sanitize_ai_text(data.get("image_alt", "").strip())
        raw_tags = data.get("tags") or []
        if not isinstance(raw_tags, list):
            raw_tags = []
        ai_tags = [sanitize_ai_text(str(t).strip()) for t in raw_tags[:5] if str(t).strip()]

        try:
            chosen_cat_id = int(data.get("category_id"))
        except (ValueError, TypeError):
            chosen_cat_id = None

        if not new_title or not new_body:
            raise ValueError("بيانات العنوان أو المحتوى فارغة.")

        category = None
        if chosen_cat_id:
            category = Category.objects.filter(id=chosen_cat_id, is_active=True).first()
        if not category and allowed_cats:
            category = allowed_cats[0]

        from .core_utils import translate_text
        title_en = translate_text(new_title)
        body_en = translate_text(new_body)
        excerpt_en = translate_text(new_excerpt)

        author = pick_default_author(ai_settings)
        article = Article(
            title=new_title,
            title_ar=new_title,
            title_en=title_en,
            slug=generate_slug_for_title(new_title),
            body=new_body,
            body_ar=new_body,
            body_en=body_en,
            excerpt=new_excerpt,
            excerpt_ar=new_excerpt,
            excerpt_en=excerpt_en,
            author=author,
            category=category,
            cover_image_alt=image_alt,
            status='draft',
            published_at=timezone.now(),
            is_featured=False,
            is_breaking=False,
            auto_translate=False
        )
        attach_default_cover_image(article, 'silver')
        article.save()

        tag_names = (ai_tags if ai_tags else ([category.name] if category else [])) + wp_site.get_site_tags_list()
        published_url = None
        wp_error_detail = None
        try:
            published_url = push_article_to_wordpress(
                wp_site, article, extra_tag_names=tag_names,
                focus_keyword=focus_keyword, meta_description=meta_description, wp_category_id=wp_category_id
            )
        except Exception as wpe:
            logger.error(f"Error syndicating silver price article to WP site {wp_site.name}: {wpe}")
            wp_error_detail = str(wpe)

        AIImportLog.objects.create(
            source=None,
            article=article,
            wp_site=wp_site,
            source_url=SILVER_SPOT_API_URL,
            published_url=published_url or '',
            title=new_title,
            status='success' if published_url else 'failed',
            error_message='' if published_url else (wp_error_detail or 'فشل النشر على ووردبريس'),
            wp_category_id=wp_category_id,
            wp_category_name='أسعار',
            focus_keyword=focus_keyword,
            tag_names=','.join(tag_names) if tag_names else '',
            input_tokens=ai_usage.get('input_tokens'),
            output_tokens=ai_usage.get('output_tokens'),
        )
        return bool(published_url)
    except Exception as ex:
        logger.error(f"Failed to generate silver price article for {wp_site.name}: {ex}")
        AIImportLog.objects.create(
            source=None,
            source_url=SILVER_SPOT_API_URL,
            wp_site=wp_site,
            title="تحديث سعر الفضة",
            status='failed',
            error_message=f"فشل صياغة خبر سعر الفضة لـ {wp_site.name}: {str(ex)}",
            input_tokens=ai_usage.get('input_tokens'),
            output_tokens=ai_usage.get('output_tokens'),
        )
        return False


def generate_dollar_price_article_for_site(wp_site, dollar_data, comparison_text, ai_settings, api_key, allowed_cats, categories_list_str, wp_category_id=None):
    """
    Writes and publishes a fresh dollar-exchange-rate article to a single WordPress
    site, using the exact real number in dollar_data rather than any AI-invented figure.
    Returns True on a successful publish, False otherwise.
    """
    if wp_site.use_rich_formatting:
        body_format_instruction = f"محتوى الخبر الكامل مقسماً بأسلوب متوافق مع السيو (SEO): {HEADING_STRUCTURE_INSTRUCTION}"
    else:
        body_format_instruction = "محتوى الخبر الكامل بالتنسيق الصحفي مقسماً إلى فقرات باستخدام وسوم HTML للفقرات <p>...</p> حصراً."

    internal_link_instruction = ""
    if wp_site.use_internal_links:
        candidate_posts = fetch_recent_wp_posts(wp_site)
        if candidate_posts:
            links_list_str = "\n".join([f"- {p['title']}: {p['link']}" for p in candidate_posts])
            internal_link_instruction = (
                f"\nإن أمكن بشكل طبيعي، ضمّن رابطاً داخلياً واحداً أو رابطين على الأكثر باستخدام وسم "
                f"<a href=\"...\">نص الرابط</a> داخل فقرات الخبر، يشيران فقط إلى أحد الروابط التالية "
                f"لمقالات أخرى على نفس الموقع (لا تخترع أي رابط جديد، استخدم الروابط أدناه حرفياً):\n{links_list_str}"
            )

    comparison_line = f"\n{comparison_text}" if comparison_text else "\nلا تتوفر بيانات مقارنة بتحديث سابق - لا تذكر أي مقارنة أو نسبة تغيير في هذه الحالة."

    prompt = (
        f"بصفتك محررًا اقتصاديًا محترفًا باللغة العربية، اكتب خبرًا صحفيًا محدَّثًا عن سعر صرف الدولار اليوم في مصر، "
        f"معتمداً حصرياً على الرقم الحقيقي التالي المأخوذ من السوق لحظة كتابة الخبر - "
        f"اذكره كما هو تماماً دون تقريب أو اختراع رقم بديل:\n"
        f"- سعر صرف الدولار الأمريكي: {dollar_data['usd_to_egp']} جنيه مصري"
        f"{comparison_line}\n\n"
        f"الرجاء الالتزام التام بالتعليمات التالية:\n"
        f"1. اكتب بأسلوب صحفي اقتصادي مباشر وواضح، بين 200 و350 كلمة. {READABILITY_INSTRUCTION}\n"
        f"2. {STRONG_TITLE_INSTRUCTION}\n"
        f"3. اكتب ملخصًا قصيرًا وموجزًا للخبر (Excerpt) مكون من سطرين إلى ثلاثة أسطر.\n"
        f"4. أضف في نهاية الخبر فقرة قصيرة بعنوان \"نظرة عامة على السوق\" تصف الاتجاه العام لحركة سعر الصرف "
        f"بصياغة عامة ومتحفظة، على أن تنتهي الفقرة حرفياً بجملة توضيحية مشابهة لـ: \"هذه قراءة عامة لحركة السوق "
        f"ولا تُعد توصية استثمارية.\" لا تذكر أي أرقام أو مستويات أو نسب مستقبلية مختلَقة، فقط وصف عام للاتجاه.\n"
        f"5. {build_seo_keyphrase_instruction(wp_site.use_rich_formatting)}\n"
        f"6. {IMAGE_ALT_INSTRUCTION}\n"
        f"7. قم بإرجاع الإجابة بتنسيق JSON حصريًا دون أي علامات markdown أو علامات برمجية إضافية مثل ```json. "
        f"يجب أن يكون ملف الـ JSON يحتوي على المفاتيح التالية تماماً باللغة الإنجليزية:\n"
        f"- \"title\": عنوان الخبر\n"
        f"- \"excerpt\": ملخص الخبر\n"
        f"- \"body\": {body_format_instruction}\n"
        f"- \"category_id\": الرقم التعريفي (ID) للقسم المختار من القائمة المتاحة أدناه.\n"
        f"- \"focus_keyword\": عبارة مفتاحية قصيرة (2-4 كلمات) تلخص موضوع الخبر، لاستخدامها في تحليل السيو (SEO).\n"
        f"- \"meta_description\": وصف تعريفي (Meta Description) لمحركات البحث لا يتجاوز 155 حرفاً.\n"
        f"- \"image_alt\": النص البديل لصورة الغلاف كما هو موضح أعلاه.\n"
        f"- \"tags\": قائمة (array) من 3 إلى 5 وسوم؛ يجب أن يكون كل وسم مرتبطاً مباشرة بمحتوى هذا الخبر تحديداً "
        f"(وليس عاماً)، وأن يكون عبارة بحثية واقعية يستخدمها القارئ فعلاً عند البحث في جوجل عن هذا الموضوع بالذات "
        f"(مثال: \"سعر الدولار اليوم\"، \"سعر الدولار مقابل الجنيه المصري\").\n\n"
        f"8. اختر القسم الأنسب لهذا الخبر من قائمة الأقسام المتاحة التالية حصرياً:\n{categories_list_str}\n"
        f"{internal_link_instruction}"
    )

    ai_response, ai_usage = call_gemini_api(prompt, api_key=api_key)
    if not ai_response:
        AIImportLog.objects.create(
            source=None,
            source_url=GOLD_FX_API_URL,
            wp_site=wp_site,
            title="تحديث سعر الدولار",
            status='failed',
            error_message="لم يستجب الـ API الخاص بـ Gemini أو فشل استخراج النص."
        )
        return False

    try:
        cleaned_response = ai_response.strip()
        if cleaned_response.startswith("```json"):
            cleaned_response = cleaned_response[7:]
        if cleaned_response.endswith("```"):
            cleaned_response = cleaned_response[:-3]
        cleaned_response = cleaned_response.strip()

        data = json.loads(cleaned_response)
        new_title = sanitize_ai_text(data.get("title", "").strip())
        new_excerpt = sanitize_ai_text(data.get("excerpt", "").strip())
        new_body = sanitize_ai_body(
            data.get("body", "").strip(),
            allow_headings=wp_site.use_rich_formatting,
            allow_links=wp_site.use_internal_links,
            link_base_url=wp_site.url,
        )
        if wp_site.use_rich_formatting:
            new_body = apply_heading_color(new_body, wp_site.heading_color)
        focus_keyword = sanitize_ai_text(data.get("focus_keyword", "").strip())
        meta_description = sanitize_ai_text(data.get("meta_description", "").strip())
        image_alt = sanitize_ai_text(data.get("image_alt", "").strip())
        raw_tags = data.get("tags") or []
        if not isinstance(raw_tags, list):
            raw_tags = []
        ai_tags = [sanitize_ai_text(str(t).strip()) for t in raw_tags[:5] if str(t).strip()]

        try:
            chosen_cat_id = int(data.get("category_id"))
        except (ValueError, TypeError):
            chosen_cat_id = None

        if not new_title or not new_body:
            raise ValueError("بيانات العنوان أو المحتوى فارغة.")

        category = None
        if chosen_cat_id:
            category = Category.objects.filter(id=chosen_cat_id, is_active=True).first()
        if not category and allowed_cats:
            category = allowed_cats[0]

        from .core_utils import translate_text
        title_en = translate_text(new_title)
        body_en = translate_text(new_body)
        excerpt_en = translate_text(new_excerpt)

        author = pick_default_author(ai_settings)
        article = Article(
            title=new_title,
            title_ar=new_title,
            title_en=title_en,
            slug=generate_slug_for_title(new_title),
            body=new_body,
            body_ar=new_body,
            body_en=body_en,
            excerpt=new_excerpt,
            excerpt_ar=new_excerpt,
            excerpt_en=excerpt_en,
            author=author,
            category=category,
            cover_image_alt=image_alt,
            status='draft',
            published_at=timezone.now(),
            is_featured=False,
            is_breaking=False,
            auto_translate=False
        )
        attach_default_cover_image(article, 'dollar')
        article.save()

        tag_names = (ai_tags if ai_tags else ([category.name] if category else [])) + wp_site.get_site_tags_list()
        published_url = None
        wp_error_detail = None
        try:
            published_url = push_article_to_wordpress(
                wp_site, article, extra_tag_names=tag_names,
                focus_keyword=focus_keyword, meta_description=meta_description, wp_category_id=wp_category_id
            )
        except Exception as wpe:
            logger.error(f"Error syndicating dollar price article to WP site {wp_site.name}: {wpe}")
            wp_error_detail = str(wpe)

        AIImportLog.objects.create(
            source=None,
            article=article,
            wp_site=wp_site,
            source_url=GOLD_FX_API_URL,
            published_url=published_url or '',
            title=new_title,
            status='success' if published_url else 'failed',
            error_message='' if published_url else (wp_error_detail or 'فشل النشر على ووردبريس'),
            wp_category_id=wp_category_id,
            wp_category_name='أسعار',
            focus_keyword=focus_keyword,
            tag_names=','.join(tag_names) if tag_names else '',
            input_tokens=ai_usage.get('input_tokens'),
            output_tokens=ai_usage.get('output_tokens'),
        )
        return bool(published_url)
    except Exception as ex:
        logger.error(f"Failed to generate dollar price article for {wp_site.name}: {ex}")
        AIImportLog.objects.create(
            source=None,
            source_url=GOLD_FX_API_URL,
            wp_site=wp_site,
            title="تحديث سعر الدولار",
            status='failed',
            error_message=f"فشل صياغة خبر سعر الدولار لـ {wp_site.name}: {str(ex)}",
            input_tokens=ai_usage.get('input_tokens'),
            output_tokens=ai_usage.get('output_tokens'),
        )
        return False


def generate_regular_article_for_site(wp_site, source, item, ai_settings, api_key, allowed_cats,
                                       categories_list_str, get_wp_primary_categories):
    """
    Generates a fully unique AI-rewritten article for one WordPress site and pushes it.
    Used both for standalone sites and as the "master" generation for a merge group
    (WordPressSiteGroup) - siblings in the group reuse this result via
    reword_regular_article_for_site() instead of calling Gemini from scratch, to cut cost.

    Returns None only when nothing usable came back (API failure or JSON parse failure) -
    in that case the caller must not increment generated_count, matching legacy behavior.
    On any parsed response (even if the WP push itself failed) returns a dict with
    'published' (bool) plus everything reword_regular_article_for_site() needs to build a
    lighter, reworded sibling article without another full Gemini generation call.
    """
    if wp_site.use_rich_formatting:
        body_format_instruction = f"محتوى الخبر الكامل مقسماً بأسلوب متوافق مع السيو (SEO): {HEADING_STRUCTURE_INSTRUCTION}"
    else:
        body_format_instruction = "محتوى الخبر الكامل بالتنسيق الصحفي مقسماً إلى فقرات باستخدام وسوم HTML للفقرات <p>...</p> حصراً."

    internal_link_instruction = ""
    if wp_site.use_internal_links:
        candidate_posts = fetch_recent_wp_posts(wp_site)
        if candidate_posts:
            links_list_str = "\n".join([f"- {p['title']}: {p['link']}" for p in candidate_posts])
            internal_link_instruction = (
                f"\n7. إن أمكن بشكل طبيعي، ضمّن رابطاً داخلياً واحداً أو رابطين على الأكثر باستخدام وسم "
                f"<a href=\"...\">نص الرابط</a> داخل فقرات الخبر، يشيران فقط إلى أحد الروابط التالية "
                f"لمقالات أخرى على نفس الموقع (لا تخترع أي رابط جديد، استخدم الروابط أدناه حرفياً):\n{links_list_str}"
            )

    explainer_instruction = ""
    if wp_site.use_explainer_style:
        explainer_instruction = (
            "\n8. إذا كان هذا الخبر يتعلق بقرار تنظيمي أو رسوم أو ضرائب أو تغييرات أسعار تستحق شرحاً "
            "تفصيلياً (وليس مجرد خبر عاجل سريع)، فاختر أسلوباً تفسيرياً بدلاً من الأسلوب المعتاد: صغ "
            "العنوان كسؤال يعكس جوهر الموضوع، وقسّم محتوى الخبر إلى عناوين فرعية على شكل أسئلة فرعية "
            "(مثل: لماذا...؟ هل...؟ ما حجم/تأثير...؟ كيف...؟) باتباع نفس ترتيب مستويات العناوين "
            "الموضح أعلاه (أول عنوان فرعي <h2>، والذي يليه <h3>، وهكذا)، بحيث يجيب كل قسم عن سؤاله "
            "مباشرة، ويمكن أن يصل طول الخبر في هذه الحالة حتى 800 كلمة متجاوزاً الحد المذكور في "
            "التعليمة الثانية. أما إذا كان الخبر عاجلاً أو حدثياً عادياً لا يحتاج شرحاً، فاتبع التنسيق "
            "المعتاد القصير."
        )

    # Prefer the site's real WordPress categories (as configured in the plugin's
    # own admin UI) so Gemini picks a category that actually exists on this site
    # directly - no more relying on Django's fragile name-based category_mapping.
    # Falls back to the shared local categories list for sites on an older plugin
    # version without this endpoint.
    site_primary_cats = get_wp_primary_categories(wp_site)
    if site_primary_cats:
        site_categories_list_str = "\n".join([f"- {c['id']}: {c['name']}" for c in site_primary_cats])
    else:
        site_categories_list_str = categories_list_str

    prompt = (
        f"بصفتك محررًا صحفيًا محترفًا باللغة العربية، يرجى كتابة خبر صحفي جديد ومصاغ بأسلوبك الخاص بالكامل "
        f"استناداً إلى المعلومات والخبر التالي (المصدر الأصلي غير مذكور هنا عمداً - لا تحاول تخمينه أو "
        f"ذكر أي جهة إعلامية):\n"
        f"عنوان الخبر الأصلي: {item['title']}\n"
        f"تفاصيل الخبر: {item['description']}\n\n"
        f"الرجاء الالتزام التام بالتعليمات التالية:\n"
        f"1. اكتب الخبر باللغة العربية الفصحى وبأسلوب صحفي متميز وجذاب ومحايد. {READABILITY_INSTRUCTION}\n"
        f"2. يجب أن لا يزيد حجم الخبر الإجمالي عن {ai_settings.max_words} كلمة إطلاقاً (تأكد أن يتراوح طول الخبر بين 300 إلى 450 كلمة كحد أقصى لتفادي الإطالة).\n"
        f"3. اكتب عنواناً مختلفاً تماماً عن العنوان الأصلي بصياغتك الخاصة. {STRONG_TITLE_INSTRUCTION}\n"
        f"4. اكتب ملخصًا قصيرًا وموجزًا للخبر (Excerpt) مكون من سطرين إلى ثلاثة أسطر.\n"
        f"5. {build_seo_keyphrase_instruction(wp_site.use_rich_formatting)}\n"
        f"6. {IMAGE_ALT_INSTRUCTION}\n"
        f"7. قم بإرجاع الإجابة بتنسيق JSON حصريًا دون أي علامات markdown أو علامات برمجية إضافية مثل ```json. "
        f"يجب أن يكون ملف الـ JSON يحتوي على المفاتيح التالية تماماً باللغة الإنجليزية:\n"
        f"- \"title\": عنوان الخبر الجديد\n"
        f"- \"excerpt\": ملخص الخبر\n"
        f"- \"body\": {body_format_instruction}\n"
        f"- \"category_id\": الرقم التعريفي (ID) للقسم المختار من القائمة المتاحة أدناه.\n"
        f"- \"focus_keyword\": عبارة مفتاحية قصيرة (2-4 كلمات) تلخص موضوع الخبر الأساسي، لاستخدامها في تحليل السيو (SEO).\n"
        f"- \"meta_description\": وصف تعريفي (Meta Description) لمحركات البحث لا يتجاوز 155 حرفاً، يتضمن العبارة المفتاحية أعلاه.\n"
        f"- \"image_alt\": النص البديل لصورة الغلاف كما هو موضح أعلاه.\n"
        f"- \"tags\": قائمة (array) من 3 إلى 5 وسوم؛ يجب أن يكون كل وسم مرتبطاً مباشرة بمحتوى هذا "
        f"الخبر تحديداً (وليس عاماً)، وأن يكون عبارة بحثية واقعية يستخدمها القارئ فعلاً عند البحث في "
        f"جوجل عن هذا الموضوع بالذات (مثال لخبر عن سعر اليورو: \"سعر اليورو اليوم\"، \"اليورو مقابل "
        f"الجنيه\")، بدون ذكر اسم أي موقع إخباري.\n\n"
        f"8. اختر القسم الأنسب لموضوع الخبر من قائمة الأقسام المتاحة التالية حصرياً:\n{site_categories_list_str}\n"
        f"9. {NO_SOURCE_NAME_INSTRUCTION}\n"
        f"{internal_link_instruction}"
        f"{explainer_instruction}\n\n"
        f"هام جداً: صغ هذا الخبر بصياغة فريدة ومختلفة تماماً عن أي صياغات سابقة، باستخدام هيكل ومترادفات مختلفة لموقع الويب المحدد: {wp_site.name}."
    )

    ai_response, ai_usage = call_gemini_api(prompt, api_key=api_key)
    if not ai_response:
        return None

    try:
        cleaned_response = ai_response.strip()
        if cleaned_response.startswith("```json"):
            cleaned_response = cleaned_response[7:]
        if cleaned_response.endswith("```"):
            cleaned_response = cleaned_response[:-3]
        cleaned_response = cleaned_response.strip()

        data = json.loads(cleaned_response)
        new_title = sanitize_ai_text(data.get("title", "").strip())
        new_excerpt = sanitize_ai_text(data.get("excerpt", "").strip())
        new_body = sanitize_ai_body(
            data.get("body", "").strip(),
            allow_headings=wp_site.use_rich_formatting or wp_site.use_explainer_style,
            allow_links=wp_site.use_internal_links,
            link_base_url=wp_site.url,
        )
        if wp_site.use_rich_formatting:
            new_body = apply_heading_color(new_body, wp_site.heading_color)
        focus_keyword = sanitize_ai_text(data.get("focus_keyword", "").strip())
        meta_description = sanitize_ai_text(data.get("meta_description", "").strip())
        image_alt = sanitize_ai_text(data.get("image_alt", "").strip())
        raw_tags = data.get("tags") or []
        if not isinstance(raw_tags, list):
            raw_tags = []
        ai_tags = [sanitize_ai_text(str(t).strip()) for t in raw_tags[:5] if str(t).strip()]
        try:
            chosen_cat_id = int(data.get("category_id"))
        except (ValueError, TypeError):
            chosen_cat_id = None

        if not new_title or not new_body:
            raise ValueError("بيانات العنوان أو المحتوى فارغة.")
        if title_contains_source_name(new_title, source.name):
            raise ValueError("العنوان يحتوي على اسم المصدر.")
        if title_contains_source_name(new_body, source.name):
            raise ValueError("محتوى الخبر يحتوي على اسم المصدر.")

        wp_category_id_for_push = None
        category_name_for_group = ''
        if site_primary_cats:
            # chosen_cat_id is a real WP category id here (Gemini picked from
            # site_categories_list_str above) - use it directly, falling back to
            # the site's first primary category if the id doesn't match one we offered.
            chosen_cat = next((c for c in site_primary_cats if c['id'] == chosen_cat_id), None) if chosen_cat_id else None
            if not chosen_cat:
                chosen_cat = site_primary_cats[0]
            wp_category_id_for_push = chosen_cat['id']
            category_name_for_group = chosen_cat['name']
            # The local Article record still needs *a* local category (never
            # shown publicly - these WP-bound articles are saved as local drafts only).
            category = allowed_cats[0] if allowed_cats else None
        else:
            category = None
            if chosen_cat_id:
                category = Category.objects.filter(id=chosen_cat_id, is_active=True).first()
            if not category and allowed_cats:
                category = allowed_cats[0]
            category_name_for_group = category.name if category else ''

        from .core_utils import translate_text
        title_en = translate_text(new_title)
        body_en = translate_text(new_body)
        excerpt_en = translate_text(new_excerpt)

        author = pick_default_author(ai_settings)
        article = Article(
            title=new_title,
            title_ar=new_title,
            title_en=title_en,
            slug=generate_slug_for_title(new_title),
            body=new_body,
            body_ar=new_body,
            body_en=body_en,
            excerpt=new_excerpt,
            excerpt_ar=new_excerpt,
            excerpt_en=excerpt_en,
            author=author,
            category=category,
            cover_image_alt=image_alt,
            status='draft',
            published_at=timezone.now(),
            is_featured=False,
            is_breaking=False,
            auto_translate=False
        )
        img_file = fetch_image_file(item['image_url']) if item.get('image_url') else None
        if not img_file:
            from .core_utils import translate_text
            translated_title = translate_text(new_title)
            commons_url = _find_topical_image(translated_title, translated_title)
            img_file = fetch_image_file(commons_url) if commons_url else None
        if img_file:
            article.cover_image = img_file
        else:
            attach_default_cover_image(article, 'general_news')

        article.save()

        # Push this unique version to this specific WP site
        published_url = None
        wp_error_detail = None
        try:
            tag_names = (ai_tags if ai_tags else ([category.name] if category else [])) + wp_site.get_site_tags_list()
            published_url = push_article_to_wordpress(
                wp_site, article, extra_tag_names=tag_names,
                focus_keyword=focus_keyword, meta_description=meta_description,
                wp_category_id=wp_category_id_for_push
            )
        except Exception as wpe:
            logger.error(f"Error syndicating to WP site {wp_site.name}: {wpe}")
            wp_error_detail = str(wpe)

        AIImportLog.objects.create(
            source=source,
            article=article,
            wp_site=wp_site,
            source_url=item['link'],
            published_url=published_url or '',
            title=new_title,
            status='success' if published_url else 'failed',
            error_message='' if published_url else (wp_error_detail or 'فشل النشر على ووردبريس'),
            wp_category_id=wp_category_id_for_push,
            wp_category_name=category_name_for_group,
            focus_keyword=focus_keyword,
            tag_names=','.join(tag_names) if tag_names else '',
            input_tokens=ai_usage.get('input_tokens'),
            output_tokens=ai_usage.get('output_tokens'),
        )
        return {
            'published': bool(published_url),
            'title': new_title,
            'body': new_body,
            'excerpt': new_excerpt,
            'tags': ai_tags,
            'local_category': category,
            'category_name': category_name_for_group,
            'focus_keyword': focus_keyword,
            'meta_description': meta_description,
            'image_alt': image_alt,
        }
    except Exception as ex:
        logger.error(f"Failed to generate unique WP article: {ex}")
        AIImportLog.objects.create(
            source=source,
            source_url=item['link'],
            title=item['title'],
            status='failed',
            error_message=f"فشل صياغة فريدة للووردبريس {wp_site.name}: {str(ex)}",
            input_tokens=ai_usage.get('input_tokens'),
            output_tokens=ai_usage.get('output_tokens'),
        )
        return None


def reword_regular_article_for_site(wp_site, source, item, master, ai_settings, api_key,
                                     get_wp_primary_categories, get_wp_category_id):
    """
    Lighter-weight sibling of generate_regular_article_for_site(): reuses an
    already-generated "master" article's category/tags/SEO fields (chosen once per
    merge group, via a WordPressSiteGroup, to cut Gemini calls) and only asks Gemini
    to reword the title/body/excerpt for this specific site in a distinct style -
    every site in a group still publishes its own unique wording, only the research/
    categorization work is shared to reduce cost (a partial, not full, cost reduction).
    """
    if wp_site.use_rich_formatting:
        body_format_instruction = f"محتوى الخبر الكامل مقسماً بأسلوب متوافق مع السيو (SEO): {HEADING_STRUCTURE_INSTRUCTION}"
    else:
        body_format_instruction = "محتوى الخبر الكامل بالتنسيق الصحفي مقسماً إلى فقرات باستخدام وسوم HTML للفقرات <p>...</p> حصراً."

    master_plain_text = BeautifulSoup(master['body'], 'html.parser').get_text(separator=' ', strip=True)

    prompt = (
        f"بصفتك محررًا صحفيًا محترفًا باللغة العربية، لديك خبر صحفي جاهز، والمطلوب منك إعادة صياغته بالكامل "
        f"بأسلوب مختلف تماماً (مفردات وتراكيب جديدة) ليُنشر على موقع مختلف، مع الحفاظ التام على المعنى والمعلومات "
        f"والحقائق كما هي دون أي إضافة أو حذف:\n"
        f"العنوان الحالي: {master['title']}\n"
        f"نص الخبر الحالي: {master_plain_text}\n\n"
        f"الرجاء الالتزام التام بالتعليمات التالية:\n"
        f"1. أعد صياغة الخبر بالكامل بأسلوب صحفي متميز وجذاب ومحايد يختلف تماماً عن الصياغة الحالية من حيث "
        f"المفردات وترتيب الجمل، مع الحفاظ على نفس المعنى والمعلومات تماماً. {READABILITY_INSTRUCTION}\n"
        f"2. اكتب عنواناً بصياغة مختلفة تماماً عن العنوان الحالي يحمل نفس المعنى. {STRONG_TITLE_INSTRUCTION}\n"
        f"3. اكتب ملخصًا قصيرًا وموجزًا للخبر (Excerpt) مكون من سطرين إلى ثلاثة أسطر بصياغة جديدة.\n"
        f"4. قم بإرجاع الإجابة بتنسيق JSON حصريًا دون أي علامات markdown أو علامات برمجية إضافية مثل ```json. "
        f"يجب أن يكون ملف الـ JSON يحتوي على المفاتيح التالية تماماً باللغة الإنجليزية:\n"
        f"- \"title\": العنوان الجديد بالصياغة المختلفة\n"
        f"- \"excerpt\": الملخص الجديد\n"
        f"- \"body\": {body_format_instruction}\n\n"
        f"5. {NO_SOURCE_NAME_INSTRUCTION} إن كان النص الأصلي أعلاه يذكر اسم أي موقع أو وكالة إخبارية بالخطأ، "
        f"احذف الإشارة إليها تماماً في الصياغة الجديدة دون الإخلال بالمعنى.\n\n"
        f"هام جداً: يجب أن تكون الصياغة فريدة تماماً ومختلفة عن النص الأصلي أعلاه لموقع الويب المحدد: {wp_site.name}، "
        f"مع عدم تغيير أي معلومة أو رقم أو حقيقة واردة في النص الأصلي."
    )

    ai_response, ai_usage = call_gemini_api(prompt, api_key=api_key)
    if not ai_response:
        return None

    try:
        cleaned_response = ai_response.strip()
        if cleaned_response.startswith("```json"):
            cleaned_response = cleaned_response[7:]
        if cleaned_response.endswith("```"):
            cleaned_response = cleaned_response[:-3]
        cleaned_response = cleaned_response.strip()

        data = json.loads(cleaned_response)
        new_title = sanitize_ai_text(data.get("title", "").strip())
        new_excerpt = sanitize_ai_text(data.get("excerpt", "").strip())
        new_body = sanitize_ai_body(
            data.get("body", "").strip(),
            allow_headings=wp_site.use_rich_formatting or wp_site.use_explainer_style,
            allow_links=False,
            link_base_url=wp_site.url,
        )
        if wp_site.use_rich_formatting:
            new_body = apply_heading_color(new_body, wp_site.heading_color)

        if not new_title or not new_body:
            raise ValueError("بيانات العنوان أو المحتوى فارغة.")
        if title_contains_source_name(new_title, source.name):
            raise ValueError("العنوان يحتوي على اسم المصدر.")
        if title_contains_source_name(new_body, source.name):
            raise ValueError("محتوى الخبر يحتوي على اسم المصدر.")

        site_primary_cats = get_wp_primary_categories(wp_site)
        wp_category_id_for_push = None
        if site_primary_cats:
            wp_category_id_for_push = get_wp_category_id(wp_site, master['category_name'])
            if wp_category_id_for_push is None:
                wp_category_id_for_push = site_primary_cats[0]['id']
        category = master['local_category']

        from .core_utils import translate_text
        title_en = translate_text(new_title)
        body_en = translate_text(new_body)
        excerpt_en = translate_text(new_excerpt)

        author = pick_default_author(ai_settings)
        article = Article(
            title=new_title,
            title_ar=new_title,
            title_en=title_en,
            slug=generate_slug_for_title(new_title),
            body=new_body,
            body_ar=new_body,
            body_en=body_en,
            excerpt=new_excerpt,
            excerpt_ar=new_excerpt,
            excerpt_en=excerpt_en,
            author=author,
            category=category,
            cover_image_alt=master.get('image_alt', ''),
            status='draft',
            published_at=timezone.now(),
            is_featured=False,
            is_breaking=False,
            auto_translate=False
        )
        img_file = fetch_image_file(item['image_url']) if item.get('image_url') else None
        if not img_file:
            from .core_utils import translate_text
            translated_title = translate_text(new_title)
            commons_url = _find_topical_image(translated_title, translated_title)
            img_file = fetch_image_file(commons_url) if commons_url else None
        if img_file:
            article.cover_image = img_file
        else:
            attach_default_cover_image(article, 'general_news')

        article.save()

        published_url = None
        wp_error_detail = None
        try:
            tag_names = (master['tags'] if master['tags'] else ([category.name] if category else [])) + wp_site.get_site_tags_list()
            published_url = push_article_to_wordpress(
                wp_site, article, extra_tag_names=tag_names,
                focus_keyword=master['focus_keyword'], meta_description=master['meta_description'],
                wp_category_id=wp_category_id_for_push
            )
        except Exception as wpe:
            logger.error(f"Error syndicating reworded article to WP site {wp_site.name}: {wpe}")
            wp_error_detail = str(wpe)

        AIImportLog.objects.create(
            source=source,
            article=article,
            wp_site=wp_site,
            source_url=item['link'],
            published_url=published_url or '',
            title=new_title,
            status='success' if published_url else 'failed',
            error_message='' if published_url else (wp_error_detail or 'فشل النشر على ووردبريس'),
            wp_category_id=wp_category_id_for_push,
            wp_category_name=master['category_name'],
            focus_keyword=master['focus_keyword'],
            tag_names=','.join(tag_names) if tag_names else '',
            input_tokens=ai_usage.get('input_tokens'),
            output_tokens=ai_usage.get('output_tokens'),
        )
        return {'published': bool(published_url)}
    except Exception as ex:
        logger.error(f"Failed to reword article for {wp_site.name}: {ex}")
        AIImportLog.objects.create(
            source=source,
            source_url=item['link'],
            title=item['title'],
            status='failed',
            error_message=f"فشل إعادة صياغة الخبر لـ {wp_site.name}: {str(ex)}",
            input_tokens=ai_usage.get('input_tokens'),
            output_tokens=ai_usage.get('output_tokens'),
        )
        return None


def get_today_total_cost():
    """
    Sum of AIImportLog.estimated_cost across every site/source for today,
    used by the daily cost cap below. Includes failed rows deliberately - a
    failed WordPress push after a paid Gemini call still cost real money
    (see the AIImportLog.wp_site cost incident this cap exists to guard
    against), so excluding failures would undercount the real spend.
    """
    today_start = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)
    total = AIImportLog.objects.filter(created_at__gte=today_start).aggregate(total=Sum('estimated_cost'))['total']
    return total or Decimal('0')


def send_telegram_alert(text):
    """Best-effort one-shot Telegram notification to every configured allowed chat - never raises."""
    ai_settings = AISettings.get_settings()
    bot_token = ai_settings.telegram_bot_token
    chat_ids = [c.strip() for c in (ai_settings.telegram_allowed_chats or '').split(',') if c.strip()]
    if not bot_token or not chat_ids:
        return
    for chat_id in chat_ids:
        try:
            requests.post(
                f"https://api.telegram.org/bot{bot_token}/sendMessage",
                json={'chat_id': chat_id, 'text': text},
                timeout=10,
            )
        except Exception as e:
            logger.warning(f"Failed to send Telegram cost-cap alert to {chat_id}: {e}")


def is_daily_cost_cap_exceeded(ai_settings):
    """
    Hard circuit breaker independent of any specific bug: if today's real
    total spend (across every site/source, success or failed) has reached
    the configured daily_cost_limit_usd, every further generation this
    cycle - and every cycle for the rest of the day - is skipped outright,
    regardless of cause. Sends one Telegram alert the first time the cap
    trips each day (deduped via cost_cap_alert_sent_date) rather than
    spamming one every 10-minute cycle. No-op (returns False) when no cap
    is configured.
    """
    if not ai_settings.daily_cost_limit_usd:
        return False

    today = timezone.now().date()
    total_today = get_today_total_cost()
    if total_today < ai_settings.daily_cost_limit_usd:
        return False

    if ai_settings.cost_cap_alert_sent_date != today:
        send_telegram_alert(
            f"⚠️ تم الوصول للحد الأقصى اليومي لتكلفة الذكاء الاصطناعي "
            f"(${total_today:.2f} من ${ai_settings.daily_cost_limit_usd}). "
            f"تم إيقاف التوليد التلقائي حتى بداية اليوم التالي."
        )
        ai_settings.cost_cap_alert_sent_date = today
        ai_settings.save(update_fields=['cost_cap_alert_sent_date'])

    return True


def run_ai_generation_cycle(target_site_id=None):
    """
    Executes a complete AI generation cycle:
    1. Reads active settings.
    2. Identifies news categories that need updates.
    3. Fetches the source news items.
    4. Filters out duplicate news items.
    5. Calls Gemini API to rewrite articles.
    6. Downloads cover images.
    7. Saves the synthesized articles.

    `target_site_id`: optional. When set, this is a manual "generate now"
    trigger for one specific WordPressSite (see TriggerSiteScraperView) -
    the cycle only considers that site (never the local/main site, and never
    any other WordPress site), and bypasses schedule-slot timing / legacy
    cooldown gates for it so content is generated immediately regardless of
    the configured schedule. It still respects that site's own daily_limit
    and articles_per_run caps, and the global ai_settings.articles_per_day
    quota, and never marks schedule bookkeeping as run - so the site's normal
    automatic schedule is left completely undisturbed.
    """
    ai_settings = AISettings.get_settings()
    if not ai_settings.is_active:
        logger.info("AI Generation system is inactive.")
        return 0

    if is_daily_cost_cap_exceeded(ai_settings):
        logger.warning("Daily AI cost cap reached - skipping this generation cycle entirely.")
        return 0

    api_key = ai_settings.gemini_api_key or getattr(settings, 'GEMINI_API_KEY', None)
    if not api_key:
        logger.error("Gemini API key is not configured. Aborting run.")
        return 0

    # Iterate every active source. local_sources (if configured) only restricts which
    # sources are eligible to publish to the main local site (checked per-source below) -
    # it must not hide sources that are only linked to a WordPress site.
    local_sources_qs = ai_settings.local_sources.filter(is_active=True)
    local_sources_restricted = local_sources_qs.exists()
    local_source_ids = set(local_sources_qs.values_list('id', flat=True)) if local_sources_restricted else None
    sources = AISource.objects.filter(is_active=True)
    if not sources.exists():
        logger.warning("No active AI sources configured.")
        return 0

    # Get allowed categories for classification
    allowed_cats = list(ai_settings.categories.filter(is_active=True))
    if not allowed_cats:
        allowed_cats = list(Category.objects.filter(is_active=True))
        
    categories_list_str = "\n".join([f"- {c.id}: {c.name}" for c in allowed_cats])
    
    limit = ai_settings.articles_per_day
    generated_count = 0

    # Track how many articles each WordPress site has already received today,
    # so its per-site daily_limit is honored on top of the global limit.
    today_start = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)
    wp_site_counts = {
        row['wp_site']: row['count']
        for row in AIImportLog.objects.filter(
            status='success', wp_site__isnull=False, created_at__gte=today_start
        ).values('wp_site').annotate(count=Count('id'))
    }
    # Separate from wp_site_counts (today's total): tracks how many articles this
    # specific cycle invocation has generated per site, capped by articles_per_run.
    wp_site_run_counts = {}

    # Precompute this cycle's regular-news (RSS/Trends) cap per WP site once, so
    # it stays consistent across every source/item processed in this cycle. Sites
    # with schedule slots configured only get regular news when a "regular" slot
    # is due right now (Cairo time), capped by that slot's own count; sites with
    # no slots keep the legacy fixed articles_per_run cap on every cycle run.
    regular_news_caps = {}
    regular_due_slots = {}
    _regular_cap_sites = WordPressSite.objects.filter(is_active=True)
    if target_site_id:
        _regular_cap_sites = _regular_cap_sites.filter(id=target_site_id)
    for _site in _regular_cap_sites:
        _cap, _due_slot = get_regular_news_run_cap(_site, force=bool(target_site_id))
        regular_news_caps[_site.id] = _cap
        if _due_slot:
            regular_due_slots[_site.id] = _due_slot

    # Real WP category ids (from the plugin's own primary-categories endpoint)
    # cached per site for this cycle, so multiple price-article types for the
    # same site don't refetch. get_wp_category_id() returns None when the
    # plugin doesn't expose the endpoint yet (older version) or the named
    # category isn't configured as primary there - callers fall back to the
    # legacy Django category_mapping behavior in that case.
    wp_primary_categories_cache = {}

    def get_wp_primary_categories(wp_site):
        if wp_site.id not in wp_primary_categories_cache:
            wp_primary_categories_cache[wp_site.id] = fetch_wp_primary_categories(wp_site)
        return wp_primary_categories_cache[wp_site.id]

    def get_wp_category_id(wp_site, category_name):
        match = next(
            (c for c in get_wp_primary_categories(wp_site) if c['name'].strip() == category_name),
            None,
        )
        return match['id'] if match else None

    # Loop over all active sources
    for source in sources:
        if generated_count >= limit:
            break
        if is_daily_cost_cap_exceeded(ai_settings):
            logger.warning("Daily AI cost cap reached mid-cycle - stopping the rest of this run.")
            break

        items = fetch_news_items_from_source(source.url)
        if not items:
            continue
            
        # Get WordPress sites mapped to this source
        _wp_sites_qs = WordPressSite.objects.filter(is_active=True, sources=source)
        if target_site_id:
            _wp_sites_qs = _wp_sites_qs.filter(id=target_site_id)
        wp_sites = list(_wp_sites_qs)
        
        for item in items:
            if generated_count >= limit:
                break
                
            # Check duplicate
            if AIImportLog.objects.filter(source_url=item['link'], status='success').exists():
                continue
            if Article.all_objects.filter(slug=generate_slug_for_title(item['title'])).exists():
                continue
            # Gold/silver/dollar price news comes exclusively from the dedicated
            # live gold-price generator, never from the RSS rewrite pipeline.
            if is_excluded_price_topic(item['title'], item.get('description')):
                continue

            # Fetch full text to ensure AI has enough useful info
            try:
                if item.get('link'):
                    full_text = fetch_full_article_text(item['link'])
                    if full_text and len(full_text) > len(item.get('description', '')):
                        item['description'] = full_text
            except Exception as e:
                pass

            source_allowed_for_local = not local_sources_restricted or source.id in local_source_ids
            if ai_settings.publish_to_main_site and source_allowed_for_local and not target_site_id:
                # Always generate and publish locally first (Case 1)
                prompt = (
                    f"بصفتك محررًا صحفيًا محترفًا باللغة العربية، يرجى كتابة خبر صحفي جديد ومصاغ بأسلوبك الخاص بالكامل "
                    f"استناداً إلى المعلومات والخبر التالي (المصدر الأصلي غير مذكور هنا عمداً - لا تحاول تخمينه "
                    f"أو ذكر أي جهة إعلامية):\n"
                    f"عنوان الخبر الأصلي: {item['title']}\n"
                    f"تفاصيل الخبر: {item['description']}\n\n"
                    f"الرجاء الالتزام التام بالتعليمات التالية:\n"
                    f"1. اكتب الخبر باللغة العربية الفصحى وبأسلوب صحفي متميز وجذاب ومحايد. {READABILITY_INSTRUCTION}\n"
                    f"2. يجب أن لا يزيد حجم الخبر الإجمالي عن {ai_settings.max_words} كلمة إطلاقاً (تأكد أن يتراوح طول الخبر بين 300 إلى 450 كلمة كحد أقصى لتفادي الإطالة).\n"
                    f"3. اكتب عنواناً مختلفاً تماماً عن العنوان الأصلي بصياغتك الخاصة. {STRONG_TITLE_INSTRUCTION}\n"
                    f"4. اكتب ملخصًا قصيرًا وموجزًا للخبر (Excerpt) مكون من سطرين إلى ثلاثة أسطر.\n"
                    f"5. {IMAGE_ALT_INSTRUCTION}\n"
                    f"6. قم بإرجاع الإجابة بتنسيق JSON حصريًا دون أي علامات markdown أو علامات برمجية إضافية مثل ```json. "
                    f"يجب أن يكون ملف الـ JSON يحتوي على المفاتيح التالية تماماً باللغة الإنجليزية:\n"
                    f"- \"title\": عنوان الخبر الجديد\n"
                    f"- \"excerpt\": ملخص الخبر\n"
                    f"- \"body\": محتوى الخبر الكامل بالتنسيق الصحفي مقسماً إلى فقرات باستخدام وسوم HTML للفقرات <p>...</p> حصراً.\n"
                    f"- \"category_id\": الرقم التعريفي (ID) للقسم المختار من القائمة المتاحة أدناه.\n"
                    f"- \"image_alt\": النص البديل لصورة الغلاف كما هو موضح أعلاه.\n\n"
                    f"7. اختر القسم الأنسب لموضوع الخبر من قائمة الأقسام المتاحة التالية حصرياً:\n{categories_list_str}\n"
                    f"8. {NO_SOURCE_NAME_INSTRUCTION}"
                )
                
                ai_response, ai_usage = call_gemini_api(prompt, api_key=api_key)
                if not ai_response:
                    AIImportLog.objects.create(
                        source=source,
                        source_url=item['link'],
                        title=item['title'],
                        status='failed',
                        error_message="لم يستجب الـ API الخاص بـ Gemini أو فشل استخراج النص."
                    )
                    continue
                    
                try:
                    cleaned_response = ai_response.strip()
                    if cleaned_response.startswith("```json"):
                        cleaned_response = cleaned_response[7:]
                    if cleaned_response.endswith("```"):
                        cleaned_response = cleaned_response[:-3]
                    cleaned_response = cleaned_response.strip()
                    
                    data = json.loads(cleaned_response)
                    new_title = sanitize_ai_text(data.get("title", "").strip())
                    new_excerpt = sanitize_ai_text(data.get("excerpt", "").strip())
                    new_body = sanitize_ai_body(data.get("body", "").strip())
                    image_alt = sanitize_ai_text(data.get("image_alt", "").strip())
                    try:
                        chosen_cat_id = int(data.get("category_id"))
                    except (ValueError, TypeError):
                        chosen_cat_id = None

                    if not new_title or not new_body:
                        raise ValueError("بيانات العنوان أو المحتوى فارغة في استجابة الذكاء الاصطناعي.")
                    if title_contains_source_name(new_title, source.name):
                        raise ValueError("العنوان يحتوي على اسم المصدر.")

                    category = None
                    if chosen_cat_id:
                        category = Category.objects.filter(id=chosen_cat_id, is_active=True).first()
                    if not category and allowed_cats:
                        category = allowed_cats[0]

                    from .core_utils import translate_text
                    title_en = translate_text(new_title)
                    body_en = translate_text(new_body)
                    excerpt_en = translate_text(new_excerpt)

                    author = pick_default_author(ai_settings)
                    article = Article(
                        title=new_title,
                        title_ar=new_title,
                        title_en=title_en,
                        slug=generate_slug_for_title(new_title),
                        body=new_body,
                        body_ar=new_body,
                        body_en=body_en,
                        excerpt=new_excerpt,
                        excerpt_ar=new_excerpt,
                        excerpt_en=excerpt_en,
                        author=author,
                        category=category,
                        cover_image_alt=image_alt,
                        status='published',
                        published_at=timezone.now(),
                        is_featured=False,
                        is_breaking=False,
                        auto_translate=False
                    )
                    img_file = fetch_image_file(item['image_url']) if item.get('image_url') else None
                    if not img_file:
                        commons_url = _find_topical_image(title_en, title_en)
                        img_file = fetch_image_file(commons_url) if commons_url else None
                    if img_file:
                        article.cover_image = img_file
                    else:
                        attach_default_cover_image(article, 'general_news')

                    article.save()
                    
                    AIImportLog.objects.create(
                        source=source,
                        article=article,
                        wp_site=None,
                        source_url=item['link'],
                        published_url=article.get_absolute_url() if article else '',
                        title=new_title,
                        status='success',
                        input_tokens=ai_usage.get('input_tokens'),
                        output_tokens=ai_usage.get('output_tokens'),
                    )

                    generated_count += 1
                except Exception as ex:
                    logger.error(f"Failed to parse/save local article: {ex}")
                    AIImportLog.objects.create(
                        source=source,
                        source_url=item['link'],
                        title=item['title'],
                        status='failed',
                        error_message=f"فشل معالجة استجابة الـ JSON: {str(ex)}",
                        input_tokens=ai_usage.get('input_tokens'),
                        output_tokens=ai_usage.get('output_tokens'),
                    )
            
            # Case 2: External WordPress sites connected!
            # Generate a unique article for each site - unless several sites are
            # linked to the same active WordPressSiteGroup (merge group), in which
            # case only one "master" article is generated via Gemini and the rest
            # of the group gets a cheaper reword pass instead of a full generation,
            # to reduce cost while each site still publishes its own unique wording.
            if wp_sites:
                standalone_sites = []
                grouped_sites = {}
                for wp_site in wp_sites:
                    if generated_count >= limit:
                        break
                    if wp_site_counts.get(wp_site.id, 0) >= wp_site.daily_limit or wp_site_run_counts.get(wp_site.id, 0) >= regular_news_caps.get(wp_site.id, 0):
                        continue
                    group = wp_site.merge_group
                    if group and group.is_active:
                        grouped_sites.setdefault(group.id, []).append(wp_site)
                    else:
                        standalone_sites.append(wp_site)

                def _apply_publish_result(wp_site, result):
                    nonlocal generated_count
                    if result is None:
                        return
                    if result['published']:
                        wp_site_counts[wp_site.id] = wp_site_counts.get(wp_site.id, 0) + 1
                        wp_site_run_counts[wp_site.id] = wp_site_run_counts.get(wp_site.id, 0) + 1
                        if wp_site.id in regular_due_slots:
                            mark_slot_run(regular_due_slots[wp_site.id], 'regular')
                    generated_count += 1

                for wp_site in standalone_sites:
                    if generated_count >= limit:
                        break
                    result = generate_regular_article_for_site(
                        wp_site, source, item, ai_settings, api_key, allowed_cats,
                        categories_list_str, get_wp_primary_categories,
                    )
                    _apply_publish_result(wp_site, result)

                for group_sites in grouped_sites.values():
                    if generated_count >= limit:
                        break
                    master_site = group_sites[0]
                    master_result = generate_regular_article_for_site(
                        master_site, source, item, ai_settings, api_key, allowed_cats,
                        categories_list_str, get_wp_primary_categories,
                    )
                    _apply_publish_result(master_site, master_result)
                    if master_result is None:
                        continue

                    for wp_site in group_sites[1:]:
                        if generated_count >= limit:
                            break
                        reword_result = reword_regular_article_for_site(
                            wp_site, source, item, master_result, ai_settings, api_key,
                            get_wp_primary_categories, get_wp_category_id,
                        )
                        _apply_publish_result(wp_site, reword_result)

    # Live gold price articles: independent of RSS sources. Sites with no
    # schedule slots keep firing every cycle (legacy behavior); sites with
    # slots only fire when a "gold" slot is due right now (Cairo time).
    gold_price_sites, gold_due_slots, _ = sites_due_for_type('gold', 'generate_gold_price_articles', force_site_id=target_site_id)
    if gold_price_sites:
        gold_data = fetch_live_gold_prices()
        if gold_data:
            comparison_text = ""
            if ai_settings.last_gold_price_24k_egp:
                diff = gold_data['price_24k_egp'] - ai_settings.last_gold_price_24k_egp
                if abs(diff) >= 0.5:
                    direction = "ارتفع" if diff > 0 else "تراجع"
                    comparison_text = (
                        f"- مقارنة حقيقية بآخر تحديث مسجَّل: {direction} سعر جرام الذهب عيار 24 بمقدار "
                        f"{abs(round(diff, 2))} جنيه مصري (اذكر هذه المقارنة بدقة كما هي)."
                    )
            ai_settings.last_gold_price_24k_egp = gold_data['price_24k_egp']
            ai_settings.last_gold_price_at = gold_data['timestamp']
            ai_settings.save(update_fields=['last_gold_price_24k_egp', 'last_gold_price_at'])

            for wp_site in gold_price_sites:
                if generated_count >= limit:
                    break
                if wp_site_counts.get(wp_site.id, 0) >= wp_site.daily_limit:
                    continue
                if wp_site.id not in gold_due_slots and wp_site_run_counts.get(wp_site.id, 0) >= wp_site.articles_per_run:
                    continue
                success = generate_gold_price_article_for_site(
                    wp_site, gold_data, comparison_text, ai_settings, api_key, allowed_cats, categories_list_str,
                    wp_category_id=get_wp_category_id(wp_site, 'أسعار')
                )
                if success:
                    wp_site_counts[wp_site.id] = wp_site_counts.get(wp_site.id, 0) + 1
                    wp_site_run_counts[wp_site.id] = wp_site_run_counts.get(wp_site.id, 0) + 1
                    generated_count += 1
                    if wp_site.id in gold_due_slots:
                        mark_slot_run(gold_due_slots[wp_site.id], 'gold')
        else:
            logger.error("Failed to fetch live gold price data; skipping gold price article generation this cycle.")

    silver_price_sites, silver_due_slots, silver_legacy_used = sites_due_for_type(
        'silver', 'generate_silver_price_articles', ai_settings, 'last_silver_price_at', force_site_id=target_site_id
    )
    if silver_price_sites:
        silver_data = fetch_live_silver_prices()
        if silver_data:
            comparison_text = ""
            if ai_settings.last_silver_price_egp:
                diff = silver_data['price_999_egp'] - ai_settings.last_silver_price_egp
                if abs(diff) >= 0.5:
                    direction = "ارتفع" if diff > 0 else "تراجع"
                    comparison_text = (
                        f"- مقارنة حقيقية بآخر تحديث مسجَّل: {direction} سعر جرام الفضة الخالصة بمقدار "
                        f"{abs(round(diff, 2))} جنيه مصري (اذكر هذه المقارنة بدقة كما هي)."
                    )
            ai_settings.last_silver_price_egp = silver_data['price_999_egp']
            if silver_legacy_used:
                ai_settings.last_silver_price_at = silver_data['timestamp']
                ai_settings.save(update_fields=['last_silver_price_egp', 'last_silver_price_at'])
            else:
                ai_settings.save(update_fields=['last_silver_price_egp'])

            for wp_site in silver_price_sites:
                if generated_count >= limit:
                    break
                if wp_site_counts.get(wp_site.id, 0) >= wp_site.daily_limit:
                    continue
                if wp_site.id not in silver_due_slots and wp_site_run_counts.get(wp_site.id, 0) >= wp_site.articles_per_run:
                    continue
                success = generate_silver_price_article_for_site(
                    wp_site, silver_data, comparison_text, ai_settings, api_key, allowed_cats, categories_list_str,
                    wp_category_id=get_wp_category_id(wp_site, 'أسعار')
                )
                if success:
                    wp_site_counts[wp_site.id] = wp_site_counts.get(wp_site.id, 0) + 1
                    wp_site_run_counts[wp_site.id] = wp_site_run_counts.get(wp_site.id, 0) + 1
                    generated_count += 1
                    if wp_site.id in silver_due_slots:
                        mark_slot_run(silver_due_slots[wp_site.id], 'silver')
        else:
            logger.error("Failed to fetch live silver price data; skipping silver price article generation this cycle.")

    dollar_price_sites, dollar_due_slots, _ = sites_due_for_type('dollar', 'generate_dollar_price_articles', force_site_id=target_site_id)
    if dollar_price_sites:
        dollar_data = fetch_live_dollar_price()
        if dollar_data:
            comparison_text = ""
            if ai_settings.last_dollar_price_egp:
                diff = dollar_data['usd_to_egp'] - ai_settings.last_dollar_price_egp
                if abs(diff) >= 0.01:
                    direction = "ارتفع" if diff > 0 else "تراجع"
                    comparison_text = (
                        f"- مقارنة حقيقية بآخر تحديث مسجَّل: {direction} سعر صرف الدولار بمقدار "
                        f"{abs(round(diff, 2))} جنيه مصري (اذكر هذه المقارنة بدقة كما هي)."
                    )
            ai_settings.last_dollar_price_egp = dollar_data['usd_to_egp']
            ai_settings.last_dollar_price_at = dollar_data['timestamp']
            ai_settings.save(update_fields=['last_dollar_price_egp', 'last_dollar_price_at'])

            for wp_site in dollar_price_sites:
                if generated_count >= limit:
                    break
                if wp_site_counts.get(wp_site.id, 0) >= wp_site.daily_limit:
                    continue
                if wp_site.id not in dollar_due_slots and wp_site_run_counts.get(wp_site.id, 0) >= wp_site.articles_per_run:
                    continue
                success = generate_dollar_price_article_for_site(
                    wp_site, dollar_data, comparison_text, ai_settings, api_key, allowed_cats, categories_list_str,
                    wp_category_id=get_wp_category_id(wp_site, 'أسعار')
                )
                if success:
                    wp_site_counts[wp_site.id] = wp_site_counts.get(wp_site.id, 0) + 1
                    wp_site_run_counts[wp_site.id] = wp_site_run_counts.get(wp_site.id, 0) + 1
                    generated_count += 1
                    if wp_site.id in dollar_due_slots:
                        mark_slot_run(dollar_due_slots[wp_site.id], 'dollar')
        else:
            logger.error("Failed to fetch live dollar price data; skipping dollar price article generation this cycle.")

    iron_sites, iron_due_slots, iron_legacy_used = sites_due_for_type(
        'iron', 'generate_iron_price_articles', ai_settings, 'last_iron_price_at', force_site_id=target_site_id
    )
    if iron_sites:
        iron_data = fetch_idsc_indicator('iron')
        iron_investment_data = fetch_idsc_indicator('iron_investment')
        if iron_data and iron_investment_data:
            if iron_legacy_used:
                ai_settings.last_iron_price_at = timezone.now()
                ai_settings.save(update_fields=['last_iron_price_at'])
            source_url = f"{IDSC_API_BASE}/PricesData/GetMainIndicatorData/{IDSC_INDICATOR_IDS['iron']}"
            iron_items = [
                ("حديد عز", iron_data),
                ("حديد إستثماري", iron_investment_data),
            ]
            for wp_site in iron_sites:
                if generated_count >= limit:
                    break
                if wp_site_counts.get(wp_site.id, 0) >= wp_site.daily_limit:
                    continue
                if wp_site.id not in iron_due_slots and wp_site_run_counts.get(wp_site.id, 0) >= wp_site.articles_per_run:
                    continue
                success = generate_official_commodity_article_for_site(
                    wp_site, "أسعار الحديد (عز واستثماري)", iron_items, source_url,
                    ai_settings, api_key, allowed_cats, categories_list_str, content_type='iron',
                    wp_category_id=get_wp_category_id(wp_site, 'أسعار')
                )
                if success:
                    wp_site_counts[wp_site.id] = wp_site_counts.get(wp_site.id, 0) + 1
                    wp_site_run_counts[wp_site.id] = wp_site_run_counts.get(wp_site.id, 0) + 1
                    generated_count += 1
                    if wp_site.id in iron_due_slots:
                        mark_slot_run(iron_due_slots[wp_site.id], 'iron')
        else:
            logger.error("Failed to fetch official iron price data; skipping this cycle.")

    cement_sites, cement_due_slots, cement_legacy_used = sites_due_for_type(
        'cement', 'generate_cement_price_articles', ai_settings, 'last_cement_price_at', force_site_id=target_site_id
    )
    if cement_sites:
        cement_data = fetch_idsc_indicator('cement')
        if cement_data:
            if cement_legacy_used:
                ai_settings.last_cement_price_at = timezone.now()
                ai_settings.save(update_fields=['last_cement_price_at'])
            source_url = f"{IDSC_API_BASE}/PricesData/GetMainIndicatorData/{IDSC_INDICATOR_IDS['cement']}"
            for wp_site in cement_sites:
                if generated_count >= limit:
                    break
                if wp_site_counts.get(wp_site.id, 0) >= wp_site.daily_limit:
                    continue
                if wp_site.id not in cement_due_slots and wp_site_run_counts.get(wp_site.id, 0) >= wp_site.articles_per_run:
                    continue
                success = generate_official_commodity_article_for_site(
                    wp_site, "سعر الإسمنت (الرمادي)", [("الأسمنت الرمادي", cement_data)], source_url,
                    ai_settings, api_key, allowed_cats, categories_list_str, content_type='cement',
                    wp_category_id=get_wp_category_id(wp_site, 'أسعار')
                )
                if success:
                    wp_site_counts[wp_site.id] = wp_site_counts.get(wp_site.id, 0) + 1
                    wp_site_run_counts[wp_site.id] = wp_site_run_counts.get(wp_site.id, 0) + 1
                    generated_count += 1
                    if wp_site.id in cement_due_slots:
                        mark_slot_run(cement_due_slots[wp_site.id], 'cement')
        else:
            logger.error("Failed to fetch official cement price data; skipping this cycle.")

    poultry_sites, poultry_due_slots, poultry_legacy_used = sites_due_for_type(
        'poultry', 'generate_poultry_price_articles', ai_settings, 'last_poultry_price_at', force_site_id=target_site_id
    )
    if poultry_sites:
        poultry_data = fetch_idsc_indicator('poultry')
        red_meat_data = fetch_idsc_indicator('red_meat')
        if poultry_data and red_meat_data:
            if poultry_legacy_used:
                ai_settings.last_poultry_price_at = timezone.now()
                ai_settings.save(update_fields=['last_poultry_price_at'])
            source_url = f"{IDSC_API_BASE}/PricesData/GetMainIndicatorData/{IDSC_INDICATOR_IDS['poultry']}"
            poultry_items = [
                ("الدواجن الطازجة", poultry_data),
                ("اللحوم الطازجة", red_meat_data),
            ]
            for wp_site in poultry_sites:
                if generated_count >= limit:
                    break
                if wp_site_counts.get(wp_site.id, 0) >= wp_site.daily_limit:
                    continue
                if wp_site.id not in poultry_due_slots and wp_site_run_counts.get(wp_site.id, 0) >= wp_site.articles_per_run:
                    continue
                success = generate_official_commodity_article_for_site(
                    wp_site, "أسعار اللحوم والدواجن", poultry_items, source_url,
                    ai_settings, api_key, allowed_cats, categories_list_str, content_type='poultry',
                    wp_category_id=get_wp_category_id(wp_site, 'أسعار')
                )
                if success:
                    wp_site_counts[wp_site.id] = wp_site_counts.get(wp_site.id, 0) + 1
                    wp_site_run_counts[wp_site.id] = wp_site_run_counts.get(wp_site.id, 0) + 1
                    generated_count += 1
                    if wp_site.id in poultry_due_slots:
                        mark_slot_run(poultry_due_slots[wp_site.id], 'poultry')
        else:
            logger.error("Failed to fetch official poultry price data; skipping this cycle.")

    fish_sites, fish_due_slots, fish_legacy_used = sites_due_for_type(
        'fish', 'generate_fish_price_articles', ai_settings, 'last_fish_price_at', force_site_id=target_site_id
    )
    if fish_sites:
        fish_data = fetch_idsc_indicator('fish')
        fish_tilapia_data = fetch_idsc_indicator('fish_tilapia')
        fish_shrimp_data = fetch_idsc_indicator('fish_shrimp')
        fish_sardine_data = fetch_idsc_indicator('fish_sardine')
        if fish_data and fish_tilapia_data and fish_shrimp_data and fish_sardine_data:
            if fish_legacy_used:
                ai_settings.last_fish_price_at = timezone.now()
                ai_settings.save(update_fields=['last_fish_price_at'])
            source_url = f"{IDSC_API_BASE}/PricesData/GetMainIndicatorData/{IDSC_INDICATOR_IDS['fish']}"
            fish_items = [
                ("السمك (متوسط عام)", fish_data),
                ("البلطي", fish_tilapia_data),
                ("الجمبري", fish_shrimp_data),
                ("السردين المجمد", fish_sardine_data),
            ]
            for wp_site in fish_sites:
                if generated_count >= limit:
                    break
                if wp_site_counts.get(wp_site.id, 0) >= wp_site.daily_limit:
                    continue
                if wp_site.id not in fish_due_slots and wp_site_run_counts.get(wp_site.id, 0) >= wp_site.articles_per_run:
                    continue
                success = generate_official_commodity_article_for_site(
                    wp_site, "أسعار الأسماك (بلطي، جمبري، سردين)", fish_items, source_url,
                    ai_settings, api_key, allowed_cats, categories_list_str, content_type='fish',
                    wp_category_id=get_wp_category_id(wp_site, 'أسعار')
                )
                if success:
                    wp_site_counts[wp_site.id] = wp_site_counts.get(wp_site.id, 0) + 1
                    wp_site_run_counts[wp_site.id] = wp_site_run_counts.get(wp_site.id, 0) + 1
                    generated_count += 1
                    if wp_site.id in fish_due_slots:
                        mark_slot_run(fish_due_slots[wp_site.id], 'fish')
        else:
            logger.error("Failed to fetch official fish price data; skipping this cycle.")

    vegetable_sites, vegetable_due_slots, vegetable_legacy_used = sites_due_for_type(
        'vegetable', 'generate_vegetable_price_articles', ai_settings, 'last_vegetable_price_at', force_site_id=target_site_id
    )
    if vegetable_sites:
        tomatoes_data = fetch_idsc_indicator('tomatoes')
        potatoes_data = fetch_idsc_indicator('potatoes')
        onions_data = fetch_idsc_indicator('onions')
        if tomatoes_data and potatoes_data and onions_data:
            if vegetable_legacy_used:
                ai_settings.last_vegetable_price_at = timezone.now()
                ai_settings.save(update_fields=['last_vegetable_price_at'])
            source_url = f"{IDSC_API_BASE}/PricesData/GetMainIndicatorData/{IDSC_INDICATOR_IDS['tomatoes']}"
            vegetable_items = [
                ("الطماطم", tomatoes_data),
                ("البطاطس", potatoes_data),
                ("البصل", onions_data),
            ]
            for wp_site in vegetable_sites:
                if generated_count >= limit:
                    break
                if wp_site_counts.get(wp_site.id, 0) >= wp_site.daily_limit:
                    continue
                if wp_site.id not in vegetable_due_slots and wp_site_run_counts.get(wp_site.id, 0) >= wp_site.articles_per_run:
                    continue
                success = generate_official_commodity_article_for_site(
                    wp_site, "أسعار الخضار (طماطم، بطاطس، بصل)", vegetable_items, source_url,
                    ai_settings, api_key, allowed_cats, categories_list_str, content_type='vegetable',
                    wp_category_id=get_wp_category_id(wp_site, 'أسعار')
                )
                if success:
                    wp_site_counts[wp_site.id] = wp_site_counts.get(wp_site.id, 0) + 1
                    wp_site_run_counts[wp_site.id] = wp_site_run_counts.get(wp_site.id, 0) + 1
                    generated_count += 1
                    if wp_site.id in vegetable_due_slots:
                        mark_slot_run(vegetable_due_slots[wp_site.id], 'vegetable')
        else:
            logger.error("Failed to fetch official vegetable price data; skipping this cycle.")

    arab_currency_sites, arab_currency_due_slots, arab_currency_legacy_used = sites_due_for_type(
        'arab_currencies', 'generate_arab_currencies_articles', ai_settings, 'last_arab_currencies_at', force_site_id=target_site_id
    )
    if arab_currency_sites:
        currency_data = fetch_arab_currency_rates()
        if currency_data:
            if arab_currency_legacy_used:
                ai_settings.last_arab_currencies_at = timezone.now()
                ai_settings.save(update_fields=['last_arab_currencies_at'])
            source_url = f"{IDSC_API_BASE}/PricesData/GetCurrencyExchange"
            for wp_site in arab_currency_sites:
                if generated_count >= limit:
                    break
                if wp_site_counts.get(wp_site.id, 0) >= wp_site.daily_limit:
                    continue
                if wp_site.id not in arab_currency_due_slots and wp_site_run_counts.get(wp_site.id, 0) >= wp_site.articles_per_run:
                    continue
                success = generate_arab_currencies_article_for_site(
                    wp_site, currency_data, source_url, ai_settings, api_key, allowed_cats, categories_list_str,
                    wp_category_id=get_wp_category_id(wp_site, 'أسعار')
                )
                if success:
                    wp_site_counts[wp_site.id] = wp_site_counts.get(wp_site.id, 0) + 1
                    wp_site_run_counts[wp_site.id] = wp_site_run_counts.get(wp_site.id, 0) + 1
                    generated_count += 1
                    if wp_site.id in arab_currency_due_slots:
                        mark_slot_run(arab_currency_due_slots[wp_site.id], 'arab_currencies')
        else:
            logger.error("Failed to fetch official Arab currency exchange rates; skipping this cycle.")

    # Update last run timestamp
    ai_settings.last_run = timezone.now()
    ai_settings.save()
    
    return generated_count

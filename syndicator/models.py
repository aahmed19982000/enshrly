from decimal import Decimal
import uuid
from django.db import models
from django.contrib.auth.models import User
from .fields import EncryptedCharField
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from mptt.models import MPTTModel, TreeForeignKey

# Soft Delete Manager and QuerySet
class SoftDeleteQuerySet(models.QuerySet):
    def delete(self):
        return self.update(deleted_at=timezone.now())

    def hard_delete(self):
        return super().delete()

    def alive(self):
        return self.filter(deleted_at__isnull=True)

    def dead(self):
        return self.filter(deleted_at__isnull=False)

class SoftDeleteManager(models.Manager):
    def get_queryset(self):
        return SoftDeleteQuerySet(self.model, using=self._db).alive()

    def all_with_deleted(self):
        return SoftDeleteQuerySet(self.model, using=self._db)

class Category(MPTTModel):
    name = models.CharField(max_length=100, unique=True)
    slug = models.SlugField(max_length=100, unique=True, allow_unicode=True)
    icon = models.CharField(max_length=100, blank=True, null=True, help_text="CSS class name or simple icon label")
    parent = TreeForeignKey('self', on_delete=models.CASCADE, null=True, blank=True, related_name='children')
    is_active = models.BooleanField(default=True)
    order = models.PositiveIntegerField(default=0)
    color = models.CharField(max_length=20, blank=True, null=True, help_text="Hex color code or class name")

    # Meta SEO fields
    meta_title = models.CharField(max_length=255, blank=True, null=True)
    meta_description = models.TextField(blank=True, null=True)
    meta_keywords = models.CharField(max_length=255, blank=True, null=True)

    class MPTTMeta:
        order_insertion_by = ['order', 'name']

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        from django.urls import reverse
        return reverse('news_ai:index', kwargs={'slug': self.slug})

class Article(models.Model):
    STATUS_CHOICES = (
        ('draft', _('مسودة')),
        ('review', _('مراجعة')),
        ('published', _('منشور')),
        ('archived', _('مؤرشف')),
    )
    title = models.CharField(max_length=255)
    slug = models.SlugField(max_length=255, unique=True, allow_unicode=True)
    body = models.TextField()
    excerpt = models.TextField(blank=True, null=True)
    author = models.ForeignKey(User, on_delete=models.CASCADE, related_name='articles')
    category = TreeForeignKey(Category, on_delete=models.PROTECT, related_name='articles', null=True, blank=True)
    additional_categories = models.ManyToManyField(Category, related_name='additional_articles', blank=True, help_text="أقسام فرعية إضافية (اختياري)")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='published')
    published_at = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    deleted_at = models.DateTimeField(blank=True, null=True)
    
    is_featured = models.BooleanField(default=False)
    is_breaking = models.BooleanField(default=True)
    auto_translate = models.BooleanField(default=False, help_text="ترجمة تلقائية للإنجليزية (Auto-translate to English)")
    cover_image = models.ImageField(upload_to='articles/', blank=True, null=True)
    cover_image_alt = models.CharField(max_length=300, blank=True, default='', verbose_name="النص البديل للصورة (Alt Text)", help_text="وصف احترافي لمحتوى الصورة نفسها لأغراض السيو وإمكانية الوصول - وليس تكراراً لعنوان الخبر. يُترك فارغاً لاستخدام العنوان كبديل احتياطي.")
    views_count = models.PositiveIntegerField(default=0)
    read_time = models.PositiveIntegerField(default=0, help_text="Read time in minutes")
    allow_comments = models.BooleanField(default=True)
    
    # Meta SEO fields
    meta_title = models.CharField(max_length=255, blank=True, null=True)
    meta_desc = models.TextField(blank=True, null=True)

    objects = SoftDeleteManager()
    all_objects = models.Manager()

    class Meta:
        permissions = [
            ("can_publish", "Can publish articles"),
            ("can_feature", "Can feature articles"),
        ]

    def save(self, *args, **kwargs):
        if self.status == 'published' and not self.published_at:
            self.published_at = timezone.now()

        # Automatic Pillow Image Compression and WebP Conversion
        from .core_utils import optimize_image_field, translate_text, is_html_empty

        optimize_image_field(self, 'cover_image', max_size=(1200, 1200), quality=85)

        # ── Auto-populate SEO Metadata if empty ──
        from django.utils.html import strip_tags

        # Arabic SEO Fields Auto-population
        if not getattr(self, 'meta_title_ar', None) or getattr(self, 'meta_title_ar', None).strip() == "":
            title_ar_val = getattr(self, 'title_ar', None) or self.title
            if title_ar_val:
                self.meta_title_ar = title_ar_val
                if not self.meta_title:
                    self.meta_title = title_ar_val

        if not getattr(self, 'meta_desc_ar', None) or getattr(self, 'meta_desc_ar', None).strip() == "":
            excerpt_ar_val = getattr(self, 'excerpt_ar', None) or self.excerpt
            body_ar_val = getattr(self, 'body_ar', None) or self.body
            desc_val = ""
            if excerpt_ar_val and excerpt_ar_val.strip() != "":
                desc_val = strip_tags(excerpt_ar_val)
            elif body_ar_val and body_ar_val.strip() != "":
                desc_val = strip_tags(body_ar_val)
            if desc_val:
                desc_val = " ".join(desc_val.split())[:160]
                self.meta_desc_ar = desc_val
                if not self.meta_desc:
                    self.meta_desc = desc_val

        # English SEO Fields Auto-population (fallback/fallback when auto_translate is disabled)
        if not getattr(self, 'meta_title_en', None) or getattr(self, 'meta_title_en', None).strip() == "":
            title_en_val = getattr(self, 'title_en', None) or self.title
            if title_en_val:
                self.meta_title_en = title_en_val

        if not getattr(self, 'meta_desc_en', None) or getattr(self, 'meta_desc_en', None).strip() == "":
            excerpt_en_val = getattr(self, 'excerpt_en', None) or self.excerpt
            body_en_val = getattr(self, 'body_en', None) or self.body
            desc_val = ""
            if excerpt_en_val and excerpt_en_val.strip() != "":
                desc_val = strip_tags(excerpt_en_val)
            elif body_en_val and body_en_val.strip() != "":
                desc_val = strip_tags(body_en_val)
            if desc_val:
                desc_val = " ".join(desc_val.split())[:160]
                self.meta_desc_en = desc_val

        # ── Auto-translate Arabic fields → English ──
        # Only runs when the update_fields kwarg does NOT exclude these fields
        # (i.e., not triggered by a simple soft-delete update_fields=['deleted_at'])
        update_fields = kwargs.get('update_fields')
        skip_translation = update_fields is not None and 'title_en' not in update_fields

        if not skip_translation and getattr(self, 'auto_translate', True):
            # Title
            if getattr(self, 'title_ar', None):
                self.title_en = translate_text(self.title_ar)

            # Excerpt
            if getattr(self, 'excerpt_ar', None):
                self.excerpt_en = translate_text(self.excerpt_ar)

            # Body
            if getattr(self, 'body_ar', None):
                self.body_en = translate_text(self.body_ar)

            # SEO meta title
            if getattr(self, 'meta_title_ar', None):
                self.meta_title_en = translate_text(self.meta_title_ar)

            # SEO meta description
            if getattr(self, 'meta_desc_ar', None):
                self.meta_desc_en = translate_text(self.meta_desc_ar)

        super().save(*args, **kwargs)


    def delete(self, *args, **kwargs):
        self.deleted_at = timezone.now()
        self.save(update_fields=['deleted_at'])

    def hard_delete(self, *args, **kwargs):
        super().delete(*args, **kwargs)

    def __str__(self):
        return self.title

    def get_absolute_url(self):
        from django.urls import reverse
        return reverse('news_ai:index', kwargs={'slug': self.slug})

    def get_cover_image_alt(self):
        return self.cover_image_alt or self.title

class Comment(models.Model):
    article = models.ForeignKey(Article, on_delete=models.CASCADE, related_name='comments')
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='comments')
    body = models.TextField()
    parent = models.ForeignKey('self', on_delete=models.CASCADE, null=True, blank=True, related_name='replies')
    is_approved = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Comment by {self.user.username} on {self.article.title}"

class Like(models.Model):
    article = models.ForeignKey(Article, on_delete=models.CASCADE, related_name='likes')
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='likes')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('article', 'user')

    def __str__(self):
        return f"{self.user.username} liked {self.article.title}"

class Bookmark(models.Model):
    article = models.ForeignKey(Article, on_delete=models.CASCADE, related_name='bookmarks')
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='bookmarks')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('article', 'user')

    def __str__(self):
        return f"{self.user.username} bookmarked {self.article.title}"

class RelatedArticle(models.Model):
    article = models.ForeignKey(Article, on_delete=models.CASCADE, related_name='related_from')
    related_article = models.ForeignKey(Article, on_delete=models.CASCADE, related_name='related_to')
    order = models.PositiveIntegerField(default=0)

    class Meta:
        unique_together = ('article', 'related_article')
        ordering = ['order']

    def __str__(self):
        return f"{self.related_article.title} related to {self.article.title}"

# Cache Invalidation on Publish/Save/Delete
from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver
from django.core.cache import cache

@receiver(post_save, sender=Article)
@receiver(post_delete, sender=Article)
@receiver(post_save, sender=Category)
@receiver(post_delete, sender=Category)
def clear_cache_on_change(sender, **kwargs):
    try:
        cache.clear()
    except Exception:
        # Safeguard if cache connection fails in environment
        pass

from django.db.models.signals import m2m_changed

@receiver(post_save, sender=Article)
def auto_add_to_latest_news(sender, instance, **kwargs):
    if instance.status == 'published':
        try:
            latest_category = Category.objects.filter(name_ar__icontains='آخر الأخبار').first()
            if latest_category and instance.category != latest_category:
                if not instance.additional_categories.filter(pk=latest_category.pk).exists():
                    instance.additional_categories.add(latest_category)
        except Exception:
            pass

@receiver(m2m_changed, sender=Article.additional_categories.through)
def ensure_latest_news_category_on_m2m(sender, instance, action, **kwargs):
    if action in ['post_add', 'post_remove', 'post_clear']:
        if instance.status == 'published':
            try:
                latest_category = Category.objects.filter(name_ar__icontains='آخر الأخبار').first()
                if latest_category and instance.category != latest_category:
                    if not instance.additional_categories.filter(pk=latest_category.pk).exists():
                        instance.additional_categories.add(latest_category)
            except Exception:
                pass


def generate_api_token():
    import secrets
    return f"am_{secrets.token_hex(24)}"


class AISettings(models.Model):
    gemini_api_key = EncryptedCharField(max_length=500, blank=True, null=True, help_text="Gemini API Key. If empty, uses environment variable.")
    api_token = models.CharField(max_length=255, default=generate_api_token, unique=True, help_text="مفتاح الأمان للربط الآمن بالووردبريس (Django API Token).")
    telegram_bot_token = EncryptedCharField(max_length=500, blank=True, null=True, help_text="رمز توكن بوت تليجرام (Telegram Bot Token) للتحكم بالنظام.")
    telegram_allowed_chats = models.TextField(blank=True, null=True, help_text="معرفات محادثات تليجرام المسموحة، مفصولة بفاصلة (مثال: 1234567, 9876543).")
    articles_per_day = models.PositiveIntegerField(default=3, help_text="Number of articles to publish daily.")
    max_words = models.PositiveIntegerField(default=500, help_text="Max word count per article.")
    is_active = models.BooleanField(default=True, help_text="Toggle AI news fetching on or off.")
    publish_to_main_site = models.BooleanField(default=True, verbose_name="النشر على الموقع الأساسي", help_text="تفعيل أو تعطيل نشر الأخبار المولدة على الموقع الرئيسي.")
    default_authors = models.ManyToManyField(User, blank=True, related_name='ai_settings_authors', verbose_name="الكتّاب الافتراضيون للأخبار", help_text="عند اختيار أكثر من كاتب، يُنوَّع تلقائياً بينهم عشوائياً لكل خبر جديد. اتركه فارغاً لاستخدام حساب النظام الآلي.")
    categories = models.ManyToManyField(Category, blank=True, related_name='ai_settings', verbose_name="الأقسام المتاحة للنشر")
    local_sources = models.ManyToManyField('AISource', blank=True, related_name='local_ai_settings', verbose_name="مصادر الأخبار المغذية لموقع المغرب العربي", help_text="إذا تركت هذا الحقل فارغاً سيستخدم النظام جميع المصادر النشطة للنشر المحلي.")
    last_run = models.DateTimeField(blank=True, null=True)
    last_gold_price_24k_egp = models.FloatField(blank=True, null=True, verbose_name="آخر سعر مسجَّل لجرام الذهب عيار 24 (جنيه)", help_text="يُستخدم داخلياً لمقارنة سعر الذهب بالتحديث السابق.")
    last_gold_price_at = models.DateTimeField(blank=True, null=True, verbose_name="وقت آخر تسجيل لسعر الذهب")
    last_silver_price_egp = models.FloatField(blank=True, null=True, verbose_name="آخر سعر مسجَّل لجرام الفضة (جنيه)", help_text="يُستخدم داخلياً لمقارنة سعر الفضة بالتحديث السابق.")
    last_silver_price_at = models.DateTimeField(blank=True, null=True, verbose_name="وقت آخر تسجيل لسعر الفضة")
    last_dollar_price_egp = models.FloatField(blank=True, null=True, verbose_name="آخر سعر صرف مسجَّل للدولار (جنيه)", help_text="يُستخدم داخلياً لمقارنة سعر الدولار بالتحديث السابق.")
    last_dollar_price_at = models.DateTimeField(blank=True, null=True, verbose_name="وقت آخر تسجيل لسعر الدولار")
    last_iron_price_at = models.DateTimeField(blank=True, null=True, verbose_name="وقت آخر نشر لسعر الحديد", help_text="يُستخدم داخلياً لتقييد النشر لمرة واحدة يومياً.")
    last_cement_price_at = models.DateTimeField(blank=True, null=True, verbose_name="وقت آخر نشر لسعر الإسمنت", help_text="يُستخدم داخلياً لتقييد النشر لمرة واحدة يومياً.")
    last_poultry_price_at = models.DateTimeField(blank=True, null=True, verbose_name="وقت آخر نشر لسعر الدواجن", help_text="يُستخدم داخلياً لتقييد النشر لمرة واحدة يومياً.")
    last_fish_price_at = models.DateTimeField(blank=True, null=True, verbose_name="وقت آخر نشر لسعر السمك", help_text="يُستخدم داخلياً لتقييد النشر لمرة واحدة يومياً.")
    last_vegetable_price_at = models.DateTimeField(blank=True, null=True, verbose_name="وقت آخر نشر لأسعار الخضار", help_text="يُستخدم داخلياً لتقييد النشر لمرة واحدة يومياً.")
    last_arab_currencies_at = models.DateTimeField(blank=True, null=True, verbose_name="وقت آخر نشر لأسعار العملات العربية", help_text="يُستخدم داخلياً لتقييد النشر لمرة واحدة يومياً.")

    daily_cost_limit_usd = models.DecimalField(
        max_digits=6, decimal_places=2, blank=True, null=True,
        verbose_name="الحد الأقصى اليومي لتكلفة الذكاء الاصطناعي (USD)",
        help_text="عند وصول إجمالي تكلفة اليوم لهذا الرقم، يتوقف النظام تلقائياً عن أي توليد جديد "
                   "حتى بداية اليوم التالي، بغض النظر عن السبب - حماية من أي خلل غير متوقع يستهلك "
                   "الميزانية. اتركه فارغاً لتعطيل هذا الحد.",
    )
    cost_cap_alert_sent_date = models.DateField(
        blank=True, null=True, editable=False,
        verbose_name="تاريخ آخر تنبيه لوصول الحد الأقصى",
    )

    class Meta:
        verbose_name = "AI Global Settings"
        verbose_name_plural = "AI Global Settings"

    def __str__(self):
        return f"AI Settings (Active: {self.is_active}, {self.articles_per_day} daily)"

    @classmethod
    def get_settings(cls):
        obj, created = cls.objects.get_or_create(pk=1)
        return obj


class AISourceGroup(models.Model):
    name = models.CharField(max_length=100, unique=True, verbose_name="اسم المجموعة")
    description = models.TextField(blank=True, null=True, verbose_name="الوصف")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "مجموعة مصادر الأخبار"
        verbose_name_plural = "مجموعات مصادر الأخبار"

    def __str__(self):
        return self.name


class AISource(models.Model):
    LANGUAGE_CHOICES = (
        ('ar', 'عربي (Arabic)'),
        ('en', 'إنجليزي/عالمي (English/Global)'),
        ('both', 'مختلط (Mixed)'),
    )
    name = models.CharField(max_length=255, verbose_name="اسم الموقع المصدر")
    url = models.URLField(max_length=1000, unique=True, verbose_name="رابط التغذية RSS أو الموقع")
    group = models.ForeignKey(AISourceGroup, on_delete=models.SET_NULL, null=True, blank=True, related_name='sources', verbose_name="المجموعة")
    is_active = models.BooleanField(default=True, verbose_name="نشط")
    language = models.CharField(max_length=10, choices=LANGUAGE_CHOICES, default='ar', verbose_name="لغة المصدر")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "AI News Source"
        verbose_name_plural = "AI News Sources"

    def __str__(self):
        return self.name


class AIImportLog(models.Model):
    STATUS_CHOICES = (
        ('success', 'نجاح (Success)'),
        ('failed', 'فشل (Failed)'),
    )
    source = models.ForeignKey(AISource, on_delete=models.SET_NULL, null=True, related_name='logs', verbose_name="المصدر")
    article = models.ForeignKey(Article, on_delete=models.SET_NULL, null=True, blank=True, related_name='ai_logs', verbose_name="الخبر المنشور")
    wp_site = models.ForeignKey('WordPressSite', on_delete=models.SET_NULL, null=True, blank=True, related_name='import_logs', verbose_name="الموقع المستهدف")
    source_url = models.URLField(max_length=1000, verbose_name="رابط الخبر الأصلي")
    published_url = models.URLField(max_length=1000, blank=True, null=True, verbose_name="رابط الخبر المنشور")
    title = models.CharField(max_length=255, blank=True, null=True, verbose_name="عنوان الخبر")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='success')
    error_message = models.TextField(blank=True, null=True, verbose_name="رسالة الخطأ")
    wp_category_id = models.PositiveIntegerField(null=True, blank=True, editable=False, verbose_name="معرّف القسم في ووردبريس")
    wp_category_name = models.CharField(max_length=150, blank=True, default='', editable=False, verbose_name="اسم القسم في ووردبريس")
    focus_keyword = models.CharField(max_length=255, blank=True, default='', editable=False, verbose_name="الكلمة المفتاحية")
    tag_names = models.TextField(blank=True, default='', editable=False, verbose_name="الوسوم (مفصولة بفاصلة)")
    estimated_cost = models.DecimalField(max_digits=10, decimal_places=6, default=0, editable=False, verbose_name="التكلفة الفعلية (USD)")
    input_tokens = models.PositiveIntegerField(null=True, blank=True, editable=False, verbose_name="توكنات الإدخال الفعلية")
    output_tokens = models.PositiveIntegerField(null=True, blank=True, editable=False, verbose_name="توكنات الإخراج الفعلية")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "AI Import Log"
        verbose_name_plural = "AI Import Logs"
        ordering = ['-created_at']

    def _calculate_estimated_cost(self):
        """
        Computes the API cost of the Gemini request in USD, once at creation
        time and cached in estimated_cost (inputs never change afterward).
        Prefers the real token counts reported by Gemini's usageMetadata
        (set on input_tokens/output_tokens before save() by the caller) and
        only falls back to a rough word-count-based guess for older call
        sites or failures where no usage data was ever returned.
        """
        if self.status == 'failed' and self.error_message and "لم يستجب الـ API" in self.error_message:
            return Decimal('0')

        if self.input_tokens is not None and self.output_tokens is not None:
            input_tokens = self.input_tokens
            output_tokens = self.output_tokens
        else:
            # Legacy fallback estimate (no real usage data available)
            input_tokens = 1500
            if self.article_id:
                text = f"{self.article.title or ''} {self.article.excerpt or ''} {self.article.body or ''}"
                word_count = len(text.split())
                output_tokens = int(word_count * 2.2)  # Arabic words are ~2.2 tokens on Gemini
            elif self.status == 'success':
                output_tokens = 800
            else:
                output_tokens = 400

        # Gemini 2.5 Flash pricing (USD per 1M tokens)
        input_cost = (input_tokens / 1000000.0) * 0.30
        output_cost = (output_tokens / 1000000.0) * 2.50
        return Decimal(str(input_cost + output_cost))

    def save(self, *args, **kwargs):
        if self._state.adding:
            self.estimated_cost = self._calculate_estimated_cost()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.title or self.source_url} - {self.status}"



class WordPressSiteGroup(models.Model):
    """
    A syndication group of WordPress sites: when a news item qualifies for
    two or more active member sites at once, only one full ("master")
    generation call is made and the rest get a cheaper rewording pass based
    on the master's content, instead of each site paying for a full
    independent generation. Sites outside any active group keep the
    original fully-independent behavior.
    """
    name = models.CharField(max_length=255, verbose_name="اسم المجموعة")
    is_active = models.BooleanField(default=True, verbose_name="نشطة (تفعيل الدمج)", help_text="عند التعطيل، تُعامل كل المواقع الأعضاء كمواقع مستقلة كالمعتاد.")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "مجموعة دمج مواقع"
        verbose_name_plural = "مجموعات دمج المواقع"

    def __str__(self):
        return self.name


class WordPressSite(models.Model):
    name = models.CharField(max_length=255, verbose_name="اسم الموقع")
    url = models.URLField(max_length=1000, verbose_name="رابط الموقع (WordPress URL)")
    username = models.CharField(max_length=150, verbose_name="اسم المستخدم في ووردبريس")
    application_password = EncryptedCharField(max_length=500, verbose_name="كلمة مرور التطبيق (Application Password)")
    wp_author_ids = models.TextField(blank=True, default='', verbose_name="معرّفات الكتّاب في ووردبريس (Author IDs)", help_text="معرّفات (IDs) مستخدمي ووردبريس الذين سيُنسب إليهم المقال، مفصولة بفاصلة. عند وجود أكثر من واحد يُختار أحدهم عشوائياً لكل مقال. اتركه فارغاً لينشر باسم مستخدم المصادقة (username أعلاه).")
    daily_limit = models.PositiveIntegerField(default=3, verbose_name="الحد الأقصى للنشر اليومي")
    articles_per_run = models.PositiveIntegerField(default=1, verbose_name="عدد المقالات لكل تشغيل", help_text="أقصى عدد أخبار تُنشر لهذا الموقع في كل مرة تعمل فيها الدورة (يدوياً أو تلقائياً كل 4 ساعات)، بالإضافة إلى الحد الأقصى اليومي الإجمالي أعلاه.")
    is_active = models.BooleanField(default=True, verbose_name="نشط")
    merge_group = models.ForeignKey(WordPressSiteGroup, on_delete=models.SET_NULL, null=True, blank=True, related_name='sites', verbose_name="مجموعة الدمج", help_text="إذا انضم موقعان أو أكثر من نفس المجموعة النشطة لنفس الخبر، يُولَّد الخبر مرة واحدة (Master) ثم تُعاد صياغته بشكل أخف وأرخص لباقي أعضاء المجموعة بدلاً من توليد كامل ومستقل لكل موقع، لتقليل التكلفة. اتركه فارغاً ليبقى الموقع مستقلاً بالكامل بأسلوبه الخاص.")
    sources = models.ManyToManyField(AISource, related_name='wp_sites', verbose_name="مصادر الأخبار المرتبطة", blank=True)
    source_groups = models.ManyToManyField(AISourceGroup, related_name='wp_sites', verbose_name="مجموعات المصادر المرتبطة", blank=True)
    category_mapping = models.TextField(default="{}", help_text="خريطة الأقسام بتنسيق JSON. رقم واحد يُستخدم كقسم أساسي: {\"اقتصاد\": 5}. أو قسم أساسي وأقسام فرعية معاً: {\"اقتصاد\": {\"primary\": 5, \"secondary\": [12, 20]}}", verbose_name="خريطة الأقسام")
    use_rich_formatting = models.BooleanField(default=False, verbose_name="تنسيق غني بعناوين فرعية ملوّنة (SEO)", help_text="عند التفعيل، يُقسَّم الخبر إلى عناوين فرعية H2/H3 ملوّنة بدلاً من فقرات فقط، مع إضافة وسوم (Tags) تلقائية لتحسين توافق السيو (Yoast).")
    heading_color = models.CharField(max_length=7, default='#0066cc', verbose_name="لون العناوين الفرعية", help_text="كود اللون السداسي عشري (Hex)، مثال: #0066cc")
    use_internal_links = models.BooleanField(default=False, verbose_name="إضافة روابط داخلية تلقائية (SEO)", help_text="عند التفعيل، يحاول النظام تضمين رابط داخلي أو رابطين ضمن نص الخبر يشيران إلى مقالات حديثة أخرى منشورة على هذا الموقع نفسه.")
    generate_gold_price_articles = models.BooleanField(default=False, verbose_name="توليد مقالات سعر الذهب الحية", help_text="عند التفعيل، يُنشئ النظام في كل دورة توليد مقالاً جديداً بسعر الذهب اللحظي (عيار 24، 21، 18) بالجنيه المصري لهذا الموقع فقط.")
    generate_silver_price_articles = models.BooleanField(default=False, verbose_name="توليد مقالات سعر الفضة الحية", help_text="عند التفعيل، يُنشئ النظام في كل دورة توليد مقالاً جديداً بسعر الفضة اللحظي بالجنيه المصري لهذا الموقع فقط.")
    generate_dollar_price_articles = models.BooleanField(default=False, verbose_name="توليد مقالات سعر الدولار الحية", help_text="عند التفعيل، يُنشئ النظام في كل دورة توليد مقالاً جديداً بسعر صرف الدولار اللحظي مقابل الجنيه المصري لهذا الموقع فقط.")
    generate_iron_price_articles = models.BooleanField(default=False, verbose_name="توليد مقالات سعر الحديد اليومية", help_text="عند التفعيل، يُنشئ النظام مرة واحدة يومياً مقالاً بسعر حديد عز الرسمي (بيانات مركز معلومات مجلس الوزراء) لهذا الموقع فقط.")
    generate_cement_price_articles = models.BooleanField(default=False, verbose_name="توليد مقالات سعر الإسمنت اليومية", help_text="عند التفعيل، يُنشئ النظام مرة واحدة يومياً مقالاً بسعر الأسمنت الرمادي الرسمي لهذا الموقع فقط.")
    generate_poultry_price_articles = models.BooleanField(default=False, verbose_name="توليد مقالات سعر الدواجن اليومية", help_text="عند التفعيل، يُنشئ النظام مرة واحدة يومياً مقالاً بسعر الدواجن الطازجة الرسمي لهذا الموقع فقط.")
    generate_fish_price_articles = models.BooleanField(default=False, verbose_name="توليد مقالات سعر السمك اليومية", help_text="عند التفعيل، يُنشئ النظام مرة واحدة يومياً مقالاً بسعر السمك الرسمي لهذا الموقع فقط.")
    generate_vegetable_price_articles = models.BooleanField(default=False, verbose_name="توليد مقالات أسعار الخضار اليومية", help_text="عند التفعيل، يُنشئ النظام مرة واحدة يومياً مقالاً بأسعار سلة خضار أساسية (طماطم، بطاطس، بصل) الرسمية لهذا الموقع فقط.")
    generate_arab_currencies_articles = models.BooleanField(default=False, verbose_name="توليد مقالات أسعار العملات العربية والأجنبية اليومية", help_text="عند التفعيل، يُنشئ النظام مرة واحدة يومياً مقالاً بأسعار صرف الريال السعودي والدينار الكويتي والدرهم الإماراتي، بالإضافة لليورو والجنيه الاسترليني والفرنك السويسري، الرسمية مقابل الجنيه المصري لهذا الموقع فقط.")
    site_tags = models.TextField(blank=True, default='', verbose_name="وسوم ثابتة لهذا الموقع", help_text="وسوم ثابتة (افصل بينها بفاصلة) تُضاف تلقائياً لكل خبر يُنشر على هذا الموقع، مثال: بانكرز توداي, موقع بانكرز توداي الاخباري")
    use_explainer_style = models.BooleanField(default=False, verbose_name="أسلوب تفسيري (Explainer) عند الحاجة", help_text="عند التفعيل، يقرر الذكاء الاصطناعي تلقائياً استخدام أسلوب شرح بعناوين على شكل أسئلة (لماذا؟ هل؟ كيف؟) للأخبار الاقتصادية/التنظيمية (رسوم، ضرائب، قرارات) التي تحتاج تفصيلاً، بدلاً من الخبر القصير المعتاد.")

    # --- Social share image generation (Facebook cards) ---
    SOCIAL_TEMPLATE_CHOICES = [
        ('bottom_banner', 'شريط سفلي (صورة كاملة + شريط عنوان أسفل)'),
        ('boxed_card', 'بطاقة مؤطرة (صورة مع إطار وصندوق عنوان)'),
        ('split_block', 'تقسيم علوي/سفلي (صورة أعلى وكتلة لون بالعنوان أسفل)'),
    ]
    social_image_enabled = models.BooleanField(default=False, verbose_name="تفعيل توليد صور السوشال ميديا", help_text="عند التفعيل، يُولَّد تصميم صورة تلقائياً (صورة الخبر + عنوان + لوجو الموقع) عند نشر كل خبر جديد لهذا الموقع.")
    social_template = models.CharField(max_length=20, choices=SOCIAL_TEMPLATE_CHOICES, default='bottom_banner', verbose_name="قالب تصميم الصورة")
    social_logo = models.ImageField(upload_to='site_logos/', blank=True, null=True, verbose_name="لوجو الموقع", help_text="يُفضَّل صورة PNG بخلفية شفافة.")
    social_primary_color = models.CharField(max_length=7, default='#0d9488', verbose_name="اللون الأساسي للتصميم", help_text="كود اللون السداسي عشري (Hex)، مثال: #0d9488")
    social_secondary_color = models.CharField(max_length=7, default='#0f172a', verbose_name="اللون الثانوي للتصميم", help_text="كود اللون السداسي عشري (Hex)، مثال: #0f172a")
    facebook_page_id = models.CharField(max_length=100, blank=True, default='', verbose_name="معرّف صفحة فيسبوك (Page ID)", help_text="اتركه فارغاً لتعطيل النشر التلقائي على فيسبوك مع إبقاء توليد الصورة فقط.")
    facebook_access_token = EncryptedCharField(max_length=1000, blank=True, null=True, verbose_name="توكن وصول صفحة فيسبوك (Page Access Token)", help_text="توكن وصول دائم (Long-Lived Page Access Token) يُنشأ يدوياً من أدوات مطوري فيسبوك لهذه الصفحة تحديداً.")

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "WordPress Site"
        verbose_name_plural = "WordPress Sites"

    def __str__(self):
        return f"{self.name} ({self.url})"

    def get_category_mappings(self):
        import json
        try:
            return json.loads(self.category_mapping)
        except Exception:
            return {}

    def get_site_tags_list(self):
        return [t.strip() for t in self.site_tags.split(',') if t.strip()]

    def get_wp_author_ids_list(self):
        ids = []
        for part in self.wp_author_ids.split(','):
            part = part.strip()
            if part.isdigit():
                ids.append(int(part))
        return ids

    @property
    def facebook_auto_publish_enabled(self):
        return bool(self.facebook_page_id and self.facebook_access_token)


class SocialSharePost(models.Model):
    STATUS_CHOICES = [
        ('generated', 'تم توليد الصورة'),
        ('posted', 'نُشرت على فيسبوك'),
        ('failed', 'فشل'),
    ]
    wp_site = models.ForeignKey(WordPressSite, on_delete=models.CASCADE, related_name='social_posts', verbose_name="الموقع")
    article = models.ForeignKey('Article', on_delete=models.SET_NULL, null=True, blank=True, related_name='social_posts', verbose_name="الخبر")
    article_title = models.CharField(max_length=255, blank=True, default='', verbose_name="عنوان الخبر (وقت التوليد)")
    template_used = models.CharField(max_length=20, choices=WordPressSite.SOCIAL_TEMPLATE_CHOICES, verbose_name="القالب المستخدم")
    generated_image = models.ImageField(upload_to='social_shares/%Y/%m/', blank=True, null=True, verbose_name="الصورة المولَّدة")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='generated', verbose_name="الحالة")
    facebook_post_id = models.CharField(max_length=100, blank=True, default='', verbose_name="معرّف منشور فيسبوك")
    error_message = models.TextField(blank=True, default='', verbose_name="رسالة الخطأ")
    created_at = models.DateTimeField(auto_now_add=True)
    posted_at = models.DateTimeField(null=True, blank=True, verbose_name="وقت النشر على فيسبوك")

    class Meta:
        verbose_name = "صورة سوشال ميديا"
        verbose_name_plural = "صور السوشال ميديا"
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.article_title or self.pk} - {self.wp_site.name}"


class WordPressScheduleSlot(models.Model):
    CONTENT_TYPE_CHOICES = [
        ('gold', 'سعر الذهب'),
        ('silver', 'سعر الفضة'),
        ('dollar', 'سعر الدولار'),
        ('iron', 'سعر الحديد'),
        ('cement', 'سعر الإسمنت'),
        ('poultry', 'سعر الدواجن'),
        ('fish', 'سعر السمك'),
        ('vegetable', 'أسعار الخضار'),
        ('arab_currencies', 'أسعار العملات العربية والأجنبية'),
    ]

    wp_site = models.ForeignKey(WordPressSite, on_delete=models.CASCADE, related_name='schedule_slots', verbose_name="الموقع")
    time_of_day = models.TimeField(verbose_name="وقت الفترة (بتوقيت القاهرة)")
    content_types = models.TextField(verbose_name="أنواع المحتوى", help_text="مفصولة بفاصلة، مثال: iron,vegetable")
    regular_news_count = models.PositiveIntegerField(default=1, verbose_name="عدد الأخبار العامة في هذه الفترة", help_text="يُستخدم فقط إذا كانت \"أخبار عامة\" ضمن أنواع المحتوى المختارة أعلاه.")
    is_active = models.BooleanField(default=True, verbose_name="مفعّلة")
    last_run_log = models.TextField(blank=True, default='{}', verbose_name="سجل آخر تنفيذ لكل نوع", help_text="يُستخدم داخلياً لضمان تنفيذ كل نوع محتوى في هذه الفترة مرة واحدة فقط في يومه، بشكل مستقل عن باقي الأنواع في نفس الفترة.")

    class Meta:
        ordering = ['time_of_day']
        verbose_name = "فترة نشر مجدولة"
        verbose_name_plural = "فترات النشر المجدولة"

    def __str__(self):
        return f"{self.wp_site.name} - {self.time_of_day.strftime('%H:%M')}"

    def get_content_types_list(self):
        return [c.strip() for c in self.content_types.split(',') if c.strip()]

    def get_last_run_date_for_type(self, content_type):
        """Returns the ISO date string this specific content type last ran under
        this slot, or None. Tracked per-type so a multi-type slot (e.g. iron +
        cement together) runs each type independently instead of one type's
        run silently blocking the others for the rest of the day."""
        import json
        try:
            log = json.loads(self.last_run_log)
        except (ValueError, TypeError):
            log = {}
        return log.get(content_type)

    def set_last_run_date_for_type(self, content_type, date_obj):
        import json
        try:
            log = json.loads(self.last_run_log)
        except (ValueError, TypeError):
            log = {}
        log[content_type] = date_obj.isoformat()
        self.last_run_log = json.dumps(log)
        self.save(update_fields=['last_run_log'])

    def get_last_run_summary(self):
        """Human-readable 'type: date' pairs for display in the admin UI."""
        import json
        try:
            log = json.loads(self.last_run_log)
        except (ValueError, TypeError):
            log = {}
        choice_labels = dict(self.CONTENT_TYPE_CHOICES)
        return [(choice_labels.get(k, k), v) for k, v in log.items()]


class WPConnectionToken(models.Model):
    token = models.UUIDField(default=uuid.uuid4, editable=False, unique=True, verbose_name="كود الربط (Token)")
    client_name = models.CharField(max_length=255, verbose_name="اسم العميل / الموقع", help_text="لتمييز الكود ولمن يتبع")
    package_daily_limit = models.PositiveIntegerField(default=3, verbose_name="الحد اليومي للباقة المشتراة")
    is_used = models.BooleanField(default=False, verbose_name="تم الاستخدام؟")
    wp_site = models.ForeignKey(WordPressSite, on_delete=models.SET_NULL, null=True, blank=True, verbose_name="الموقع المرتبط", help_text="سيتم ملؤه تلقائياً بعد نجاح الربط")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "كود ربط ووردبريس"
        verbose_name_plural = "أكواد ربط ووردبريس"
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.client_name} - {'مستخدم' if self.is_used else 'متاح'} ({self.token})"

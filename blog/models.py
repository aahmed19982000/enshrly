from django.db import models
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify
from django_ckeditor_5.fields import CKEditor5Field


class BlogPost(models.Model):
    title = models.CharField(max_length=200, verbose_name="العنوان")
    slug = models.SlugField(max_length=220, unique=True, blank=True, allow_unicode=True, verbose_name="الرابط المختصر", help_text="يُنشأ تلقائياً من العنوان إذا تُرك فارغاً.")
    excerpt = models.TextField(max_length=300, verbose_name="مقتطف", help_text="ملخص قصير يظهر في قائمة المدونة ونتائج البحث (وكـ meta description).")
    body = CKEditor5Field(verbose_name="المحتوى", config_name='default')
    cover_image = models.ImageField(upload_to='blog_covers/', blank=True, null=True, verbose_name="صورة الغلاف")
    is_published = models.BooleanField(default=False, verbose_name="منشور")
    published_at = models.DateTimeField(default=timezone.now, verbose_name="تاريخ النشر")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-published_at']
        verbose_name = "مقالة مدونة"
        verbose_name_plural = "مقالات المدونة"

    def __str__(self):
        return self.title

    def get_absolute_url(self):
        return reverse('blog:detail', kwargs={'slug': self.slug})

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.title, allow_unicode=True)
        super().save(*args, **kwargs)

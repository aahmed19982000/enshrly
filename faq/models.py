from django.db import models


class FAQCategory(models.Model):
    name = models.CharField(max_length=100, verbose_name="اسم التصنيف")
    order = models.PositiveIntegerField(default=0, verbose_name="الترتيب")

    class Meta:
        ordering = ['order', 'id']
        verbose_name = "تصنيف أسئلة شائعة"
        verbose_name_plural = "تصنيفات الأسئلة الشائعة"

    def __str__(self):
        return self.name


class FAQItem(models.Model):
    category = models.ForeignKey(FAQCategory, on_delete=models.CASCADE, related_name='items', verbose_name="التصنيف")
    question = models.CharField(max_length=300, verbose_name="السؤال")
    answer = models.TextField(verbose_name="الإجابة")
    order = models.PositiveIntegerField(default=0, verbose_name="الترتيب")
    is_active = models.BooleanField(default=True, verbose_name="مفعّل")

    class Meta:
        ordering = ['order', 'id']
        verbose_name = "سؤال شائع"
        verbose_name_plural = "الأسئلة الشائعة"

    def __str__(self):
        return self.question

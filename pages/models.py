from django.db import models


class ContactMessage(models.Model):
    name = models.CharField(max_length=150, verbose_name='الاسم')
    contact_info = models.CharField(max_length=150, verbose_name='رقم الواتساب أو البريد الإلكتروني')
    message = models.TextField(verbose_name='الرسالة')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='تاريخ الإرسال')

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'رسالة تواصل'
        verbose_name_plural = 'رسائل التواصل'

    def __str__(self):
        return f"{self.name} — {self.created_at:%Y-%m-%d %H:%M}"

from django.db import models
from django.contrib.auth.models import User
from accounts.models import CustomerProfile
import uuid

class SubscriptionPackage(models.Model):
    name = models.CharField(max_length=100, verbose_name="اسم الباقة")
    price = models.DecimalField(max_digits=10, decimal_places=2, verbose_name="السعر (بالدولار)")
    daily_limit = models.PositiveIntegerField(verbose_name="الحد الأقصى للنشر اليومي")
    features = models.TextField(verbose_name="المميزات", help_text="كل ميزة في سطر منفصل")
    is_active = models.BooleanField(default=True, verbose_name="مفعلة")
    is_custom = models.BooleanField(default=False, verbose_name="مخصصة للشركات (تتطلب تواصل)")

    def __str__(self):
        return f"{self.name} - ${self.price}"

class Transaction(models.Model):
    STATUS_CHOICES = [
        ('pending', 'قيد الانتظار'),
        ('completed', 'مكتملة'),
        ('failed', 'فشلت'),
    ]

    GATEWAY_CHOICES = [
        ('paypal', 'PayPal'),
        ('crypto', 'عملات رقمية'),
        ('local', 'بوابة محلية'),
    ]

    transaction_id = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    customer = models.ForeignKey(CustomerProfile, on_delete=models.SET_NULL, null=True, related_name='transactions')
    package = models.ForeignKey(SubscriptionPackage, on_delete=models.SET_NULL, null=True)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    gateway = models.CharField(max_length=20, choices=GATEWAY_CHOICES, verbose_name="بوابة الدفع")
    gateway_transaction_id = models.CharField(max_length=255, blank=True, null=True, verbose_name="معرف العملية في البوابة")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.transaction_id} - {self.status}"

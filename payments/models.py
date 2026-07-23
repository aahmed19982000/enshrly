from django.db import models
from django.contrib.auth.models import User
from accounts.models import CustomerProfile
import uuid
from decimal import Decimal

class SubscriptionPackage(models.Model):
    name = models.CharField(max_length=100, verbose_name="اسم الباقة")
    price = models.DecimalField(max_digits=10, decimal_places=2, verbose_name="السعر (بالدولار)")
    daily_limit = models.PositiveIntegerField(verbose_name="الحد الأقصى للنشر اليومي")
    features = models.TextField(verbose_name="المميزات", help_text="كل ميزة في سطر منفصل")
    is_active = models.BooleanField(default=True, verbose_name="مفعلة")
    is_custom = models.BooleanField(default=False, verbose_name="مخصصة للشركات (تتطلب تواصل)")

    def __str__(self):
        return f"{self.name} - ${self.price}"

    @property
    def price_egp(self):
        from syndicator.models import AISettings
        try:
            settings = AISettings.get_settings()
            rate = settings.last_dollar_price_egp or 50.0
        except Exception:
            rate = 50.0
        return (self.price * Decimal(str(rate))).quantize(Decimal('1.00'))

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
        ('paymob', 'Paymob (بطاقة بنكية)'),
    ]

    CURRENCY_CHOICES = [
        ('USD', 'دولار أمريكي (USD)'),
        ('EGP', 'جنيه مصري (EGP)'),
    ]

    transaction_id = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    customer = models.ForeignKey(CustomerProfile, on_delete=models.SET_NULL, null=True, related_name='transactions')
    package = models.ForeignKey(SubscriptionPackage, on_delete=models.SET_NULL, null=True)
    amount = models.DecimalField(max_digits=10, decimal_places=2, verbose_name="المبلغ المدفوع")
    currency = models.CharField(max_length=10, choices=CURRENCY_CHOICES, default='USD', verbose_name="العملة")
    gateway = models.CharField(max_length=20, choices=GATEWAY_CHOICES, verbose_name="بوابة الدفع")
    gateway_transaction_id = models.CharField(max_length=255, blank=True, null=True, verbose_name="معرف العملية في البوابة")
    sender_phone = models.CharField(max_length=20, blank=True, null=True, verbose_name="رقم المرسل (المحفظة)")
    verified_transaction_id = models.CharField(max_length=255, blank=True, null=True, unique=True, verbose_name="رقم العملية الموثق")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.transaction_id} - {self.amount} {self.currency} - {self.status}"



from django.db import models
from django.contrib.auth.models import User
from django.core.validators import MaxValueValidator
from accounts.models import CustomerProfile
import uuid
from decimal import Decimal

class PricedPlan(models.Model):
    """
    Shared shape for anything sold on a recurring billing_period basis
    (SubscriptionPackage, FacebookAddonPlan): a base monthly USD price plus
    a per-period discount percent, and the USD/EGP total price computation
    for a given period. Abstract - contributes no table of its own.
    """
    BILLING_PERIOD_CHOICES = [
        ('monthly', 'شهري'),
        ('quarterly', 'ربع سنوي (3 أشهر)'),
        ('semiannual', 'نصف سنوي (6 أشهر)'),
        ('annual', 'سنوي (12 شهر)'),
    ]
    # عدد الأشهر التي تغطيها كل فترة اشتراك، تُستخدم لحساب السعر الإجمالي قبل الخصم
    BILLING_PERIOD_MONTHS = {'monthly': 1, 'quarterly': 3, 'semiannual': 6, 'annual': 12}
    # مدة صلاحية كود الربط (WPConnectionToken) بالأيام لكل فترة اشتراك
    BILLING_PERIOD_DAYS = {'monthly': 30, 'quarterly': 90, 'semiannual': 182, 'annual': 365}

    name = models.CharField(max_length=100, verbose_name="الاسم")
    price = models.DecimalField(max_digits=10, decimal_places=2, verbose_name="السعر (بالدولار)")
    is_active = models.BooleanField(default=True, verbose_name="مفعلة")
    is_recommended = models.BooleanField(
        default=False,
        verbose_name="الأكثر شعبية",
        help_text="تُميَّز هذه الباقة بشارة \"الأكثر شعبية\" وتُبرز بصرياً في صفحات الأسعار. يفضّل تفعيلها لباقة واحدة فقط في نفس الوقت."
    )

    quarterly_discount_percent = models.PositiveIntegerField(
        default=0, validators=[MaxValueValidator(100)],
        verbose_name="نسبة الخصم - اشتراك ربع سنوي (%)"
    )
    semiannual_discount_percent = models.PositiveIntegerField(
        default=0, validators=[MaxValueValidator(100)],
        verbose_name="نسبة الخصم - اشتراك نصف سنوي (%)"
    )
    annual_discount_percent = models.PositiveIntegerField(
        default=0, validators=[MaxValueValidator(100)],
        verbose_name="نسبة الخصم - اشتراك سنوي (%)"
    )

    class Meta:
        abstract = True

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

    def discount_percent_for_period(self, period):
        """نسبة الخصم المضبوطة لهذه الباقة حسب فترة الاشتراك المختارة."""
        return {
            'quarterly': self.quarterly_discount_percent,
            'semiannual': self.semiannual_discount_percent,
            'annual': self.annual_discount_percent,
        }.get(period, 0)

    def price_for_period(self, period, currency='USD'):
        """
        السعر الإجمالي لفترة الاشتراك المختارة بعد تطبيق نسبة الخصم الخاصة
        بالباقة لتلك الفترة. يُحسب دائماً على الخادم (وليس في المتصفح) حتى
        لا يعتمد المبلغ النهائي المحفوظ في Transaction على قيمة يرسلها العميل.
        """
        months = self.BILLING_PERIOD_MONTHS.get(period, 1)
        base = self.price if currency == 'USD' else self.price_egp
        discount = self.discount_percent_for_period(period)
        total = base * months * (Decimal(100) - discount) / Decimal(100)
        return total.quantize(Decimal('0.01'))

    def all_period_prices(self):
        """خريطة السعر ونسبة الخصم لكل فترة اشتراك متاحة، لعرضها في صفحات التسعير."""
        return {
            period: {
                'usd': str(self.price_for_period(period, 'USD')),
                'egp': str(self.price_for_period(period, 'EGP')),
                'discount': self.discount_percent_for_period(period),
            }
            for period, _ in self.BILLING_PERIOD_CHOICES
        }


class SubscriptionPackage(PricedPlan):
    daily_limit = models.PositiveIntegerField(verbose_name="الحد الأقصى للنشر اليومي")
    monthly_articles_limit = models.PositiveIntegerField(
        default=0,
        verbose_name="الحد الأقصى للنشر الشهري",
        help_text="للعرض فقط حالياً في لوحة التحكم وصفحة الباقات، بدون إنفاذ فعلي في محرك النشر. 0 = لا يُعرض حد شهري منفصل."
    )
    features = models.TextField(verbose_name="المميزات", help_text="كل ميزة في سطر منفصل")
    is_custom = models.BooleanField(default=False, verbose_name="مخصصة للشركات (تتطلب تواصل)")
    included_facebook_addon_plan = models.ForeignKey(
        'FacebookAddonPlan', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='included_in_packages',
        verbose_name="تشمل باقة فيسبوك التالية مجاناً",
        help_text="اختر باقة نشر فيسبوك تُمنح تلقائياً مجاناً لمن يشترك في هذه الباقة. اتركه فارغاً لو الباقة دي لا تشمل خدمة فيسبوك."
    )


class FacebookAddonPlan(PricedPlan):
    monthly_articles_limit = models.PositiveIntegerField(
        default=0,
        verbose_name="الحد الأقصى لعدد الأخبار المنشورة على فيسبوك شهرياً",
        help_text="بعد الوصول لهذا العدد يتوقف النشر التلقائي على فيسبوك حتى بداية الشهر التالي أو الترقية. 0 = بدون حد (غير محدود)."
    )
    show_watermark = models.BooleanField(
        default=False,
        verbose_name="إظهار علامة \"Sahafi Hub\" على التصميم",
        help_text="يُفعَّل عادة في الباقة المجانية كحافز للترقية لباقة بدون علامة مائية."
    )

    class Meta:
        verbose_name = "باقة نشر فيسبوك"
        verbose_name_plural = "باقات نشر فيسبوك"

class AdsAddonPlan(PricedPlan):
    """
    Gates access to the self-serve Facebook Ads management feature (see the
    `ads` app). Unlike FacebookAddonPlan's monthly_articles_limit, this does
    NOT limit ad spend - the customer connects and pays for their own
    Facebook Ad Account directly to Meta. What's gated here is enshrly's own
    management service: how many campaigns the system will run for them at
    once, and a safety ceiling on the daily budget a non-expert customer can
    set through the wizard.
    """
    max_concurrent_active_campaigns = models.PositiveIntegerField(
        default=1,
        verbose_name="أقصى عدد حملات نشطة في نفس الوقت",
        help_text="0 = بدون حد."
    )
    max_daily_budget_usd = models.DecimalField(
        max_digits=10, decimal_places=2, default=Decimal('10.00'),
        verbose_name="أقصى ميزانية يومية للحملة (USD)",
        help_text="سقف أمان لكل حملة عبر معالج الإنشاء، لحماية العميل غير الخبير من إدخال ميزانية كبيرة بالخطأ."
    )

    class Meta:
        verbose_name = "باقة إدارة إعلانات فيسبوك"
        verbose_name_plural = "باقات إدارة إعلانات فيسبوك"


class DevicePairing(models.Model):
    """
    Singleton tracking whether the currently displayed pairing QR (see
    pair_qr_view) has been claimed by confirm_pairing yet, so the QR page
    can poll and switch to a "paired" state without a manual refresh.
    """
    paired = models.BooleanField(default=False)
    updated_at = models.DateTimeField(auto_now=True)

    @classmethod
    def get_singleton(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


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

    PRODUCT_TYPE_CHOICES = [
        ('package', 'باقة نشر'),
        ('facebook_addon', 'باقة نشر فيسبوك'),
        ('ads_addon', 'باقة إدارة إعلانات فيسبوك'),
    ]

    transaction_id = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    customer = models.ForeignKey(CustomerProfile, on_delete=models.SET_NULL, null=True, related_name='transactions')
    product_type = models.CharField(max_length=20, choices=PRODUCT_TYPE_CHOICES, default='package', verbose_name="نوع المنتج")
    package = models.ForeignKey(SubscriptionPackage, on_delete=models.SET_NULL, null=True, blank=True)
    facebook_addon_plan = models.ForeignKey(
        'FacebookAddonPlan', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='transactions', verbose_name="باقة فيسبوك (لو نوع المنتج إضافة فيسبوك)"
    )
    ads_addon_plan = models.ForeignKey(
        'AdsAddonPlan', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='transactions', verbose_name="باقة إدارة إعلانات (لو نوع المنتج إدارة إعلانات)"
    )
    wp_site = models.ForeignKey(
        'syndicator.WordPressSite', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='facebook_addon_transactions', verbose_name="الموقع المستهدف (لباقات فيسبوك)"
    )
    billing_period = models.CharField(
        max_length=12,
        choices=SubscriptionPackage.BILLING_PERIOD_CHOICES,
        default='monthly',
        verbose_name="فترة الاشتراك"
    )
    amount = models.DecimalField(max_digits=10, decimal_places=2, verbose_name="المبلغ المدفوع")
    currency = models.CharField(max_length=10, choices=CURRENCY_CHOICES, default='USD', verbose_name="العملة")
    gateway = models.CharField(max_length=20, choices=GATEWAY_CHOICES, verbose_name="بوابة الدفع")
    gateway_transaction_id = models.CharField(max_length=255, blank=True, null=True, verbose_name="معرف العملية في البوابة")
    sender_phone = models.CharField(max_length=20, blank=True, null=True, verbose_name="رقم المرسل (المحفظة)")
    verified_transaction_id = models.CharField(max_length=255, blank=True, null=True, unique=True, verbose_name="رقم العملية الموثق")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    fallback_notified = models.BooleanField(
        default=False,
        verbose_name="تم إرسال رسالة التأكيد اليدوي",
        help_text="لتفادي إرسال رسالة واتساب مكررة لطلب سكرين شوت التحويل عند انقطاع اتصال تطبيق المراقبة"
    )
    page_closed_notified = models.BooleanField(
        default=False,
        verbose_name="تم إرسال رسالة إغلاق صفحة الدفع",
        help_text="لتفادي إرسال رسالة واتساب مكررة لو العميل أغلق/أعاد فتح صفحة الدفع أكثر من مرة"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.transaction_id} - {self.amount} {self.currency} - {self.status}"

    @property
    def product_display_name(self):
        """Name of whatever this transaction actually paid for, regardless of
        product_type - for shared templates/notifications that used to
        assume every Transaction had a `package`."""
        if self.product_type == 'facebook_addon':
            return self.facebook_addon_plan.name if self.facebook_addon_plan else 'باقة نشر فيسبوك'
        if self.product_type == 'ads_addon':
            return self.ads_addon_plan.name if self.ads_addon_plan else 'باقة إدارة إعلانات فيسبوك'
        return self.package.name if self.package else 'الباقة'



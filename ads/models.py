from django.db import models
from django.utils import timezone

from syndicator.fields import EncryptedCharField


class AdAccountConnection(models.Model):
    """
    One per WordPressSite: the customer's own Facebook Ad Account, connected
    via OAuth (see views_ads_connect.py) with ads_management/ads_read scope.
    Mirrors WordPressSite.facebook_page_id/facebook_access_token, but kept on
    its own model (rather than bolted onto WordPressSite) since it carries a
    distinct addon-plan/billing relationship and richer connection metadata.
    """
    STATUS_CHOICES = [
        ('not_connected', 'غير متصل'),
        ('connected', 'متصل'),
        ('needs_reauth', 'يحتاج إعادة ربط'),
        ('error', 'خطأ'),
    ]

    wp_site = models.OneToOneField(
        'syndicator.WordPressSite', on_delete=models.CASCADE, related_name='ad_account_connection',
        verbose_name="الموقع",
    )
    facebook_ad_account_id = models.CharField(max_length=100, blank=True, default='', verbose_name="معرّف حساب الإعلانات (act_...)")
    facebook_business_id = models.CharField(max_length=100, blank=True, default='', verbose_name="معرّف Business Manager")
    ad_account_name = models.CharField(max_length=255, blank=True, default='', verbose_name="اسم حساب الإعلانات")
    currency = models.CharField(max_length=10, blank=True, default='', verbose_name="عملة حساب الإعلانات")
    access_token = EncryptedCharField(max_length=1000, blank=True, null=True, verbose_name="توكن وصول (Long-Lived User Token بصلاحية ads_management)")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='not_connected', verbose_name="حالة الاتصال")
    error_message = models.TextField(blank=True, default='', verbose_name="رسالة الخطأ")

    ads_addon_plan = models.ForeignKey(
        'payments.AdsAddonPlan', null=True, blank=True, on_delete=models.SET_NULL,
        verbose_name="باقة إدارة الإعلانات الحالية",
    )
    ads_addon_trial_ends_at = models.DateTimeField(null=True, blank=True, verbose_name="نهاية الاشتراك/الفترة التجريبية")

    connected_at = models.DateTimeField(null=True, blank=True, verbose_name="تاريخ الربط")
    last_verified_at = models.DateTimeField(null=True, blank=True, verbose_name="آخر تحقق من صلاحية الاتصال")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "ربط حساب إعلانات فيسبوك"
        verbose_name_plural = "ربط حسابات إعلانات فيسبوك"

    def __str__(self):
        return f"{self.wp_site.name} - {self.ad_account_name or self.facebook_ad_account_id or 'غير متصل'}"

    @property
    def is_connected(self):
        return self.status == 'connected' and bool(self.facebook_ad_account_id and self.access_token)

    @property
    def ads_addon_is_active(self):
        """Gates access to the ads management feature itself - mirrors
        WordPressSite.facebook_addon_is_active. Independent from is_connected:
        a customer can have a paid, active plan but not have finished
        connecting their ad account yet (see ads_dashboard_view)."""
        if not self.ads_addon_plan_id:
            return False
        if self.ads_addon_trial_ends_at:
            return timezone.now() <= self.ads_addon_trial_ends_at
        return True


class Campaign(models.Model):
    OBJECTIVE_CHOICES = [
        ('OUTCOME_TRAFFIC', 'زيادة الزيارات لموقعك'),
        ('OUTCOME_ENGAGEMENT', 'تفاعل أكبر مع المنشور'),
        ('OUTCOME_AWARENESS', 'أكبر انتشار ممكن'),
    ]
    STATUS_CHOICES = [
        ('draft', 'مسودة'),
        ('pending_creation', 'جاري الإنشاء'),
        ('active', 'نشطة'),
        ('paused', 'متوقفة مؤقتاً'),
        ('completed', 'منتهية'),
        ('error', 'خطأ'),
    ]

    ad_account = models.ForeignKey(AdAccountConnection, on_delete=models.CASCADE, related_name='campaigns', verbose_name="حساب الإعلانات")
    article = models.ForeignKey('syndicator.Article', null=True, blank=True, on_delete=models.SET_NULL, related_name='ad_campaigns', verbose_name="الخبر المُروَّج له")
    social_share_post = models.ForeignKey('syndicator.SocialSharePost', null=True, blank=True, on_delete=models.SET_NULL, related_name='ad_campaigns', verbose_name="منشور فيسبوك المُروَّج له")
    facebook_post_id = models.CharField(max_length=64, blank=True, default='', verbose_name="معرّف منشور فيسبوك (بدون سجل SocialSharePost محلي)")

    name = models.CharField(max_length=255, verbose_name="اسم الحملة")
    objective = models.CharField(max_length=30, choices=OBJECTIVE_CHOICES, verbose_name="الهدف")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='draft', verbose_name="الحالة")
    facebook_campaign_id = models.CharField(max_length=100, blank=True, default='', verbose_name="معرّف الحملة على فيسبوك")

    daily_budget = models.DecimalField(max_digits=10, decimal_places=2, verbose_name="الميزانية اليومية")
    start_date = models.DateField(verbose_name="تاريخ البدء")
    end_date = models.DateField(null=True, blank=True, verbose_name="تاريخ الانتهاء")
    destination_url = models.URLField(max_length=1000, blank=True, default='', verbose_name="رابط الوجهة")

    error_message = models.TextField(blank=True, default='', verbose_name="رسالة الخطأ")
    created_by = models.ForeignKey('accounts.CustomerProfile', null=True, blank=True, on_delete=models.SET_NULL, verbose_name="أُنشئت بواسطة")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "حملة إعلانية"
        verbose_name_plural = "الحملات الإعلانية"
        ordering = ['-created_at']

    def __str__(self):
        return self.name

    @property
    def latest_insight(self):
        return self.insights.order_by('-date').first()

    @property
    def total_spend(self):
        return self.insights.aggregate(total=models.Sum('spend'))['total'] or 0


class AdSet(models.Model):
    STATUS_CHOICES = Campaign.STATUS_CHOICES

    campaign = models.ForeignKey(Campaign, on_delete=models.CASCADE, related_name='ad_sets', verbose_name="الحملة")
    facebook_adset_id = models.CharField(max_length=100, blank=True, default='', verbose_name="معرّف المجموعة الإعلانية")
    optimization_goal = models.CharField(max_length=50, blank=True, default='', verbose_name="هدف التحسين")
    billing_event = models.CharField(max_length=50, blank=True, default='', verbose_name="حدث الفوترة")
    targeting_spec = models.JSONField(default=dict, blank=True, verbose_name="بيانات الاستهداف")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='draft', verbose_name="الحالة")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "مجموعة إعلانية"
        verbose_name_plural = "المجموعات الإعلانية"

    def __str__(self):
        return f"{self.campaign.name} - AdSet"


class AdCreative(models.Model):
    SOURCE_TYPE_CHOICES = [
        ('existing_post', 'ترويج منشور فيسبوك موجود'),
        ('article_cover', 'صورة غلاف الخبر'),
    ]

    ad_set = models.ForeignKey(AdSet, on_delete=models.CASCADE, related_name='creatives', verbose_name="المجموعة الإعلانية")
    facebook_creative_id = models.CharField(max_length=100, blank=True, default='', verbose_name="معرّف المحتوى الإعلاني")
    source_type = models.CharField(max_length=20, choices=SOURCE_TYPE_CHOICES, verbose_name="مصدر المحتوى")
    image_url = models.URLField(max_length=1000, blank=True, default='', verbose_name="رابط الصورة")
    primary_text = models.TextField(blank=True, default='', verbose_name="النص الأساسي")
    headline = models.CharField(max_length=255, blank=True, default='', verbose_name="العنوان")
    call_to_action = models.CharField(max_length=30, default='LEARN_MORE', verbose_name="زر الدعوة لاتخاذ إجراء")
    destination_url = models.URLField(max_length=1000, blank=True, default='', verbose_name="رابط الوجهة")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "محتوى إعلاني"
        verbose_name_plural = "المحتوى الإعلاني"

    def __str__(self):
        return self.headline or f"Creative #{self.pk}"


class Ad(models.Model):
    STATUS_CHOICES = Campaign.STATUS_CHOICES

    ad_set = models.ForeignKey(AdSet, on_delete=models.CASCADE, related_name='ads', verbose_name="المجموعة الإعلانية")
    creative = models.ForeignKey(AdCreative, on_delete=models.CASCADE, related_name='ads', verbose_name="المحتوى الإعلاني")
    facebook_ad_id = models.CharField(max_length=100, blank=True, default='', verbose_name="معرّف الإعلان")
    name = models.CharField(max_length=255, blank=True, default='', verbose_name="اسم الإعلان")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='draft', verbose_name="الحالة")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "إعلان"
        verbose_name_plural = "الإعلانات"

    def __str__(self):
        return self.name or f"Ad #{self.pk}"


class AdInsightSnapshot(models.Model):
    """One row per (campaign, date) - daily performance synced from the
    Facebook Insights API. Upserted via update_or_create, see
    ads.tasks.sync_campaign_insights_task."""
    campaign = models.ForeignKey(Campaign, on_delete=models.CASCADE, related_name='insights', verbose_name="الحملة")
    date = models.DateField(verbose_name="التاريخ")
    impressions = models.PositiveIntegerField(default=0, verbose_name="مرات الظهور")
    reach = models.PositiveIntegerField(default=0, verbose_name="الوصول")
    clicks = models.PositiveIntegerField(default=0, verbose_name="النقرات")
    spend = models.DecimalField(max_digits=10, decimal_places=2, default=0, verbose_name="الإنفاق")
    ctr = models.DecimalField(max_digits=6, decimal_places=3, default=0, verbose_name="معدل النقر (%)")
    cpc = models.DecimalField(max_digits=10, decimal_places=4, default=0, verbose_name="تكلفة النقرة")
    cpm = models.DecimalField(max_digits=10, decimal_places=4, default=0, verbose_name="تكلفة الألف ظهور")
    raw_response = models.JSONField(null=True, blank=True, verbose_name="استجابة API الخام")
    synced_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "أداء يومي للحملة"
        verbose_name_plural = "الأداء اليومي للحملات"
        unique_together = ('campaign', 'date')
        ordering = ['-date']

    def __str__(self):
        return f"{self.campaign.name} - {self.date}"

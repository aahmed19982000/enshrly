from django.views.generic import ListView, CreateView, UpdateView, DeleteView, View
from django.contrib.auth.mixins import UserPassesTestMixin
from django.urls import reverse_lazy
from django.shortcuts import get_object_or_404, redirect
from django.contrib import messages

from accounts.models import CustomerProfile
from payments.models import SubscriptionPackage, FacebookAddonPlan, AdsAddonPlan, Transaction
from payments.tasks import send_payment_success_whatsapp
from payments.views import _complete_transaction, _token_expiry_days

class StaffRequiredMixin(UserPassesTestMixin):
    login_url = reverse_lazy('news_ai:staff_login')

    def test_func(self):
        return self.request.user.is_staff

# --- Subscription Packages Management ---
class PackageListView(StaffRequiredMixin, ListView):
    model = SubscriptionPackage
    template_name = 'ai_dashboard/saas/packages_list.html'
    context_object_name = 'packages'
    ordering = ['price']

class PackageCreateView(StaffRequiredMixin, CreateView):
    model = SubscriptionPackage
    template_name = 'ai_dashboard/saas/package_form.html'
    fields = [
        'name', 'price', 'daily_limit', 'monthly_articles_limit', 'features',
        'quarterly_discount_percent', 'semiannual_discount_percent', 'annual_discount_percent',
        'included_facebook_addon_plan', 'is_recommended', 'is_custom', 'is_active',
    ]
    success_url = reverse_lazy('news_ai:saas_packages')

class PackageUpdateView(StaffRequiredMixin, UpdateView):
    model = SubscriptionPackage
    template_name = 'ai_dashboard/saas/package_form.html'
    fields = [
        'name', 'price', 'daily_limit', 'monthly_articles_limit', 'features',
        'quarterly_discount_percent', 'semiannual_discount_percent', 'annual_discount_percent',
        'included_facebook_addon_plan', 'is_recommended', 'is_custom', 'is_active',
    ]
    success_url = reverse_lazy('news_ai:saas_packages')

class PackageDeleteView(StaffRequiredMixin, DeleteView):
    model = SubscriptionPackage
    template_name = 'ai_dashboard/saas/package_confirm_delete.html'
    success_url = reverse_lazy('news_ai:saas_packages')

# --- Facebook Addon Plans Management ---
class FacebookAddonPlanListView(StaffRequiredMixin, ListView):
    model = FacebookAddonPlan
    template_name = 'ai_dashboard/saas/facebook_plans_list.html'
    context_object_name = 'plans'
    ordering = ['price']

class FacebookAddonPlanCreateView(StaffRequiredMixin, CreateView):
    model = FacebookAddonPlan
    template_name = 'ai_dashboard/saas/facebook_plan_form.html'
    fields = [
        'name', 'price', 'monthly_articles_limit', 'show_watermark',
        'quarterly_discount_percent', 'semiannual_discount_percent', 'annual_discount_percent',
        'is_recommended', 'is_active',
    ]
    success_url = reverse_lazy('news_ai:saas_facebook_plans')

class FacebookAddonPlanUpdateView(StaffRequiredMixin, UpdateView):
    model = FacebookAddonPlan
    template_name = 'ai_dashboard/saas/facebook_plan_form.html'
    fields = [
        'name', 'price', 'monthly_articles_limit', 'show_watermark',
        'quarterly_discount_percent', 'semiannual_discount_percent', 'annual_discount_percent',
        'is_recommended', 'is_active',
    ]
    success_url = reverse_lazy('news_ai:saas_facebook_plans')

class FacebookAddonPlanDeleteView(StaffRequiredMixin, DeleteView):
    model = FacebookAddonPlan
    template_name = 'ai_dashboard/saas/facebook_plan_confirm_delete.html'
    success_url = reverse_lazy('news_ai:saas_facebook_plans')

# --- Ads Management Addon Plans ---
class AdsAddonPlanListView(StaffRequiredMixin, ListView):
    model = AdsAddonPlan
    template_name = 'ai_dashboard/saas/ads_plans_list.html'
    context_object_name = 'plans'
    ordering = ['price']

class AdsAddonPlanCreateView(StaffRequiredMixin, CreateView):
    model = AdsAddonPlan
    template_name = 'ai_dashboard/saas/ads_plan_form.html'
    fields = [
        'name', 'price', 'max_concurrent_active_campaigns', 'max_daily_budget_usd',
        'quarterly_discount_percent', 'semiannual_discount_percent', 'annual_discount_percent',
        'is_recommended', 'is_active',
    ]
    success_url = reverse_lazy('news_ai:saas_ads_plans')

class AdsAddonPlanUpdateView(StaffRequiredMixin, UpdateView):
    model = AdsAddonPlan
    template_name = 'ai_dashboard/saas/ads_plan_form.html'
    fields = [
        'name', 'price', 'max_concurrent_active_campaigns', 'max_daily_budget_usd',
        'quarterly_discount_percent', 'semiannual_discount_percent', 'annual_discount_percent',
        'is_recommended', 'is_active',
    ]
    success_url = reverse_lazy('news_ai:saas_ads_plans')

class AdsAddonPlanDeleteView(StaffRequiredMixin, DeleteView):
    model = AdsAddonPlan
    template_name = 'ai_dashboard/saas/ads_plan_confirm_delete.html'
    success_url = reverse_lazy('news_ai:saas_ads_plans')

# --- Customers Management ---
class CustomerListView(StaffRequiredMixin, ListView):
    model = CustomerProfile
    template_name = 'ai_dashboard/saas/customers_list.html'
    context_object_name = 'customers'
    ordering = ['-user__date_joined']

class TransactionListView(StaffRequiredMixin, ListView):
    model = Transaction
    template_name = 'ai_dashboard/saas/transactions_list.html'
    context_object_name = 'transactions'
    ordering = ['-created_at']

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        all_tx = Transaction.objects.all()
        context['total_count'] = all_tx.count()
        context['completed_count'] = all_tx.filter(status='completed').count()
        context['pending_count'] = all_tx.filter(status='pending').count()
        
        from django.db.models import Sum
        context['total_egp'] = all_tx.filter(status='completed', currency='EGP').aggregate(Sum('amount'))['amount__sum'] or 0
        context['total_usd'] = all_tx.filter(status='completed', currency='USD').aggregate(Sum('amount'))['amount__sum'] or 0
        return context

class ConfirmTransactionView(StaffRequiredMixin, View):
    """ Allows manual confirmation of a pending transaction and issues a WP Token. """
    def post(self, request, pk, *args, **kwargs):
        transaction = get_object_or_404(Transaction, pk=pk)

        # Atomic claim, same as the automated confirmation paths in payments/views.py —
        # avoids a double-click (or a race with an automated confirmation landing at
        # the same moment) from issuing two tokens for one transaction.
        claimed = Transaction.objects.filter(pk=transaction.pk, status='pending').update(status='completed')
        if not claimed:
            messages.info(request, "هذه المعاملة مؤكدة بالفعل.")
            return redirect('news_ai:saas_transactions')

        days = _token_expiry_days(transaction)
        client_name = transaction.customer.user.first_name or transaction.customer.user.username

        # Issue token (or activate the Facebook add-on) upon manual
        # confirmation, same branching as the automated gateway paths.
        token_str = _complete_transaction(transaction, client_name)

        if token_str:
            send_payment_success_whatsapp.delay(
                phone_number=transaction.customer.whatsapp_number,
                client_name=client_name,
                package_name=transaction.package.name,
                token_code=token_str,
                days=days,
            )
            messages.success(request, f"تم تأكيد المعاملة بنجاح وإنشاء كود الربط للعميل {client_name}.")
        else:
            messages.success(request, f"تم تأكيد المعاملة وتفعيل {transaction.product_display_name} للعميل {client_name}.")
        return redirect('news_ai:saas_transactions')

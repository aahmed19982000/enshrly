from django.views.generic import ListView, CreateView, UpdateView, DeleteView, View
from django.contrib.auth.mixins import UserPassesTestMixin
from django.urls import reverse_lazy
from django.shortcuts import get_object_or_404, redirect
from django.contrib import messages

from accounts.models import CustomerProfile
from payments.models import SubscriptionPackage, Transaction
from syndicator.models import WPConnectionToken
import uuid

class StaffRequiredMixin(UserPassesTestMixin):
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
    fields = ['name', 'price', 'daily_limit', 'features', 'is_custom', 'is_active']
    success_url = reverse_lazy('news_ai:saas_packages')

class PackageUpdateView(StaffRequiredMixin, UpdateView):
    model = SubscriptionPackage
    template_name = 'ai_dashboard/saas/package_form.html'
    fields = ['name', 'price', 'daily_limit', 'features', 'is_custom', 'is_active']
    success_url = reverse_lazy('news_ai:saas_packages')

class PackageDeleteView(StaffRequiredMixin, DeleteView):
    model = SubscriptionPackage
    template_name = 'ai_dashboard/saas/package_confirm_delete.html'
    success_url = reverse_lazy('news_ai:saas_packages')

# --- Customers Management ---
class CustomerListView(StaffRequiredMixin, ListView):
    model = CustomerProfile
    template_name = 'ai_dashboard/saas/customers_list.html'
    context_object_name = 'customers'
    ordering = ['-user__date_joined']

# --- Transactions Management ---
class TransactionListView(StaffRequiredMixin, ListView):
    model = Transaction
    template_name = 'ai_dashboard/saas/transactions_list.html'
    context_object_name = 'transactions'
    ordering = ['-created_at']

class ConfirmTransactionView(StaffRequiredMixin, View):
    """ Allows manual confirmation of a pending transaction and issues a WP Token. """
    def post(self, request, pk, *args, **kwargs):
        transaction = get_object_or_404(Transaction, pk=pk)
        if transaction.status != 'completed':
            transaction.status = 'completed'
            transaction.save()
            
            # Issue token upon manual confirmation
            token_str = str(uuid.uuid4())
            WPConnectionToken.objects.create(
                token=token_str,
                client_name=transaction.user.first_name or transaction.user.username,
                package_daily_limit=transaction.package.daily_limit,
            )
            messages.success(request, f"تم تأكيد المعاملة بنجاح وإنشاء كود الربط للعميل {transaction.user.first_name}.")
        return redirect('news_ai:saas_transactions')

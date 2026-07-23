from django.urls import path
from . import views

app_name = 'payments'

urlpatterns = [
    path('packages/', views.packages_view, name='packages'),
    path('packages/free-trial/', views.start_free_trial, name='free_trial'),
    path('checkout/<int:package_id>/', views.checkout_view, name='checkout'),
    path('checkout/paypal/<uuid:transaction_id>/', views.paypal_checkout_view, name='paypal_checkout'),
    path('checkout/paypal/confirm/', views.confirm_paypal_payment, name='confirm_paypal_payment'),
    path('checkout/paymob/<uuid:transaction_id>/', views.paymob_checkout_view, name='paymob_checkout'),
    path('paymob/callback/', views.paymob_callback_view, name='paymob_callback'),
    path('paymob/webhook/', views.paymob_webhook_view, name='paymob_webhook'),
    path('checkout/pending/<uuid:transaction_id>/', views.checkout_pending_view, name='checkout_pending'),
    path('checkout/status/<uuid:transaction_id>/', views.check_payment_status_api, name='check_payment_status'),
    path('pair/', views.pair_qr_view, name='pair_qr'),
    path('api/v1/payments/transactions/', views.mobile_post_transaction, name='mobile_post_transaction'),
    path('api/v1/payments/api/confirm-payment/', views.confirm_payment_api, name='confirm_payment_api'),
    path('api/v1/payments/api/v1/pair/confirm/', views.confirm_pairing, name='confirm_pairing'),
    path('success/<uuid:transaction_id>/', views.payment_success_view, name='payment_success'),
]

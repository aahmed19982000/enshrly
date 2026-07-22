from django.urls import path
from . import views

app_name = 'payments'

urlpatterns = [
    path('packages/', views.packages_view, name='packages'),
    path('checkout/<int:package_id>/', views.checkout_view, name='checkout'),
    path('success/<uuid:transaction_id>/', views.payment_success_view, name='payment_success'),
]

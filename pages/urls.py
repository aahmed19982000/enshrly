from django.urls import path
from . import views

app_name = 'pages'

urlpatterns = [
    path('about/', views.AboutView.as_view(), name='about'),
    path('privacy/', views.PrivacyView.as_view(), name='privacy'),
    path('terms/', views.TermsView.as_view(), name='terms'),
    path('refund-policy/', views.RefundView.as_view(), name='refund'),
    path('contact/', views.contact_view, name='contact'),
]

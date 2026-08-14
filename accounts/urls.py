from django.urls import path
from . import views

app_name = 'accounts'

urlpatterns = [
    path('signup/', views.signup_view, name='signup'),
    path('verify-otp/', views.verify_otp_view, name='verify_otp'),
    path('verify-otp/resend/', views.resend_otp_view, name='resend_otp'),
    path('login/', views.login_view, name='login'),
    path('auth0/login/', views.auth0_login_view, name='auth0_login'),
    path('auth0/callback/', views.auth0_callback_view, name='auth0_callback'),
    path('forgot-password/', views.forgot_password_view, name='forgot_password'),
    path('reset-password/', views.reset_password_view, name='reset_password'),
    path('dashboard/', views.dashboard_view, name='dashboard'),
    path('facebook/', views.facebook_dashboard_view, name='facebook_dashboard'),
    path('facebook/<int:wp_site_id>/connect/', views.facebook_connect_redirect_view, name='facebook_connect_redirect'),
    path('facebook/<int:wp_site_id>/design/', views.social_design_edit_view, name='social_design_edit'),
    path('logout/', views.auth0_logout_view, name='logout'),
]

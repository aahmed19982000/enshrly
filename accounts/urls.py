from django.urls import path
from . import views
from django.contrib.auth.views import LogoutView

app_name = 'accounts'

urlpatterns = [
    path('signup/', views.signup_view, name='signup'),
    path('verify-otp/', views.verify_otp_view, name='verify_otp'),
    path('verify-otp/resend/', views.resend_otp_view, name='resend_otp'),
    path('login/', views.login_view, name='login'),
    path('forgot-password/', views.forgot_password_view, name='forgot_password'),
    path('reset-password/', views.reset_password_view, name='reset_password'),
    path('dashboard/', views.dashboard_view, name='dashboard'),
    path('facebook/', views.facebook_dashboard_view, name='facebook_dashboard'),
    path('facebook/<int:wp_site_id>/connect/', views.facebook_connect_redirect_view, name='facebook_connect_redirect'),
    path('facebook/<int:wp_site_id>/design/', views.social_design_edit_view, name='social_design_edit'),
    path('logout/', LogoutView.as_view(next_page='/'), name='logout'),
]

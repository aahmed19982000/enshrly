from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed
from django.contrib.auth.models import User
from .models import AISettings

class APITokenAuthentication(BaseAuthentication):
    def authenticate(self, request):
        auth_header = request.headers.get('Authorization', '')
        if not auth_header:
            return None
            
        if not auth_header.startswith('Bearer '):
            return None
            
        token = auth_header.split(' ')[1]
        
        # Check against AISettings API token
        settings_obj = AISettings.get_settings()
        if settings_obj.api_token and token == settings_obj.api_token:
            # Authenticate as the default author, or first superuser/staff
            user = settings_obj.default_author
            if not user:
                user = User.objects.filter(is_superuser=True).first()
            if not user:
                user = User.objects.filter(is_staff=True).first()
            if not user:
                user = User.objects.first()
                
            if user:
                return (user, None)
                
        return None

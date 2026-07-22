from django.shortcuts import render
from django.contrib.auth.decorators import login_required
from syndicator.models import WPConnectionToken

@login_required
def dashboard_view(request):
    profile = getattr(request.user, 'customer_profile', None)
    
    # Get user's tokens
    tokens = WPConnectionToken.objects.filter(client_name=request.user.first_name).order_by('-created_at')
    
    context = {
        'tokens': tokens,
        'profile': profile,
    }
    return render(request, 'accounts/dashboard.html', context)

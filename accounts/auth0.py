from authlib.integrations.django_client import OAuth
from django.conf import settings

oauth = OAuth()

# Only register when configured, so a blank AUTH0_DOMAIN doesn't crash the
# server_metadata_url discovery call at import time.
if settings.AUTH0_DOMAIN:
    oauth.register(
        'auth0',
        client_id=settings.AUTH0_CLIENT_ID,
        client_secret=settings.AUTH0_CLIENT_SECRET,
        client_kwargs={'scope': 'openid profile email'},
        server_metadata_url=f'https://{settings.AUTH0_DOMAIN}/.well-known/openid-configuration',
    )

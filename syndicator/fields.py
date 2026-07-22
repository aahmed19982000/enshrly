from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.db import models


def _get_fernet():
    return Fernet(settings.FIELD_ENCRYPTION_KEY)


class EncryptedCharField(models.CharField):
    """
    A CharField that is transparently encrypted at rest using Fernet
    (settings.FIELD_ENCRYPTION_KEY). Plain CharField on the Python side;
    only the DB-stored value is ciphertext.
    """

    def get_prep_value(self, value):
        value = super().get_prep_value(value)
        if value is None or value == '':
            return value
        return _get_fernet().encrypt(value.encode()).decode()

    def from_db_value(self, value, expression, connection):
        if value is None or value == '':
            return value
        try:
            return _get_fernet().decrypt(value.encode()).decode()
        except InvalidToken:
            # Value was stored before encryption was introduced, or with a
            # different key. Surface it as-is rather than silently losing data.
            return value

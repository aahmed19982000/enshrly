from django.db import migrations


def backfill_customer(apps, schema_editor):
    """Best-effort backfill for legacy rows created before the customer FK
    existed. Only fills a token's customer when exactly one CustomerProfile's
    user.first_name matches the token's client_name — ambiguous rows are left
    null rather than guessed, since client_name was never a reliable identifier."""
    WPConnectionToken = apps.get_model('syndicator', 'WPConnectionToken')
    CustomerProfile = apps.get_model('accounts', 'CustomerProfile')

    for token in WPConnectionToken.objects.filter(customer__isnull=True):
        name = (token.client_name or '').replace(' (Free Trial)', '').strip()
        if not name:
            continue
        matches = list(CustomerProfile.objects.filter(user__first_name=name)[:2])
        if len(matches) == 1:
            token.customer = matches[0]
            token.save(update_fields=['customer'])


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('syndicator', '0008_wpconnectiontoken_customer_and_more'),
    ]

    operations = [
        migrations.RunPython(backfill_customer, noop_reverse),
    ]

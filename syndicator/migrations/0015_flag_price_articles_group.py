from django.db import migrations

PRICE_GROUP_NAME = "الأسواق المحلية (الذهب، العملات، مواد البناء، السلع)"
PRICE_GROUP_PARENT_NAME = "الاقتصاد"


def flag_price_group(apps, schema_editor):
    AISourceGroup = apps.get_model('syndicator', 'AISourceGroup')
    group = AISourceGroup.objects.filter(
        name=PRICE_GROUP_NAME, parent__name=PRICE_GROUP_PARENT_NAME,
    ).first()
    if group:
        group.is_price_articles_group = True
        group.save(update_fields=['is_price_articles_group'])


def unflag_price_group(apps, schema_editor):
    AISourceGroup = apps.get_model('syndicator', 'AISourceGroup')
    AISourceGroup.objects.filter(name=PRICE_GROUP_NAME).update(is_price_articles_group=False)


class Migration(migrations.Migration):

    dependencies = [
        ('syndicator', '0014_source_group_wp_category_map'),
    ]

    operations = [
        migrations.RunPython(flag_price_group, unflag_price_group),
    ]

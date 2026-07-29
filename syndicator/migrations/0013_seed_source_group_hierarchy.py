from django.db import migrations

DEFAULT_HIERARCHY = [
    ("الاقتصاد", [
        "أسواق المال والبورصة",
        "الأسواق المحلية (الذهب، العملات، مواد البناء، السلع)",
        "السياسات والمالية العامة",
        "ريادة الأعمال والشركات الناشئة",
    ]),
    ("البترول والطاقة", [
        "النفط والغاز التقليدي",
        "الطاقة النظيفة والمتجددة",
        "سلاسل الإمداد وشحن الطاقة",
    ]),
    ("الرياضة", [
        "كرة القدم المحلية",
        "كرة القدم العالمية",
        "الألعاب الأخرى",
    ]),
]


def seed_groups(apps, schema_editor):
    AISourceGroup = apps.get_model('syndicator', 'AISourceGroup')
    for parent_name, children in DEFAULT_HIERARCHY:
        parent, _ = AISourceGroup.objects.get_or_create(
            name=parent_name, defaults={'parent': None},
        )
        if parent.parent_id is not None:
            parent.parent_id = None
            parent.save(update_fields=['parent'])
        for child_name in children:
            AISourceGroup.objects.update_or_create(
                name=child_name, defaults={'parent_id': parent.id},
            )


def unseed_groups(apps, schema_editor):
    AISourceGroup = apps.get_model('syndicator', 'AISourceGroup')
    all_names = [parent_name for parent_name, _ in DEFAULT_HIERARCHY]
    all_names += [child_name for _, children in DEFAULT_HIERARCHY for child_name in children]
    AISourceGroup.objects.filter(name__in=all_names).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('syndicator', '0012_aisourcegroup_parent'),
    ]

    operations = [
        migrations.RunPython(seed_groups, unseed_groups),
    ]

from django.db import migrations

TASK_NAME = "Scrape and generate news (auto-publish cycle)"
TASK_PATH = "syndicator.tasks.scrape_and_generate_news_task"


def seed_periodic_task(apps, schema_editor):
    IntervalSchedule = apps.get_model('django_celery_beat', 'IntervalSchedule')
    PeriodicTask = apps.get_model('django_celery_beat', 'PeriodicTask')

    schedule, _ = IntervalSchedule.objects.get_or_create(
        every=10, period='minutes',
    )
    PeriodicTask.objects.update_or_create(
        name=TASK_NAME,
        defaults={
            'task': TASK_PATH,
            'interval': schedule,
            'enabled': True,
        },
    )


def unseed_periodic_task(apps, schema_editor):
    PeriodicTask = apps.get_model('django_celery_beat', 'PeriodicTask')
    PeriodicTask.objects.filter(name=TASK_NAME).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('syndicator', '0015_flag_price_articles_group'),
        ('django_celery_beat', '0019_alter_periodictasks_options'),
    ]

    operations = [
        migrations.RunPython(seed_periodic_task, unseed_periodic_task),
    ]

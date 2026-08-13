from django.db import migrations

INSIGHTS_TASK_NAME = "Sync Facebook ad campaign insights"
INSIGHTS_TASK_PATH = "ads.tasks.sync_campaign_insights_task"

HEALTH_TASK_NAME = "Poll Facebook ad account/campaign health"
HEALTH_TASK_PATH = "ads.tasks.poll_campaign_health_task"


def seed_periodic_tasks(apps, schema_editor):
    IntervalSchedule = apps.get_model('django_celery_beat', 'IntervalSchedule')
    PeriodicTask = apps.get_model('django_celery_beat', 'PeriodicTask')

    hourly, _ = IntervalSchedule.objects.get_or_create(every=1, period='hours')
    PeriodicTask.objects.update_or_create(
        name=INSIGHTS_TASK_NAME,
        defaults={'task': INSIGHTS_TASK_PATH, 'interval': hourly, 'enabled': True},
    )

    every_15_min, _ = IntervalSchedule.objects.get_or_create(every=15, period='minutes')
    PeriodicTask.objects.update_or_create(
        name=HEALTH_TASK_NAME,
        defaults={'task': HEALTH_TASK_PATH, 'interval': every_15_min, 'enabled': True},
    )


def unseed_periodic_tasks(apps, schema_editor):
    PeriodicTask = apps.get_model('django_celery_beat', 'PeriodicTask')
    PeriodicTask.objects.filter(name__in=[INSIGHTS_TASK_NAME, HEALTH_TASK_NAME]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('ads', '0001_initial'),
        ('django_celery_beat', '0019_alter_periodictasks_options'),
    ]

    operations = [
        migrations.RunPython(seed_periodic_tasks, unseed_periodic_tasks),
    ]

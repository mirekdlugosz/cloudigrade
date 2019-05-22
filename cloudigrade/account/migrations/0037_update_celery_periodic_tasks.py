# Generated by Django 2.1.5 on 2019-05-21 20:00

from django.apps import apps as global_apps
from django.db import migrations


def update_celery_periodic_tasks(apps, schema_editor):
    """Update Celery PeriodicTasks with pilot-specific names."""
    try:
        PeriodicTask = apps.get_model('django_celery_beat', 'PeriodicTask')
    except LookupError:
        # The django_celery_beat app is not loaded. Nothing to do!
        return

    periodic_task_names = [
        (
            'scale_up_inspection_cluster_every_60_min',
            'pilot_scale_up_inspection_cluster_every_60_min',
        ),
        (
            'persist_inspection_cluster_results',
            'pilot_persist_inspection_cluster_results',
        ),
        ('analyze_log_every_2_mins', 'pilot_analyze_log_every_2_mins'),
        ('inspect_pending_images', 'pilot_inspect_pending_images'),
        (
            'repopulate_ec2_instance_mapping_every_week',
            'pilot_repopulate_ec2_instance_mapping_every_week',
        ),
    ]

    for old_name, new_name in periodic_task_names:
        try:
            task = PeriodicTask.objects.get(name=old_name)
        except PeriodicTask.DoesNotExist:
            # This can happen if beat has never run with old configs, such as
            # with a fresh DB or when running tests. If so, nothing to do!
            continue

        if PeriodicTask.objects.filter(name=new_name).exists():
            # A version of the task with the new name might already exist if
            # the beat has started with the new configs but before this
            # migration has run. In that case, we want to simply delete the old
            # version task.
            task.delete()
        else:
            task.name = new_name
            task.save()


class Migration(migrations.Migration):

    dependencies = [
        ('account', '0036_remove_machineimageinspectionstart_occurred_at')
    ]

    if global_apps.is_installed('django_celery_beat'):
        dependencies.append(
            ('django_celery_beat', '0006_periodictask_priority')
        )

    operations = [migrations.RunPython(update_celery_periodic_tasks)]

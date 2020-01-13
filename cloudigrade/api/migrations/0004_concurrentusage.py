# Generated by Django 2.1.8 on 2019-06-14 18:31

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("api", "0003_machineimageinspectionstart"),
    ]

    operations = [
        migrations.CreateModel(
            name="ConcurrentUsage",
            fields=[
                (
                    "id",
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("date", models.DateField()),
                ("instances", models.IntegerField()),
                ("memory", models.FloatField()),
                ("vcpu", models.IntegerField()),
                (
                    "cloud_account",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        to="api.CloudAccount",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={"ordering": ("created_at",), "abstract": False,},
        ),
    ]

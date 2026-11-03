"""Alerts on Selections (#844): two new tables, ``YSE_App_selectionalert`` and ``YSE_App_selectionalertsnapshot``.

Additive only; develop's code never reads or writes them. Their foreign keys to ``auth_user``,
``auth_group`` and ``YSE_App_selection`` get their MySQL ``ON DELETE`` rules in the next
migration (0072): Django adds the plain constraints of a CreateModel only at the end of the
migration, so they cannot be changed in this one.
"""

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion

import YSE_App.models.fields


class Migration(migrations.Migration):

    dependencies = [
        ("YSE_App", "0070_spectrum_phase_fk_on_delete"),
        ("auth", "0012_alter_user_first_name_max_length"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="SelectionAlert",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("label", models.CharField(blank=True, default="", max_length=120,
                                           help_text="Optional name shown in the messages; the Selection's name "
                                                     "otherwise.")),
                ("target", models.CharField(choices=[("me", "Me"), ("team", "Everyone in a team I own"),
                                                     ("slack", "The Slack channel of a team I own")],
                                            default="me", max_length=8)),
                ("slack_channel", models.CharField(blank=True, default="", max_length=32,
                                                   help_text="Slack channel id (C... or G...) for target 'slack'.")),
                ("on_new_match", models.BooleanField(default=True)),
                ("on_new_spectrum", models.BooleanField(default=False)),
                ("on_class_change", models.BooleanField(default=False)),
                ("mag_threshold", models.FloatField(blank=True, null=True,
                                                    help_text="Alert when the latest detection crosses this "
                                                              "magnitude.")),
                ("mag_direction", models.CharField(choices=[("brighter", "becomes brighter than"),
                                                            ("fainter", "fades below")],
                                                   default="brighter", max_length=8)),
                ("frequency", models.CharField(choices=[("immediate", "Immediately"), ("hourly", "Hourly digest"),
                                                        ("daily", "Daily digest")],
                                               default="daily", max_length=10)),
                ("enabled", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("modified_at", models.DateTimeField(auto_now=True)),
                ("last_run_at", models.DateTimeField(blank=True, null=True)),
                ("last_error", models.CharField(blank=True, default="", max_length=500)),
                ("owner", models.ForeignKey(help_text="Who set the alert; the recipient when the target is 'me'.",
                                            on_delete=django.db.models.deletion.CASCADE,
                                            related_name="selection_alerts", to=settings.AUTH_USER_MODEL)),
                ("selection", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="alerts",
                                                to="YSE_App.selection")),
                ("team", models.ForeignKey(blank=True, help_text="The team notified (targets 'team' and 'slack').",
                                           null=True, on_delete=django.db.models.deletion.CASCADE,
                                           related_name="selection_alerts", to="auth.group")),
            ],
            options={
                "ordering": ("selection__name", "pk"),
            },
        ),
        migrations.CreateModel(
            name="SelectionAlertSnapshot",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("state", YSE_App.models.fields.JSONTextField(
                    blank=True, null=True,
                    help_text="{transient id: [class, newest spectrum id, latest point id, latest mag]}.")),
                ("pending", YSE_App.models.fields.JSONTextField(blank=True, null=True,
                                                                help_text="Events waiting for the next delivery.")),
                ("baseline_at", models.DateTimeField(blank=True, null=True)),
                ("last_delivered_at", models.DateTimeField(blank=True, null=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("alert", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="snapshots",
                                            to="YSE_App.selectionalert")),
                ("recipient", models.ForeignKey(blank=True,
                                                help_text="Empty for a Slack channel (evaluated as every team "
                                                          "member).",
                                                null=True, on_delete=django.db.models.deletion.CASCADE,
                                                related_name="+", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "unique_together": {("alert", "recipient")},
            },
        ),
    ]

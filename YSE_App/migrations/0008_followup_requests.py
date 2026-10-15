from django.conf import settings
from django.db import migrations, models
import django.core.validators
import django.db.models.deletion


def _priority_from_legacy(spec_priority, phot_priority):
    values = [float(v) for v in (spec_priority, phot_priority) if v is not None]
    if not values:
        return 4.0
    priority = min(values)
    return max(1.0, min(5.0, priority))


def backfill_followup_requests(apps, schema_editor):
    TransientFollowup = apps.get_model("YSE_App", "TransientFollowup")
    HostFollowup = apps.get_model("YSE_App", "HostFollowup")
    TransientFollowupRequest = apps.get_model("YSE_App", "TransientFollowupRequest")
    Log = apps.get_model("YSE_App", "Log")

    for followup in HostFollowup.objects.all().iterator():
        followup.priority = _priority_from_legacy(
            followup.spec_priority, followup.phot_priority
        )
        followup.save(update_fields=["priority"])

    for followup in TransientFollowup.objects.all().iterator():
        priority = _priority_from_legacy(followup.spec_priority, followup.phot_priority)
        followup.priority = priority
        followup.save(update_fields=["priority"])

        comments = list(
            Log.objects.filter(transient_followup_id=followup.id)
            .exclude(comment="")
            .values_list("comment", flat=True)
        )
        comment = "; ".join(comments)
        requestor_id = followup.requested_by_id or followup.created_by_id
        req = TransientFollowupRequest.objects.create(
            followup=followup,
            requestor_id=requestor_id,
            priority=priority,
            comment=comment,
            created_by_id=followup.created_by_id,
            modified_by_id=followup.modified_by_id,
        )
        TransientFollowupRequest.objects.filter(pk=req.pk).update(
            requested_at=followup.created_date,
            created_date=followup.created_date,
        )


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("YSE_App", "0007_resource_creator_only"),
    ]

    operations = [
        migrations.AddField(
            model_name="hostfollowup",
            name="priority",
            field=models.FloatField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="transientfollowup",
            name="priority",
            field=models.FloatField(blank=True, null=True),
        ),
        migrations.CreateModel(
            name="TransientFollowupRequest",
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
                ("created_date", models.DateTimeField(auto_now_add=True)),
                ("modified_date", models.DateTimeField(auto_now=True)),
                ("requested_at", models.DateTimeField(auto_now_add=True)),
                (
                    "priority",
                    models.FloatField(
                        default=4.0,
                        validators=[
                            django.core.validators.MinValueValidator(1.0),
                            django.core.validators.MaxValueValidator(5.0),
                        ],
                    ),
                ),
                ("comment", models.TextField(blank=True)),
                (
                    "created_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="%(class)s_created_by",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "followup",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="requests",
                        to="YSE_App.transientfollowup",
                    ),
                ),
                (
                    "modified_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="%(class)s_modified_by",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "requestor",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="transient_followup_requests",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "ordering": ["requested_at", "id"],
            },
        ),
        migrations.AddIndex(
            model_name="transientfollowuprequest",
            index=models.Index(
                fields=["followup", "requestor", "-requested_at"],
                name="YSE_App_tra_followu_7c9a1e_idx",
            ),
        ),
        migrations.RunPython(backfill_followup_requests, noop_reverse),
        migrations.RemoveField(
            model_name="hostfollowup",
            name="phot_priority",
        ),
        migrations.RemoveField(
            model_name="hostfollowup",
            name="spec_priority",
        ),
        migrations.RemoveField(
            model_name="transientfollowup",
            name="phot_priority",
        ),
        migrations.RemoveField(
            model_name="transientfollowup",
            name="spec_priority",
        ),
    ]

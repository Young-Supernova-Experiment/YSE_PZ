# One Mentorship row per side, with confirmation (Ryan, 2026-10-22).
#
# * ``owner``: the side that keeps the row. Existing rows: whoever entered them when that is
#   the mentor or the mentee, else the mentee (else the mentor).
# * ``status``: confirmed / pending. On MySQL the column keeps a real DEFAULT 'confirmed',
#   so code from before this migration can still insert rows on a shared database
#   (Django's AddField drops the default again; see 0036).
# * ``counterpart``: the other side's row.
#
# Existing relations between two users were visible on both sides; each gets a confirmed
# copy owned by the other side, so nothing that shows today disappears.

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


def _status_db_default(apps, schema_editor):
    if schema_editor.connection.vendor != "mysql":
        return
    table = apps.get_model("YSE_App", "Mentorship")._meta.db_table
    schema_editor.execute(
        "ALTER TABLE %s ALTER COLUMN status SET DEFAULT 'confirmed'" % schema_editor.quote_name(table))


def _split_sides(apps, schema_editor):
    Mentorship = apps.get_model("YSE_App", "Mentorship")
    for row in Mentorship.objects.filter(owner__isnull=True).order_by("id"):
        if row.created_by_id and row.created_by_id in (row.mentor_id, row.mentee_id):
            owner = row.created_by_id
        else:
            owner = row.mentee_id or row.mentor_id
        Mentorship.objects.filter(pk=row.pk).update(owner_id=owner, status="confirmed")
        if row.mentor_id and row.mentee_id and row.mentor_id != row.mentee_id:
            other = row.mentor_id if owner == row.mentee_id else row.mentee_id
            copy = Mentorship.objects.create(
                mentor_id=row.mentor_id, mentor_name=row.mentor_name, mentee_id=row.mentee_id,
                mentee_name=row.mentee_name, kind=row.kind, start_year=row.start_year,
                end_year=row.end_year, created_by_id=row.created_by_id, owner_id=other,
                status="confirmed", counterpart_id=row.pk)
            Mentorship.objects.filter(pk=row.pk).update(counterpart_id=copy.pk)


class Migration(migrations.Migration):
    # MySQL cannot roll back DDL; the ALTER ... SET DEFAULT runs outside a transaction (as in 0036).
    atomic = False

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('YSE_App', '0054_mentorship_kinds'),
    ]

    operations = [
        migrations.AddField(
            model_name='mentorship',
            name='owner',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE,
                                    related_name='mentorships_owned', to=settings.AUTH_USER_MODEL),
        ),
        migrations.AddField(
            model_name='mentorship',
            name='status',
            field=models.CharField(choices=[('confirmed', 'Confirmed'), ('pending', 'Waiting for confirmation')],
                                   db_index=True, default='confirmed', max_length=16),
        ),
        migrations.AddField(
            model_name='mentorship',
            name='counterpart',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                                    related_name='+', to='YSE_App.mentorship'),
        ),
        migrations.RunPython(_status_db_default, migrations.RunPython.noop),
        migrations.RunPython(_split_sides, migrations.RunPython.noop),
    ]

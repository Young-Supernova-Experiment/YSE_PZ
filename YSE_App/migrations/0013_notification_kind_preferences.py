# Per-kind notification preference matrix (issue #321, part of #320).

import YSE_App.models.fields
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('YSE_App', '0012_job_queue_notifications'),
    ]

    operations = [
        migrations.AddField(
            model_name='notificationpreference',
            name='kinds',
            field=YSE_App.models.fields.JSONTextField(blank=True, help_text='Per-kind-group channel switches: {group: {in_app, email, slack}}; missing entries use the defaults.', null=True),
        ),
    ]

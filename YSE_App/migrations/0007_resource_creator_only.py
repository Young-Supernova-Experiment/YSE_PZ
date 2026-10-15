from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("YSE_App", "0006_followup_visibility"),
    ]

    operations = [
        migrations.AddField(
            model_name="classicalresource",
            name="creator_only",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="queuedresource",
            name="creator_only",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="tooresource",
            name="creator_only",
            field=models.BooleanField(default=False),
        ),
    ]

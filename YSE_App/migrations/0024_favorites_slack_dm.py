# Favorite transients (#323) and the Slack DM member id on notification preferences (#321); umbrella #320.


from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('YSE_App', '0023_feeds'),
    ]

    operations = [
        migrations.AddField(
            model_name='notificationpreference',
            name='slack_user_id',
            field=models.CharField(blank=True, db_index=True, default='', help_text="Slack member id (U…) for direct messages from the site's Slack app; looked up from the account email on the preferences page. Follows the Slack column of the matrix.", max_length=32),
        ),
        migrations.CreateModel(
            name='UserFavoriteTransient',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created', models.DateTimeField(auto_now_add=True)),
                ('transient', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='favorited_by', to='YSE_App.transient')),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='favorite_transients', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'verbose_name': 'favorite transient',
                'ordering': ('-created', '-id'),
            },
        ),
        migrations.AddIndex(
            model_name='userfavoritetransient',
            index=models.Index(fields=['transient', 'user'], name='yse_fav_transient_user_idx'),
        ),
        migrations.AlterUniqueTogether(
            name='userfavoritetransient',
            unique_together={('user', 'transient')},
        ),
    ]

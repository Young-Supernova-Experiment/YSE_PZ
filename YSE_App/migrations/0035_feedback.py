"""Feedback table for the site-wide Feedback button (#443).

A new table only (``YSE_App_feedback``); no existing table changes, so it is
safe on the shared ``YSE_test`` database (``develop`` never reads or writes it).
"""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('YSE_App', '0034_transient_photstat_view'),
    ]

    operations = [
        migrations.CreateModel(
            name='Feedback',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('kind', models.CharField(choices=[('bug', 'Bug'), ('idea', 'Idea'), ('question', 'Question')], default='bug', max_length=16)),
                ('title', models.CharField(blank=True, default='', max_length=200)),
                ('text', models.TextField()),
                ('page_url', models.CharField(blank=True, default='', help_text='Site path and query the feedback was sent from (credential-like parameters removed).', max_length=500)),
                ('created', models.DateTimeField(auto_now_add=True)),
                ('status', models.CharField(choices=[('new', 'New'), ('reviewed', 'Reviewed'), ('posted', 'Posted to GitHub'), ('dismissed', 'Dismissed')], default='new', max_length=16)),
                ('github_issue_url', models.URLField(blank=True, default='', max_length=500)),
                ('reviewed_at', models.DateTimeField(blank=True, null=True)),
                ('reviewed_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
                ('user', models.ForeignKey(blank=True, help_text='Who sent it (kept here only; never included in a GitHub issue).', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='feedback_items', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'verbose_name': 'feedback',
                'verbose_name_plural': 'feedback',
                'ordering': ('-created', '-id'),
            },
        ),
        migrations.AddIndex(
            model_name='feedback',
            index=models.Index(fields=['user', 'created'], name='yse_feedback_user_created'),
        ),
        migrations.AddIndex(
            model_name='feedback',
            index=models.Index(fields=['status', 'created'], name='yse_feedback_status_created'),
        ),
    ]

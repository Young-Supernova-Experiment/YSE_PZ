# Transient interests (#288: #289, #290) and data access requests (#291: #292, #293).

import YSE_App.models.fields
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('auth', '0012_alter_user_first_name_max_length'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('YSE_App', '0017_broker_candidates'),
    ]

    operations = [
        migrations.CreateModel(
            name='TransientInterest',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_date', models.DateTimeField(auto_now_add=True)),
                ('modified_date', models.DateTimeField(auto_now=True)),
                ('title', models.CharField(help_text="Planned paper or project, e.g. 'Nebular spectroscopy of 2026abc'.", max_length=200)),
                ('description', models.TextField(blank=True, default='', help_text='Short description: scope, data needed, timeline.')),
                ('role', models.CharField(choices=[('lead', 'Lead author'), ('coauthor', 'Co-author'), ('observer', 'Observer / data contributor')], default='lead', max_length=16)),
                ('status', models.CharField(choices=[('planned', 'Planned'), ('in_progress', 'In progress'), ('submitted', 'Submitted'), ('published', 'Published'), ('withdrawn', 'Withdrawn')], db_index=True, default='planned', max_length=16)),
                ('doi', models.CharField(blank=True, default='', help_text='DOI or arXiv id once published.', max_length=128)),
                ('created_by', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='transientinterest_created_by', to=settings.AUTH_USER_MODEL)),
                ('group', models.ForeignKey(blank=True, help_text='Collaboration the paper is written under; also the audience of the automatic comment.', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='transient_interests', to='auth.group')),
                ('modified_by', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='transientinterest_modified_by', to=settings.AUTH_USER_MODEL)),
                ('transient', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='interests', to='YSE_App.transient')),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='transient_interests', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ('-created_date', '-id'),
            },
        ),
        migrations.CreateModel(
            name='DataAccessRequest',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_date', models.DateTimeField(auto_now_add=True)),
                ('modified_date', models.DateTimeField(auto_now=True)),
                ('dataset_kind', models.CharField(choices=[('photometry', 'Photometry'), ('spectrum', 'Spectrum')], max_length=16)),
                ('dataset_id', models.PositiveIntegerField(blank=True, help_text='One TransientPhotometry / TransientSpectrum id; empty = every restricted dataset of this kind on the transient that the owner group holds.', null=True)),
                ('message', models.TextField(blank=True, default='')),
                ('status', models.CharField(choices=[('pending', 'Pending'), ('accepted', 'Accepted'), ('declined', 'Declined')], db_index=True, default='pending', max_length=16)),
                ('decided_at', models.DateTimeField(blank=True, null=True)),
                ('note', models.TextField(blank=True, default='', help_text='Decision note shown to the requester.')),
                ('granted_dataset_ids', YSE_App.models.fields.JSONTextField(blank=True, help_text='Ids of the datasets the target group was added to on acceptance (for audit / revocation).', null=True)),
                ('created_by', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='dataaccessrequest_created_by', to=settings.AUTH_USER_MODEL)),
                ('decided_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='data_access_decisions', to=settings.AUTH_USER_MODEL)),
                ('modified_by', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='dataaccessrequest_modified_by', to=settings.AUTH_USER_MODEL)),
                ('owner_group', models.ForeignKey(help_text='Group that owns the restricted data; its members decide.', on_delete=django.db.models.deletion.CASCADE, related_name='data_access_requests_owned', to='auth.group')),
                ('requester', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='data_access_requests', to=settings.AUTH_USER_MODEL)),
                ('target_group', models.ForeignKey(help_text="Requester's collaboration group that gains access on acceptance (never Public).", on_delete=django.db.models.deletion.CASCADE, related_name='data_access_requests_granted', to='auth.group')),
                ('transient', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='data_access_requests', to='YSE_App.transient')),
            ],
            options={
                'ordering': ('-created_date', '-id'),
            },
        ),
        migrations.AddIndex(
            model_name='transientinterest',
            index=models.Index(fields=['transient', 'status'], name='yse_interest_transient_status'),
        ),
        migrations.AddIndex(
            model_name='transientinterest',
            index=models.Index(fields=['user', 'status'], name='yse_interest_user_status'),
        ),
        migrations.AlterUniqueTogether(
            name='transientinterest',
            unique_together={('transient', 'user', 'title')},
        ),
        migrations.AddIndex(
            model_name='dataaccessrequest',
            index=models.Index(fields=['owner_group', 'status'], name='yse_dar_owner_status'),
        ),
        migrations.AddIndex(
            model_name='dataaccessrequest',
            index=models.Index(fields=['requester', 'status'], name='yse_dar_requester_status'),
        ),
        migrations.AddIndex(
            model_name='dataaccessrequest',
            index=models.Index(fields=['transient', 'dataset_kind'], name='yse_dar_transient_kind'),
        ),
    ]

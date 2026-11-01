# Mentorship kinds "Former advisor" and "Postdoc advisor". Choices only: no SQL.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('YSE_App', '0053_related_name_as_declared'),
    ]

    operations = [
        migrations.AlterField(
            model_name='mentorship',
            name='kind',
            field=models.CharField(choices=[('phd_advisor', 'PhD advisor'), ('current_advisor', 'Current advisor'), ('former_advisor', 'Former advisor'), ('postdoc_advisor', 'Postdoc advisor'), ('research_supervisor', 'Research supervisor')], max_length=32),
        ),
    ]

"""Load version 1 of the voluntary research consent form (#744), text verbatim from Ryan Foley.

``effective_date`` (the go-live date) is the day this migration first runs on a database.
Activity before that date is exempt and may be used anonymized (except for users who
withdraw); activity from that date on is used only for users who consented.

Idempotent: does nothing if a version "1" already exists (develop and experimental both apply
migrations to the shared YSE_test database).
"""

import datetime

from django.db import migrations

VERSION = "1"
TITLE = "YSE-PZ Research Consent Form"  # "(v1)" dropped in place by 0042 (#766)
BODY = """<p>YSE-PZ records how the platform is used: requests, priorities, plans, observing outcomes, comments and similar activity.  This study is looking to learn more about how scientists make decisions, how to more efficiently use limited resources, and how to identify interesting science opportunities faster.</p>
<p>If you agree to be in this study, you will provide basic demographic information such as career stage and country to track science outcomes. Your participation will take about 5 minutes.</p>
<p>We will keep all study information private and only use anonymized data for any study.  There is a small risk that your personal information could be shared by mistake. However, researchers take steps to protect your privacy, including encrypting all information that is not essential for running the site.</p>
<p>Your participation is completely voluntary. You can stop participating at any time. If you choose not to participate or stop later, it will not affect your grade, job, or any benefits you’re entitled to.</p>
<p>If you have any questions or want more information, you can contact Prof. Ryan Foley at foley@ucsc.edu.</p>
<p>By agreeing to participate, you are saying that:</p>
<ul>
<li>You are at least 18 years old.</li>
<li>You have read and understand this form.</li>
<li>You agree to participate in the study.</li>
</ul>
"""


def seed(apps, schema_editor):
    ResearchConsentForm = apps.get_model("YSE_App", "ResearchConsentForm")
    if ResearchConsentForm.objects.filter(version=VERSION).exists():
        return
    current = not ResearchConsentForm.objects.filter(is_current=True).exists()
    ResearchConsentForm.objects.create(
        version=VERSION, title=TITLE, body=BODY,
        effective_date=datetime.date.today(), is_current=current,
    )


def unseed(apps, schema_editor):
    ResearchConsentForm = apps.get_model("YSE_App", "ResearchConsentForm")
    ResearchConsentForm.objects.filter(version=VERSION, records__isnull=True).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('YSE_App', '0039_research_consent'),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]

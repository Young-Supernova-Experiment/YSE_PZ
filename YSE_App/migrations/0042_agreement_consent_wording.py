"""Wording edits to the current User Agreement v2 and Research Consent Form v1, in place (#766).

* Titles lose their "(v2)" / "(v1)" suffix.
* Agreement item 3 loses the parenthesis about citing the software and PASP papers.
* The consent form says "basic demographic information such as career stage and country".

These are edits in place, so version numbers, effective dates, acceptances and consent records
are untouched and nobody is asked to accept again. Idempotent (develop and experimental both
apply migrations to the shared YSE_test database): each replacement is a no-op once applied, and
fresh databases already get the new text from 0038 and 0040. Reverse is a no-op.
"""

from django.db import migrations

AGREEMENT_VERSION = "2"
AGREEMENT_TITLE_OLD = "YSE-PZ User Agreement (v2)"
AGREEMENT_TITLE_NEW = "YSE-PZ User Agreement"
AGREEMENT_BODY_OLD = " (cite the YSE-PZ software and PASP papers and include the listed acknowledgment)"
AGREEMENT_BODY_NEW = ""

CONSENT_VERSION = "1"
CONSENT_TITLE_OLD = "YSE-PZ Research Consent Form (v1)"
CONSENT_TITLE_NEW = "YSE-PZ Research Consent Form"
CONSENT_BODY_OLD = "basic demographic information to track science outcomes"
CONSENT_BODY_NEW = "basic demographic information such as career stage and country to track science outcomes"


def _edit(model, version, title_old, title_new, body_old, body_new):
    for row in model.objects.filter(version=version):
        fields = []
        if row.title == title_old:
            row.title = title_new
            fields.append("title")
        if body_old in row.body:
            row.body = row.body.replace(body_old, body_new)
            fields.append("body")
        if fields:
            row.save(update_fields=fields)


def apply_edits(apps, schema_editor):
    _edit(apps.get_model("YSE_App", "UserAgreement"), AGREEMENT_VERSION,
          AGREEMENT_TITLE_OLD, AGREEMENT_TITLE_NEW, AGREEMENT_BODY_OLD, AGREEMENT_BODY_NEW)
    _edit(apps.get_model("YSE_App", "ResearchConsentForm"), CONSENT_VERSION,
          CONSENT_TITLE_OLD, CONSENT_TITLE_NEW, CONSENT_BODY_OLD, CONSENT_BODY_NEW)


class Migration(migrations.Migration):

    dependencies = [
        ('YSE_App', '0041_research_consent_prompts'),
    ]

    operations = [
        migrations.RunPython(apply_edits, migrations.RunPython.noop),
    ]

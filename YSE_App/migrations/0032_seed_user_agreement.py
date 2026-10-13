"""Load version 1 of the YSE-PZ User Agreement (#630), as approved by Ryan Foley.

``{acknowledgements_url}`` in the body is replaced at render time with this
stack's URL of the Acknowledgements page (``reverse('acknowledgements')``), so
the link works under /yse/, /yse_test/ and /yse_experimental/.

Idempotent: does nothing if a version "1" already exists (both develop and
experimental apply it to the shared YSE_test database).
"""

import datetime

from django.db import migrations

VERSION = "1"
TITLE = "YSE-PZ User Agreement (v1)"
BODY = """<p>By using YSE-PZ you agree to the following.</p>
<ol>
<li><strong>Your account.</strong> Your account is for you alone. Do not share your login or credentials. Keep your contact and profile information current.</li>
<li><strong>Data that isn't yours.</strong> Do not publish, present, share or redistribute data, classifications or results that you did not obtain yourself, unless you have permission from the data's owner or PI, or the data are explicitly public. Respect group access restrictions and embargoes. Follow collaboration rules for reporting to TNS and other services.</li>
<li><strong>Acknowledgment.</strong> Acknowledge YSE-PZ in publications and presentations that use the platform or its data, as described on the YSE-PZ <a href="{acknowledgements_url}">Acknowledgements page</a> (cite the YSE-PZ software and PASP papers and include the listed acknowledgment), and follow any acknowledgment requested by the data's owners or programs.</li>
<li><strong>No guarantee; back up your data.</strong> YSE-PZ is provided as is, without warranty. Data may be lost, corrupted or become unavailable. Keep your own copies of anything important; YSE-PZ must not be the only copy of your data.</li>
<li><strong>Use of platform data for research.</strong> YSE-PZ records how the platform is used: requests, priorities, plans, observing outcomes, comments and similar activity. By accepting this agreement, you allow the YSE-PZ team to use this information, including information entered before you accepted, for studies of scientific decision-making, resource allocation and platform improvement. <strong>Only anonymized data will be used.</strong> Names, usernames and other identifying details are removed before any analysis, and no individual will be identified in any result, presentation or publication.</li>
<li><strong>Conduct.</strong> Use YSE-PZ professionally and in line with the collaboration's code of conduct. Do not attempt to bypass access controls or misuse others' data.</li>
<li><strong>Enforcement.</strong> The YSE-PZ administrators may suspend or remove access, and data access, for anyone who violates this agreement or misuses the platform.</li>
<li><strong>Privacy.</strong> Profile information is used to run the platform and for authorship and acknowledgment. Optional demographic information (such as gender) is stored encrypted. Administrators cannot read it, and it is decrypted only inside anonymized research datasets. You may update or delete optional fields at any time.</li>
<li><strong>Changes.</strong> This agreement may change. You will be asked to accept new versions before continuing to use YSE-PZ.</li>
</ol>
"""


def seed(apps, schema_editor):
    UserAgreement = apps.get_model("YSE_App", "UserAgreement")
    if UserAgreement.objects.filter(version=VERSION).exists():
        return
    current = not UserAgreement.objects.filter(is_current=True).exists()
    UserAgreement.objects.create(
        version=VERSION, title=TITLE, body=BODY,
        effective_date=datetime.date(2026, 10, 12), is_current=current,
    )


def unseed(apps, schema_editor):
    UserAgreement = apps.get_model("YSE_App", "UserAgreement")
    UserAgreement.objects.filter(version=VERSION, acceptances__isnull=True).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('YSE_App', '0031_researcher_profile'),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]

"""Load version 2 of the YSE-PZ User Agreement (#743) and make it the current version.

Changes from v1: item 5 ("Use of platform data for research") is removed, because research
use is now a separate, voluntary consent form (#744); "(such as gender)" is removed from the
Privacy item; the remaining items are renumbered 1-8. Nothing else changes.

v1 and its acceptances stay on record. Every user is asked to accept v2 by the existing
onboarding gate.

Idempotent: does nothing if a version "2" already exists (develop and experimental both apply
migrations to the shared YSE_test database).

Migration numbering: develop's 0038. Experimental has its own 0038-0050 under other names; the
develop -> experimental backport adds a merge migration for both leaves.
"""

import datetime

from django.db import migrations

VERSION = "2"
PREVIOUS_VERSION = "1"
TITLE = "YSE-PZ User Agreement"  # "(v2)" dropped in place by 0042 (#766)
BODY = """<p>By using YSE-PZ you agree to the following.</p>
<ol>
<li><strong>Your account.</strong> Your account is for you alone. Do not share your login or credentials. Keep your contact and profile information current.</li>
<li><strong>Data that isn't yours.</strong> Do not publish, present, share or redistribute data, classifications or results that you did not obtain yourself, unless you have permission from the data's owner or PI, or the data are explicitly public. Respect group access restrictions and embargoes. Follow collaboration rules for reporting to TNS and other services.</li>
<li><strong>Acknowledgment.</strong> Acknowledge YSE-PZ in publications and presentations that use the platform or its data, as described on the YSE-PZ <a href="{acknowledgements_url}">Acknowledgements page</a>, and follow any acknowledgment requested by the data's owners or programs.</li>
<li><strong>No guarantee; back up your data.</strong> YSE-PZ is provided as is, without warranty. Data may be lost, corrupted or become unavailable. Keep your own copies of anything important; YSE-PZ must not be the only copy of your data.</li>
<li><strong>Conduct.</strong> Use YSE-PZ professionally and in line with the collaboration's code of conduct. Do not attempt to bypass access controls or misuse others' data.</li>
<li><strong>Enforcement.</strong> The YSE-PZ administrators may suspend or remove access, and data access, for anyone who violates this agreement or misuses the platform.</li>
<li><strong>Privacy.</strong> Profile information is used to run the platform and for authorship and acknowledgment. Optional demographic information is stored encrypted. Administrators cannot read it, and it is decrypted only inside anonymized research datasets. You may update or delete optional fields at any time.</li>
<li><strong>Changes.</strong> This agreement may change. You will be asked to accept new versions before continuing to use YSE-PZ.</li>
</ol>
"""


def seed(apps, schema_editor):
    UserAgreement = apps.get_model("YSE_App", "UserAgreement")
    if UserAgreement.objects.filter(version=VERSION).exists():
        return
    # Historical models skip UserAgreement.save(), so clear the other current flags here.
    UserAgreement.objects.filter(is_current=True).update(is_current=False)
    UserAgreement.objects.create(
        version=VERSION, title=TITLE, body=BODY,
        effective_date=datetime.date.today(), is_current=True,
    )


def unseed(apps, schema_editor):
    UserAgreement = apps.get_model("YSE_App", "UserAgreement")
    deleted, _ = UserAgreement.objects.filter(version=VERSION, acceptances__isnull=True).delete()
    if deleted and not UserAgreement.objects.filter(is_current=True).exists():
        UserAgreement.objects.filter(version=PREVIOUS_VERSION).update(is_current=True)


class Migration(migrations.Migration):

    dependencies = [
        ('YSE_App', '0037_data_sharing_delegates'),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]

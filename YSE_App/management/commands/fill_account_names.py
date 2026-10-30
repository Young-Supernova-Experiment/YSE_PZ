"""Fill empty account given/family names from unambiguous profile full names.

The same rule as migration 0060 (YSE_App/common/names.py); prints counts only, never names.
"""

from django.core.management.base import BaseCommand

from YSE_App.common.names import split_full_name
from YSE_App.models import ResearcherProfile
from YSE_App.services.researcher_profile import fill_account_names


class Command(BaseCommand):
    help = "Fill empty account first/last names from profile full names when the split is unambiguous."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        profiles = (ResearcherProfile.objects.filter(user__first_name="", user__last_name="")
                    .select_related("user"))
        filled = left = 0
        for profile in profiles.iterator():
            if split_full_name(profile.full_name) is None:
                left += 1
            elif options["dry_run"] or fill_account_names(profile.user, profile):
                filled += 1
        verb = "would fill" if options["dry_run"] else "filled"
        self.stdout.write("account names: %s %d; %d left for their owners to enter" % (verb, filled, left))

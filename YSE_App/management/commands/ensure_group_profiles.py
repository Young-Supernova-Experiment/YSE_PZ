"""Create a GroupProfile for every group that lacks one and mark system groups (#445, #440).

Migration 0034_seed_group_profiles already does this once; run it again after creating
groups with code that predates group profiles. Never creates owners.
"""

from django.core.management.base import BaseCommand

from YSE_App.services.groups import ensure_all_profiles


class Command(BaseCommand):
    help = "Create missing group profiles (no owners) and mark system groups."

    def handle(self, *args, **options):
        created, marked = ensure_all_profiles()
        self.stdout.write("group profiles: created %d, marked system %d" % (created, marked))

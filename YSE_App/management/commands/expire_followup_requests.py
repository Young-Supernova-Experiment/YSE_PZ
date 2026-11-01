"""Close classical follow-up requests whose window ended without good data (#673).

  python manage.py expire_followup_requests                      # dry run: report only
  python manage.py expire_followup_requests --apply              # change the statuses
  python manage.py expire_followup_requests --since 2026-11-01   # instead of ASTROSCHEDULER_EXPIRE_AFTER

Only follow-ups on a classical resource, still Requested or InProcess, whose window ended on or
after the start date. No observation or only failed/punted ones: Failed. A good observation:
Successful. Data taken but not yet judged: left alone. The daily ExpireFollowupRequests cron
runs this with --apply while ASTROSCHEDULER_EXPIRE_AFTER is set.
"""

import datetime

from django.core.management.base import BaseCommand, CommandError

from YSE_App.services.observations import expire_requests


class Command(BaseCommand):
    help = "Close classical follow-up requests that ended without good data (dry run unless --apply)."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Change the statuses (default: report only).")
        parser.add_argument("--since", help="Start date YYYY-MM-DD (default: ASTROSCHEDULER_EXPIRE_AFTER).")

    def handle(self, *args, **options):
        since = None
        if options.get("since"):
            try:
                day = datetime.date.fromisoformat(options["since"])
            except ValueError:
                raise CommandError("--since must be YYYY-MM-DD")
            since = datetime.datetime.combine(day, datetime.time(0, 0), tzinfo=datetime.timezone.utc)
        result = expire_requests(apply=options["apply"], since=since)
        if result["since"] is None:
            self.stdout.write("Off: set ASTROSCHEDULER_EXPIRE_AFTER or pass --since.")
            return
        verb = "Changed" if result["applied"] else "Would change"
        self.stdout.write("%s: %d to Failed, %d to Successful; %d left for a judgement (data taken)." % (
            verb, len(result["failed"]), len(result["successful"]), len(result["pending"])))
        for label in ("failed", "successful", "pending"):
            if result[label]:
                self.stdout.write("  %s: follow-ups %s" % (label, ", ".join(str(i) for i in result[label])))

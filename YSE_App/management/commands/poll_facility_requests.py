"""Poll open facility requests at facilities with a status endpoint (#300).

    python manage.py poll_facility_requests            # poll now, print counts
    python manage.py poll_facility_requests --enqueue  # queue a facility.poll job instead
"""

from django.core.management.base import BaseCommand

from YSE_App.services import facility_requests as fr


class Command(BaseCommand):
    help = "Poll open facility requests (LCO and other adapters with a status endpoint) and update their state."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=200)
        parser.add_argument("--enqueue", action="store_true", help="Enqueue a background job instead of polling now.")

    def handle(self, *args, **options):
        if options["enqueue"]:
            from YSE_App.jobs import enqueue

            job = enqueue(fr.POLL_JOB_KIND, {"limit": options["limit"]})
            self.stdout.write("queued facility.poll job %s" % job.pk)
            return
        counts = fr.poll_open_requests(limit=options["limit"])
        self.stdout.write("polled=%(polled)d changed=%(changed)d errors=%(errors)d skipped=%(skipped)d" % counts)

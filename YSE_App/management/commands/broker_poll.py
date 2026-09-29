"""``manage.py broker_poll``: poll one broker's enabled filters now (issue #278, polling half).

Runs the same code as the ``brokers.ingest`` job, in this process, so an
operator can test a new ``BrokerFilter`` (``--dry-run`` evaluates without
writing candidates). ``--enqueue`` puts the job on the queue instead.
"""

import json

from django.core.management.base import BaseCommand, CommandError

from YSE_App.brokers import ingest, registry
from YSE_App.brokers.base import BrokerError
from YSE_App.brokers.jobs import enqueue_ingest


class Command(BaseCommand):
    help = "Poll a broker's enabled BrokerFilters and register passing alerts as candidates."

    def add_arguments(self, parser):
        parser.add_argument("--broker", required=True, help="provider slug (see manage.py brokers)")
        parser.add_argument("--filter-id", action="append", type=int, default=[], help="only these BrokerFilter ids")
        parser.add_argument("--limit", type=int, default=None, help="alerts per filter (default: the filter's max_alerts)")
        parser.add_argument("--dry-run", action="store_true", help="evaluate only; write no candidates")
        parser.add_argument("--enqueue", action="store_true", help="enqueue a brokers.ingest job instead of running here")

    def handle(self, *args, **options):
        broker = options["broker"]
        if registry.provider_class(broker) is None:
            raise CommandError("unknown broker %r (known: %s)" % (broker, ", ".join(registry.registered_slugs())))
        if options["enqueue"]:
            job = enqueue_ingest(broker, filter_ids=options["filter_id"], limit=options["limit"], dry_run=options["dry_run"])
            self.stdout.write("enqueued job %s (%s)" % (job.pk, job.kind))
            return
        try:
            result = ingest.run_ingest(broker, filter_ids=options["filter_id"] or None, limit=options["limit"],
                                       dry_run=options["dry_run"])
        except BrokerError as exc:
            raise CommandError(str(exc))
        self.stdout.write(json.dumps(result, indent=2, default=str))

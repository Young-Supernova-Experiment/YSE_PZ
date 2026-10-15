"""Register the built-in annotation services (#318): Gaia DR3, WISE colour and quasar-catalogue checks.

    python manage.py register_annotation_services            # all three, enabled
    python manage.py register_annotation_services --disabled # create them switched off
    python manage.py register_annotation_services --user dcoulter

Each becomes an ``ExternalService`` of kind ``annotation`` (slugs ``gaia_dr3``,
``wise``, ``quasar``); the runner registered for the slug executes queued runs.
Audience groups, caps and the enabled flag are edited in the admin afterwards.
"""

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError

from YSE_App.services.annotations import ensure_builtin_services


class Command(BaseCommand):
    help = "Create (or refresh) the built-in annotation services: gaia_dr3, wise, quasar."

    def add_arguments(self, parser):
        parser.add_argument("--user", default="", help="username recorded as creator (default: first superuser)")
        parser.add_argument("--disabled", action="store_true", help="create the services switched off")

    def handle(self, *args, **options):
        if options["user"]:
            user = User.objects.filter(username=options["user"]).first()
            if user is None:
                raise CommandError("no user named %r" % options["user"])
        else:
            user = User.objects.filter(is_superuser=True).order_by("pk").first() or User.objects.order_by("pk").first()
            if user is None:
                raise CommandError("no users exist yet; create one first")
        for service in ensure_builtin_services(user, enabled=not options["disabled"]):
            self.stdout.write("%s [%s] %s" % (service.name, service.slug, "enabled" if service.enabled else "disabled"))

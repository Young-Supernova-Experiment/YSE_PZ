"""Create (or update) a TNS ``SharingService`` and its encrypted bot credential (#325).

Migrates the settings.ini bot configuration (``tns_bot_id`` / ``tns_bot_name`` /
``tnsapikey``, or the DECam bot with ``--decam``) into one service row, or takes
the values on the command line::

    python manage.py create_tns_sharing_service --slug yse-tns --name "YSE TNS bot" --group-id 83 \
        --group-name YSE --from-settings --coauthors "R. J. Foley (UCSC), D. O. Jones (Hawaii)"
    python manage.py create_tns_sharing_service --slug decam --name "DECam TNS bot" --group-id 83 --decam
    python manage.py create_tns_sharing_service --slug yse-tns --bot-id 12345 --bot-name YSE_Bot --api-key ... --production

The service starts in sandbox mode unless ``--production`` is given.
"""

from __future__ import annotations

from django.conf import settings
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError
from django.utils.text import slugify

from YSE_App.models import EncryptedCredential, SharingService


def _clean(value):
    value = (value or "").strip()
    return "" if value.startswith("<") else value


class Command(BaseCommand):
    help = "Create or update a TNS sharing service with an encrypted bot credential."

    def add_arguments(self, parser):
        parser.add_argument("--slug", required=True, help="Service slug, e.g. yse-tns or decam.")
        parser.add_argument("--name", default="", help="Display name (default: derived from the slug).")
        parser.add_argument("--group-id", default="", help="TNS reporting group id (numeric).")
        parser.add_argument("--group-name", default="", help="Reporting group name for 'on behalf of'.")
        parser.add_argument("--coauthors", default="", help="Default reporter list.")
        parser.add_argument("--from-settings", action="store_true",
                            help="Take bot id/name/key from settings.ini [main] tns_bot_id / tns_bot_name / tnsapikey.")
        parser.add_argument("--decam", action="store_true",
                            help="Take the DECam bot from settings.ini (tns_decam_bot_id / tns_decam_bot_name / tnsdecamapikey).")
        parser.add_argument("--bot-id", default="")
        parser.add_argument("--bot-name", default="")
        parser.add_argument("--api-key", default="")
        parser.add_argument("--production", action="store_true", help="Report to the real TNS (default: sandbox).")
        parser.add_argument("--sandbox", action="store_true", help="Force sandbox mode on an existing service.")
        parser.add_argument("--user", default="", help="Username to record as creator (default: first superuser).")

    def handle(self, *args, **options):
        slug = slugify(options["slug"])
        if not slug:
            raise CommandError("--slug is required")
        bot_id, bot_name, api_key = options["bot_id"], options["bot_name"], options["api_key"]
        if options["decam"]:
            bot_id = bot_id or _clean(getattr(settings, "TNSDECAMID", ""))
            bot_name = bot_name or _clean(getattr(settings, "TNSDECAMUSER", ""))
            api_key = api_key or _clean(getattr(settings, "TNSDECAMAPIKEY", ""))
        elif options["from_settings"]:
            bot_id = bot_id or _clean(getattr(settings, "TNSID", ""))
            bot_name = bot_name or _clean(getattr(settings, "TNSUSER", ""))
            api_key = api_key or _clean(getattr(settings, "TNSAPIKEY", ""))
        user = None
        if options["user"]:
            user = User.objects.filter(username=options["user"]).first()
            if user is None:
                raise CommandError("no user %r" % options["user"])
        user = user or User.objects.filter(is_superuser=True).order_by("pk").first()
        if user is None:
            raise CommandError("no superuser to attribute the rows to; pass --user")

        service = SharingService.objects.filter(slug=slug).first()
        created = service is None
        if created:
            service = SharingService(slug=slug, kind=SharingService.KIND_TNS, created_by=user,
                                     testing=bool(getattr(settings, "SHARING_DEFAULT_TESTING", True)))
        service.modified_by = user
        service.name = options["name"] or service.name or slug.replace("-", " ").title()
        if options["group_id"]:
            service.tns_group_id = options["group_id"]
        if options["group_name"]:
            service.tns_group_name = options["group_name"]
        if options["coauthors"]:
            service.default_coauthors = options["coauthors"]
        if options["production"]:
            service.testing = False
        if options["sandbox"]:
            service.testing = True

        if bot_id and bot_name and api_key:
            cred = service.credential or EncryptedCredential.objects.filter(
                service="tns", name="TNS bot %s" % slug).first()
            if cred is None:
                cred = EncryptedCredential(name="TNS bot %s" % slug, service="tns", kind=EncryptedCredential.KIND_TNS,
                                           created_by=user)
            cred.modified_by = user
            cred.set_secret({"tns_bot_id": str(bot_id), "tns_bot_name": bot_name, "tns_api_key": api_key})
            cred.save()
            service.credential = cred
            self.stdout.write("credential %r stored (keys: tns_bot_id, tns_bot_name, tns_api_key)" % cred.name)
        elif any((bot_id, bot_name, api_key)):
            raise CommandError("bot id, bot name and api key are all required to store a credential")
        elif created:
            self.stdout.write(self.style.WARNING(
                "no bot credential given; set one in the admin (Encrypted credentials) and link it to the service"))
        service.save()
        self.stdout.write(self.style.SUCCESS("%s sharing service %r (%s, group id %s, %s)" % (
            "created" if created else "updated", service.slug, service.name, service.tns_group_id or "unset",
            "SANDBOX" if service.testing else "PRODUCTION")))

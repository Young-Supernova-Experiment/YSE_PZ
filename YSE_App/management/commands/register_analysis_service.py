"""Register (or update) an analysis service (#313).

    # the two shipped in-process runners
    python manage.py register_analysis_service --builtin sncosmo_fit
    python manage.py register_analysis_service --builtin bazin_fit --cap 20 --group YSE

    # a webhook service; results come back on POST /api/service_runs/<uuid>/callback/
    python manage.py register_analysis_service ngsf "NGSF spectral matching" \\
        --url https://ngsf.example.org/run --input spectra redshift --output results plots files \\
        --credential "NGSF token" --timeout 1800 --cap 10

    # any importable module with run(payload, params)
    python manage.py register_analysis_service mymodel "My model" --runner mypkg.yse_runner
"""

import json

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError

from YSE_App.analysis import BUILTIN_RUNNERS
from YSE_App.models import EncryptedCredential
from YSE_App.services.analysis_services import AnalysisConfigError, register_service


class Command(BaseCommand):
    help = "Create or update an analysis service (in-process runner or webhook)."

    def add_arguments(self, parser):
        parser.add_argument("slug", nargs="?", help="service slug (defaults to the built-in name)")
        parser.add_argument("name", nargs="?", default="", help="display name")
        parser.add_argument("--builtin", choices=sorted(BUILTIN_RUNNERS), help="ship one of the built-in runners")
        parser.add_argument("--runner", default="", help="dotted path of a module exposing run(payload, params)")
        parser.add_argument("--url", default="", help="webhook base URL the payload is POSTed to")
        parser.add_argument("--input", nargs="*", default=None, help="photometry spectra redshift host (or all)")
        parser.add_argument("--output", nargs="*", default=None, help="results plots files")
        parser.add_argument("--param-schema", default=None, help="JSON object describing the parameter form")
        parser.add_argument("--defaults", default=None, help="JSON object of default parameters")
        parser.add_argument("--timeout", type=int, default=None, help="seconds before a run is given up")
        parser.add_argument("--cap", type=int, default=None, help="runs per user per day (0 = unlimited)")
        parser.add_argument("--group", action="append", default=None, help="restrict to this group (repeatable)")
        parser.add_argument("--credential", default=None, help="name of an EncryptedCredential (api_token / token used as bearer)")
        parser.add_argument("--description", default="")
        parser.add_argument("--disable", action="store_true", help="register but leave the service disabled")
        parser.add_argument("--user", default="", help="username recorded as creator (default: first superuser)")

    def handle(self, *args, **options):
        runner = options["runner"] or (options["builtin"] or "")
        slug = options["slug"] or options["builtin"]
        if not slug:
            raise CommandError("give a slug or --builtin")
        if bool(runner) == bool(options["url"]):
            raise CommandError("give exactly one of --builtin/--runner (in-process) or --url (webhook)")
        param_schema = defaults = None
        try:
            if options["param_schema"]:
                param_schema = json.loads(options["param_schema"])
            if options["defaults"]:
                defaults = json.loads(options["defaults"])
        except ValueError as exc:
            raise CommandError("bad JSON: %s" % exc)
        credential = None
        if options["credential"]:
            credential = EncryptedCredential.objects.filter(name=options["credential"]).first()
            if credential is None:
                raise CommandError("no EncryptedCredential named %r" % options["credential"])
        user = None
        if options["user"]:
            user = User.objects.filter(username=options["user"]).first()
            if user is None:
                raise CommandError("no user %r" % options["user"])
        else:
            user = User.objects.filter(is_superuser=True).order_by("pk").first()
        try:
            profile = register_service(
                slug, options["name"], runner=runner, base_url=options["url"], user=user,
                description=options["description"], input_spec=options["input"], output_spec=options["output"],
                param_schema=param_schema, default_params=defaults, timeout_seconds=options["timeout"],
                max_runs_per_user_per_day=options["cap"], groups=options["group"], credential=credential,
                enabled=not options["disable"],
            )
        except AnalysisConfigError as exc:
            raise CommandError(str(exc))
        except Exception as exc:  # unknown group, ...
            raise CommandError("%s: %s" % (type(exc).__name__, exc))
        self.stdout.write(self.style.SUCCESS(
            "%s analysis service %s (%s%s) input=%s output=%s timeout=%ss cap=%s groups=%s" % (
                "Registered", profile.service.slug, profile.runner_kind,
                ": " + (profile.runner_path or profile.service.base_url),
                ",".join(profile.input_spec or []), ",".join(profile.output_spec or []),
                profile.timeout_seconds, profile.service.max_runs_per_user_per_day or "none",
                ",".join(g.name for g in profile.service.groups.all()) or "everyone",
            )))

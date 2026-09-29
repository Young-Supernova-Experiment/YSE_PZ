"""Register the ``ai_summary`` service (#296) and, optionally, refresh the search embeddings.

    python manage.py register_summary_service                     # enabled, template provider
    python manage.py register_summary_service --provider anthropic --credential "Anthropic key"
    python manage.py register_summary_service --group YSE --cap 20 --disabled
    python manage.py register_summary_service --rebuild-embeddings

The row is an ordinary ``ExternalService`` of kind ``summary`` afterwards:
provider / model / api_base overrides live in its ``default_params``, the
audience in ``groups`` (empty = every user who opts in), the per-user daily cap
in ``max_runs_per_user_per_day`` and the API key in its ``credential``.
"""

from django.contrib.auth.models import Group, User
from django.core.management.base import BaseCommand, CommandError

from YSE_App.models.credential_models import EncryptedCredential
from YSE_App.services import llm
from YSE_App.services.summaries import ensure_service, rebuild_embeddings


class Command(BaseCommand):
    help = "Create (or refresh) the ai_summary external service; optionally rebuild the summary embeddings."

    def add_arguments(self, parser):
        parser.add_argument("--user", default="", help="username recorded as creator (default: first superuser)")
        parser.add_argument("--disabled", action="store_true", help="create the service switched off")
        parser.add_argument("--provider", default="", choices=("", *llm.PROVIDERS),
                            help="store a provider override in default_params (template, openai, anthropic)")
        parser.add_argument("--model", default="", help="store a model override in default_params")
        parser.add_argument("--api-base", default="", help="store an api_base override in default_params")
        parser.add_argument("--credential", default="", help="name (or service slug) of the Encrypted credential holding api_key")
        parser.add_argument("--group", action="append", default=[], help="restrict generation to this group (repeatable)")
        parser.add_argument("--cap", type=int, default=None, help="max runs per user per day (0 = unlimited)")
        parser.add_argument("--rebuild-embeddings", action="store_true", help="recompute every summary embedding")

    def handle(self, *args, **options):
        if options["user"]:
            user = User.objects.filter(username=options["user"]).first()
            if user is None:
                raise CommandError("no user named %r" % options["user"])
        else:
            user = User.objects.filter(is_superuser=True).order_by("pk").first() or User.objects.order_by("pk").first()
            if user is None:
                raise CommandError("no users exist yet; create one first")
        params = {k: options[v] for k, v in (("provider", "provider"), ("model", "model"), ("api_base", "api_base")) if options[v]}
        service = ensure_service(user, enabled=not options["disabled"], params=params)
        changed = []
        if options["credential"]:
            cred = EncryptedCredential.objects.filter(name=options["credential"]).first() \
                or EncryptedCredential.objects.filter(service=options["credential"]).first()
            if cred is None:
                raise CommandError("no Encrypted credential named %r" % options["credential"])
            service.credential = cred
            changed.append("credential")
        if options["cap"] is not None:
            service.max_runs_per_user_per_day = max(0, options["cap"])
            changed.append("max_runs_per_user_per_day")
        if changed:
            service.modified_by = user
            service.save(update_fields=changed + ["modified_by", "modified_date"])
        if options["group"]:
            groups = list(Group.objects.filter(name__in=options["group"]))
            missing = set(options["group"]) - {g.name for g in groups}
            if missing:
                raise CommandError("unknown group(s): %s" % ", ".join(sorted(missing)))
            service.groups.set(groups)
        ok, reason = llm.chat_available(service, service.default_params or {})
        cfg = llm.chat_config(service.default_params or {})
        self.stdout.write("%s [%s] %s; provider %s%s; %s" % (
            service.name, service.slug, "enabled" if service.enabled else "disabled", cfg["provider"],
            " model %s" % cfg["model"] if cfg["model"] else "", "ready" if ok else "NOT ready: " + reason))
        if options["rebuild_embeddings"]:
            self.stdout.write("embeddings rebuilt: %d" % rebuild_embeddings())

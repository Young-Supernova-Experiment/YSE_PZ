"""Deactivate accounts that have not logged in for N days (dry run unless --apply).

    python manage.py deactivate_inactive_users --days 365            # counts only, changes nothing
    python manage.py deactivate_inactive_users --days 365 --apply    # sets is_active = False

An account counts as inactive when it is active and its last login (or, if it
never logged in, the date it was created) is more than N days ago. Never
touched: superusers, the usernames in [security] INACTIVE_USER_EXEMPT_USERNAMES,
the configured service users (SLACK_SERVICE_USERNAME, BROKER_AUTO_SAVE_USERNAME,
SHARING_SYSTEM_USERNAME) and, unless --include-token-holders is given, accounts
that hold a DRF API token. Logins over Basic or token auth do not update the
last-login date, so every account a script or cron uses must be exempted.

The output is counts only; it never prints user names. Deactivation keeps the
account and its data; an admin can re-activate it (admin > Users > Active).
"""

from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q
from django.utils import timezone


def exempt_usernames():
    names = set(getattr(settings, "INACTIVE_USER_EXEMPT_USERNAMES", []) or [])
    for key in ("SLACK_SERVICE_USERNAME", "BROKER_AUTO_SAVE_USERNAME", "SHARING_SYSTEM_USERNAME"):
        value = (getattr(settings, key, "") or "").strip()
        if value:
            names.add(value)
    return names


def find_inactive(days, include_token_holders=False, now=None):
    """Return (queryset of accounts to deactivate, dict of skipped counts)."""
    User = get_user_model()
    cutoff = (now or timezone.now()) - timedelta(days=days)
    stale = User.objects.filter(is_active=True).filter(
        Q(last_login__lt=cutoff) | Q(last_login__isnull=True, date_joined__lt=cutoff)
    )
    skipped = {"superusers": stale.filter(is_superuser=True).count()}
    candidates = stale.exclude(is_superuser=True)
    exempt = exempt_usernames()
    skipped["exempt"] = candidates.filter(username__in=exempt).count()
    candidates = candidates.exclude(username__in=exempt)
    if include_token_holders:
        skipped["token_holders"] = 0
    else:
        skipped["token_holders"] = candidates.filter(auth_token__isnull=False).count()
        candidates = candidates.exclude(auth_token__isnull=False)
    return candidates, skipped


class Command(BaseCommand):
    help = "Deactivate accounts with no login for --days N (dry run unless --apply). Prints counts only."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, required=True, help="Inactive for more than N days.")
        parser.add_argument("--apply", action="store_true", help="Deactivate them (default: only count).")
        parser.add_argument("--include-token-holders", action="store_true",
                            help="Also deactivate inactive accounts that hold a DRF API token.")

    def handle(self, *args, **options):
        days = options["days"]
        if days < 1:
            raise CommandError("--days must be at least 1")
        candidates, skipped = find_inactive(days, include_token_holders=options["include_token_holders"])
        pks = list(candidates.values_list("pk", flat=True))
        self.stdout.write(
            "skipped: %d superusers, %d exempt service accounts, %d token holders"
            % (skipped["superusers"], skipped["exempt"], skipped["token_holders"])
        )
        if not options["apply"]:
            self.stdout.write("dry run: %d accounts inactive for more than %d days would be deactivated "
                              "(re-run with --apply)" % (len(pks), days))
            return
        User = get_user_model()
        changed = 0
        # save() per account (not a bulk update) so audit logging and signals see each change.
        for user in User.objects.filter(pk__in=pks, is_active=True):
            user.is_active = False
            user.save(update_fields=["is_active"])
            changed += 1
        self.stdout.write("deactivated %d accounts inactive for more than %d days" % (changed, days))

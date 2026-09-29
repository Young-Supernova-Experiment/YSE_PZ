"""Re-encrypt every EncryptedCredential from one key to another.

    python manage.py rotate_credentials_key --old OLD_KEY --new NEW_KEY [--dry-run]

``--old`` may be given more than once (or as a comma-separated list) when rows
were written under several keys. Run it with the *old* configuration still in
place, then set ``[secrets] credentials_key`` to NEW_KEY and restart. Rows that
cannot be decrypted with any old key are reported and left untouched; the
command exits non-zero in that case so the operator notices.
"""

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from YSE_App.models import EncryptedCredential
from YSE_App.services import credentials as cs


class Command(BaseCommand):
    help = "Re-encrypt stored credentials with a new CREDENTIALS_KEY."

    def add_arguments(self, parser):
        parser.add_argument("--old", action="append", required=True,
                            help="Current key (repeat or comma-separate for several).")
        parser.add_argument("--new", required=True, help="Key to encrypt with from now on.")
        parser.add_argument("--dry-run", action="store_true",
                            help="Check every row decrypts; write nothing.")

    def handle(self, *args, **options):
        old_keys = []
        for item in options["old"]:
            old_keys.extend(cs.split_keys(item))
        new_key = options["new"].strip()
        if not old_keys:
            raise CommandError("--old must name at least one key")
        try:
            cs._fernet_for(new_key)
            for k in old_keys:
                cs._fernet_for(k)
        except cs.CredentialKeyInvalid as exc:
            raise CommandError(str(exc))
        if new_key in old_keys:
            raise CommandError("--new must differ from every --old key")

        new_fp = cs.key_fingerprint(new_key)
        rotated, skipped, failed = 0, 0, []
        with transaction.atomic():
            for cred in EncryptedCredential.objects.select_for_update().order_by("pk"):
                if not cred.encrypted_payload:
                    skipped += 1
                    continue
                try:
                    token = cs.reencrypt(cred.encrypted_payload, old_keys, new_key)
                except cs.CredentialDecryptError:
                    failed.append(cred)
                    continue
                rotated += 1
                if options["dry_run"]:
                    continue
                EncryptedCredential.objects.filter(pk=cred.pk).update(
                    encrypted_payload=token, key_fingerprint=new_fp,
                )
            if failed and not options["dry_run"]:
                transaction.set_rollback(True)

        verb = "would re-encrypt" if options["dry_run"] else "re-encrypted"
        self.stdout.write("%s %d credential(s); %d without a secret skipped; %d undecryptable."
                          % (verb, rotated, skipped, len(failed)))
        for cred in failed:
            self.stdout.write("  cannot decrypt: %s (id %d, key fingerprint %s)"
                              % (cred, cred.pk, cred.key_fingerprint or "?"))
        if failed:
            raise CommandError("some credentials could not be decrypted with the --old key(s); "
                               "nothing was changed")
        if not options["dry_run"]:
            self.stdout.write("Now set [secrets] credentials_key (or YSE_CREDENTIALS_KEY) to the "
                              "new key and restart the web and cron processes.")

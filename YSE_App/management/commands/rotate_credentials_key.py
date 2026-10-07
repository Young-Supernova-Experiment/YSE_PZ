"""Re-encrypt every EncryptedCredential from one key to another.

    python manage.py rotate_credentials_key --old=OLD_KEY --new=NEW_KEY [--dry-run]

``--old`` may be given more than once (or as a comma-separated list) when rows
were written under several keys. Run it with the *old* configuration still in
place, then set ``[secrets] credentials_key`` to NEW_KEY and restart. Rows that
cannot be decrypted with any old key are reported and left untouched; the
command exits non-zero in that case so the operator notices.

Write the keys as ``--old=KEY`` / ``--new=KEY`` (with ``=``): a Fernet key is
URL-safe base64 and about one in 32 starts with ``-``, which argparse reads as
another option when the value is a separate word ("expected one argument",
#359). The keys can also come from the environment when the flags are
omitted: ``YSE_ROTATE_OLD_KEYS`` (comma-separated) and ``YSE_ROTATE_NEW_KEY``.
"""

import os

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from YSE_App.models import EncryptedCredential
from YSE_App.services import credentials as cs


OLD_KEYS_ENV = "YSE_ROTATE_OLD_KEYS"
NEW_KEY_ENV = "YSE_ROTATE_NEW_KEY"


class Command(BaseCommand):
    help = ("Re-encrypt stored credentials with a new CREDENTIALS_KEY. Write the keys as "
            "--old=KEY --new=KEY (a key may start with '-'), or export "
            "YSE_ROTATE_OLD_KEYS / YSE_ROTATE_NEW_KEY and omit the flags.")

    def add_arguments(self, parser):
        parser.add_argument("--old", action="append", metavar="KEY",
                            help="Current key, written --old=KEY (repeat or comma-separate for "
                                 "several); default: $YSE_ROTATE_OLD_KEYS.")
        parser.add_argument("--new", metavar="KEY",
                            help="Key to encrypt with from now on, written --new=KEY; "
                                 "default: $YSE_ROTATE_NEW_KEY.")
        parser.add_argument("--dry-run", action="store_true",
                            help="Check every row decrypts; write nothing.")

    def handle(self, *args, **options):
        old_keys = []
        for item in options["old"] or [os.environ.get(OLD_KEYS_ENV, "")]:
            old_keys.extend(cs.split_keys(item))
        new_key = (options["new"] or os.environ.get(NEW_KEY_ENV, "")).strip()
        if not old_keys:
            raise CommandError("--old=KEY (or %s) must name at least one key" % OLD_KEYS_ENV)
        if not new_key:
            raise CommandError("--new=KEY (or %s) is required" % NEW_KEY_ENV)
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

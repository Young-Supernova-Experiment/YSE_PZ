"""Generate the research key pair for the encrypted profile fields (#627).

Run this on the data custodian's OWN machine, never on the server:

    python manage.py generate_research_keypair --out-dir ~/yse-research-key [--passphrase]

Writes ``research_private_key.pem`` (mode 0600; with --passphrase it is
encrypted with a passphrase you type) and ``research_public_key.pem``, and
prints the public key's fingerprint. Install ONLY the public key on the server
([research] public_key_path, or env YSE_RESEARCH_PUBLIC_KEY). Keep the private
key offline: whoever holds it can read every encrypted profile value.
"""

import getpass
import os

from django.core.management.base import BaseCommand, CommandError

from YSE_App.services import research_crypto

PRIVATE_NAME = "research_private_key.pem"
PUBLIC_NAME = "research_public_key.pem"


class Command(BaseCommand):
    help = "Generate an RSA key pair for the encrypted researcher-profile fields (run off-server)."

    def add_arguments(self, parser):
        parser.add_argument("--out-dir", required=True, help="Directory to write the two PEM files into.")
        parser.add_argument("--bits", type=int, default=research_crypto.DEFAULT_KEY_BITS,
                            help="RSA key size (default %d)." % research_crypto.DEFAULT_KEY_BITS)
        parser.add_argument("--passphrase", action="store_true",
                            help="Encrypt the private key with a passphrase (prompted).")
        parser.add_argument("--force", action="store_true", help="Overwrite existing files.")

    def handle(self, *args, **options):
        out_dir = os.path.abspath(os.path.expanduser(options["out_dir"]))
        if options["bits"] < research_crypto.MIN_KEY_BITS:
            raise CommandError("--bits must be at least %d" % research_crypto.MIN_KEY_BITS)
        os.makedirs(out_dir, mode=0o700, exist_ok=True)
        private_path = os.path.join(out_dir, PRIVATE_NAME)
        public_path = os.path.join(out_dir, PUBLIC_NAME)
        for path in (private_path, public_path):
            if os.path.exists(path) and not options["force"]:
                raise CommandError("%s exists; use --force to overwrite" % path)
        passphrase = None
        if options["passphrase"]:
            first = getpass.getpass("Passphrase for the private key: ")
            if not first or first != getpass.getpass("Repeat the passphrase: "):
                raise CommandError("passphrases are empty or do not match")
            passphrase = first.encode("utf-8")
        private_pem, public_pem, fingerprint = research_crypto.generate_keypair(options["bits"], passphrase)
        fd = os.open(private_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(private_pem)
        with open(public_path, "wb") as fh:
            fh.write(public_pem)
        self.stdout.write("Private key (keep offline; never copy it to the server): %s" % private_path)
        self.stdout.write("Public key (install this one on the server): %s" % public_path)
        self.stdout.write("Fingerprint: %s" % fingerprint)

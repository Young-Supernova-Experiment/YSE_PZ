"""Print a new Fernet key for [secrets] credentials_key / YSE_CREDENTIALS_KEY.

    python manage.py generate_credentials_key

Paste the key into YSE_PZ/settings.ini under ``[secrets]`` as
``credentials_key: <key>`` (or export ``YSE_CREDENTIALS_KEY``) and restart the
web and cron processes. Changing an existing key requires
``manage.py rotate_credentials_key --old OLD --new NEW`` first, or every stored
credential becomes unreadable.
"""

from django.core.management.base import BaseCommand

from YSE_App.services.credentials import generate_key


class Command(BaseCommand):
    help = "Generate a Fernet key for encrypting stored credentials."

    def add_arguments(self, parser):
        parser.add_argument("--quiet", action="store_true", help="Print only the key.")

    def handle(self, *args, **options):
        key = generate_key()
        if options["quiet"]:
            self.stdout.write(key)
            return
        self.stdout.write(key)
        self.stdout.write("")
        self.stdout.write(
            "Add to YSE_PZ/settings.ini:\n\n[secrets]\ncredentials_key: %s\n\n"
            "or export YSE_CREDENTIALS_KEY=%s, then restart the web/cron processes.\n"
            "Rotating an existing key: manage.py rotate_credentials_key --old OLD --new NEW"
            % (key, key)
        )

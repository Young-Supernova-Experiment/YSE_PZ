"""Reset a user's two-factor sign-in (authenticator and backup codes, #512).

    python manage.py reset_staff_mfa <username>

Removes every django-otp device of that account. At the next sign-in the user
is asked to set up an authenticator again; a session verified with a removed
device has to sign in with a code again. Needs STAFF_MFA_REQUIRED on (django-otp
installed). The same reset is the "Reset two-factor sign-in" action in
admin > Users (superusers only).
"""

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from YSE_App import staff_mfa


class Command(BaseCommand):
    help = "Remove a user's two-factor devices so they set up a new authenticator at the next sign-in."

    def add_arguments(self, parser):
        parser.add_argument('username')

    def handle(self, *args, **options):
        if not getattr(settings, 'STAFF_MFA_REQUIRED', False):
            raise CommandError('Two-factor sign-in is off ([security] STAFF_MFA_REQUIRED); nothing to reset.')
        user_model = get_user_model()
        try:
            user = user_model.objects.get(**{user_model.USERNAME_FIELD: options['username']})
        except user_model.DoesNotExist:
            raise CommandError('No such user.')
        removed = staff_mfa.reset_devices(user)
        self.stdout.write('Removed %d two-factor device(s); the user sets up a new authenticator at the next sign-in.'
                          % removed)

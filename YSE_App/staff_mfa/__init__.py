"""Two-factor sign-in for staff and superusers (django-otp TOTP, #512).

Used only when ``STAFF_MFA_REQUIRED`` is on (YSE_PZ/settings.py), which also
adds django-otp to INSTALLED_APPS and the middleware. Nothing here imports
django-otp at module level, so this package can be imported without it.

* ``middleware.StaffMFAMiddleware``: a browser session of an ``is_staff`` or
  ``is_superuser`` user that has not entered a code is sent to the code page
  (or, without a device, to setup); see that module for what is exempt.
* ``views``: the code page, the authenticator setup page (QR code, otpauth://
  URI, confirmation code) and one-time backup codes.
* ``reset_devices(user)``: removes a user's devices (admin action on Users,
  ``manage.py reset_staff_mfa``); the user sets up again at the next sign-in.
"""

from django.conf import settings
from django.utils import timezone

BACKUP_CODE_COUNT = 10
TOTP_DEVICE_NAME = 'authenticator'
BACKUP_DEVICE_NAME = 'backup codes'
# Session key: setup was offered once during the grace period.
GRACE_NOTICE_SESSION_KEY = 'staff_mfa_grace_offered'


def mfa_enabled():
    return bool(getattr(settings, 'STAFF_MFA_REQUIRED', False))


def mfa_applies_to(user):
    """True for a signed-in staff or superuser account."""
    return bool(
        user is not None
        and getattr(user, 'is_authenticated', False)
        and (getattr(user, 'is_staff', False) or getattr(user, 'is_superuser', False))
    )


def in_grace_period():
    """True before STAFF_MFA_ENFORCE_AFTER (staff without a device may skip setup)."""
    after = getattr(settings, 'STAFF_MFA_ENFORCE_AFTER', None)
    return after is not None and timezone.localdate() < after


def is_verified(user):
    check = getattr(user, 'is_verified', None)
    return bool(check()) if callable(check) else False


def has_confirmed_totp(user):
    from django_otp.plugins.otp_totp.models import TOTPDevice

    return TOTPDevice.objects.devices_for_user(user, confirmed=True).exists()


def reset_devices(user):
    """Delete every django-otp device of ``user``; returns the number removed.

    Sessions verified with a removed device become unverified on their next
    request (django-otp looks the device up again each time).
    """
    from django_otp import devices_for_user

    removed = 0
    for device in list(devices_for_user(user, confirmed=None)):
        device.delete()
        removed += 1
    return removed

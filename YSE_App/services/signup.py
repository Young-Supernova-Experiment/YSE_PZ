"""Self-service signup helpers (#448): flag, spam guard and email verification.

Plain Django (``django.contrib.auth``), no third-party registration package.

* ``SELF_SIGNUP_ENABLED`` (default off): the signup and email-verification pages return
  404 while it is off.
* Accounts are active at once (Ryan, 2026-10-13). The onboarding gate then sends the new
  user through the User Agreement and the researcher profile before any other page.
* Spam guard, no external services: a honeypot field, a signed form timestamp (a minimum
  fill time), and hourly cache counters per client IP (only a hash of the IP is used as
  the key) and site-wide.
* Email verification (``SELF_SIGNUP_EMAIL_VERIFICATION``, default on when email delivery
  is on): the address is NOT stored on the account at signup. It travels only inside an
  encrypted, time-limited token in the emailed link; opening the link and confirming
  stores it. So an unconfirmed address is never used for notifications or password
  resets, and no extra column is needed. With verification off, the address is stored
  directly.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
from typing import Optional, Tuple

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.mail import send_mail
from django.urls import reverse
from django.utils import timezone

logger = logging.getLogger(__name__)

HOUR = 3600


def signup_enabled() -> bool:
    return bool(getattr(settings, "SELF_SIGNUP_ENABLED", False))


def verification_enabled() -> bool:
    return bool(getattr(settings, "SELF_SIGNUP_EMAIL_VERIFICATION", False))


def _int_setting(name: str, default: int) -> int:
    try:
        return int(getattr(settings, name, default))
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# throttle


def client_ip(request) -> str:
    header = (getattr(settings, "SELF_SIGNUP_CLIENT_IP_HEADER", "") or "").strip()
    if header:
        value = request.META.get(header, "")
        if value:
            # The last entry is the one the trusted proxy added.
            return value.split(",")[-1].strip()
    return request.META.get("REMOTE_ADDR", "") or "unknown"


def _bucket() -> str:
    return timezone.now().strftime("%Y%m%d%H")


def _hit(key: str) -> int:
    cache.add(key, 0, HOUR + 60)
    try:
        return cache.incr(key)
    except ValueError:  # evicted between add and incr
        cache.set(key, 1, HOUR + 60)
        return 1


def _ip_key(request) -> str:
    digest = hashlib.sha256(("yse-signup:%s" % client_ip(request)).encode("utf-8")).hexdigest()[:32]
    return "yse:signup:ip:%s:%s" % (digest, _bucket())


def register_attempt(request) -> bool:
    """Count one signup attempt from this client; False when over the hourly limit."""
    limit = _int_setting("SELF_SIGNUP_ATTEMPTS_PER_IP_PER_HOUR", 10)
    if limit <= 0:
        return True
    return _hit(_ip_key(request)) <= limit


def site_quota_available() -> bool:
    limit = _int_setting("SELF_SIGNUP_ACCOUNTS_PER_HOUR", 30)
    if limit <= 0:
        return True
    return (cache.get("yse:signup:site:%s" % _bucket()) or 0) < limit


def register_account_created() -> None:
    _hit("yse:signup:site:%s" % _bucket())


def register_email_send(user: User) -> bool:
    """Count one verification email for ``user``; False when over 5 an hour."""
    return _hit("yse:signup:mail:%s:%s" % (user.pk, _bucket())) <= 5


def min_fill_seconds() -> int:
    return max(0, _int_setting("SELF_SIGNUP_MIN_FILL_SECONDS", 3))


# ---------------------------------------------------------------------------
# email verification tokens


def _fernet() -> Fernet:
    digest = hashlib.sha256(("yse-pz email verification:%s" % settings.SECRET_KEY).encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def _email_fingerprint(email: str) -> str:
    return hashlib.sha256((email or "").strip().lower().encode("utf-8")).hexdigest()[:16]


def make_token(user: User, email: str) -> str:
    """An encrypted token holding the user id, the new address and the current address' fingerprint.

    The fingerprint makes a link single-use: once the address changes, older links stop working.
    """
    payload = json.dumps({"u": user.pk, "e": email, "c": _email_fingerprint(user.email)}).encode("utf-8")
    return _fernet().encrypt(payload).decode("ascii")


def read_token(token: str) -> Optional[Tuple[User, str]]:
    """``(user, email)`` for a valid, unexpired token, else None."""
    ttl = max(1, _int_setting("SELF_SIGNUP_VERIFY_HOURS", 72)) * HOUR
    try:
        data = json.loads(_fernet().decrypt((token or "").encode("ascii"), ttl=ttl).decode("utf-8"))
    except (InvalidToken, ValueError, TypeError, UnicodeError):
        return None
    if not isinstance(data, dict):
        return None
    user = User.objects.filter(pk=data.get("u"), is_active=True).first()
    email = data.get("e") or ""
    if user is None or not email or data.get("c") != _email_fingerprint(user.email):
        return None
    return user, email


def email_in_use(email: str, exclude_user: Optional[User] = None) -> bool:
    qs = User.objects.filter(email__iexact=(email or "").strip(), is_active=True)
    if exclude_user is not None:
        qs = qs.exclude(pk=exclude_user.pk)
    return qs.exists()


def confirm_email(user: User, email: str) -> bool:
    """Store the confirmed address; False when another active account already uses it."""
    if email_in_use(email, exclude_user=user):
        return False
    user.email = email
    user.save(update_fields=["email"])
    return True


def send_verification_email(request, user: User, email: str) -> bool:
    """Email a confirmation link to ``email``. Returns False if sending failed."""
    link = request.build_absolute_uri(reverse("signup_verify_email", args=[make_token(user, email)]))
    hours = max(1, _int_setting("SELF_SIGNUP_VERIFY_HOURS", 72))
    prefix = getattr(settings, "NOTIFICATION_EMAIL_SUBJECT_PREFIX", "") or ""
    body = (
        "Someone (hopefully you) asked to use this email address for the YSE-PZ account \"%s\".\n\n"
        "To confirm the address, open this link within %d hours and press Confirm:\n\n%s\n\n"
        "If this was not you, ignore this message; the address will not be added to the account.\n"
        % (user.get_username(), hours, link)
    )
    try:
        send_mail("%sConfirm your email address" % prefix, body, None, [email], fail_silently=False)
    except Exception:  # noqa: BLE001 - signup never fails because the mail server is down
        logger.exception("signup verification email failed")
        return False
    return True

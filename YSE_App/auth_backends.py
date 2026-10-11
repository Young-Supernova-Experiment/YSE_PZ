"""Failed-login lockout backend (django-axes), used only when LOGIN_LOCKOUT_ENABLED is on.

Imported by Django only through AUTHENTICATION_BACKENDS, which names this
module only when django-axes is installed and the flag is on (YSE_PZ/settings.py).
"""

from axes.backends import AxesStandaloneBackend


class LoginLockoutBackend(AxesStandaloneBackend):
    """Axes' lockout check, skipped for authenticate() calls made without a request.

    Several views re-run authenticate() without a request after
    ``login_or_basic_auth_required`` has already checked (and counted) the same
    credentials; axes' own backend would raise for those calls. Every entry
    point that takes credentials from a client passes the request: the login
    view, the admin login, DRF Basic auth and the Basic-auth decorator.
    """

    def authenticate(self, request=None, username=None, password=None, **kwargs):
        if request is None:
            return None
        return super().authenticate(request, username=username, password=password, **kwargs)


def client_ip_address(request):
    """The connecting address (REMOTE_ADDR); forwarding headers are not trusted here."""
    inner = getattr(request, '_request', request)  # DRF Request wraps the HttpRequest
    return (getattr(inner, 'META', {}) or {}).get('REMOTE_ADDR', '') or ''

"""Two-factor pages for staff: enter a code, set up an authenticator, backup codes (#512).

Routed only when STAFF_MFA_REQUIRED is on (YSE_App/staff_mfa/urls.py). Every
page needs a signed-in session (LoginRequiredMiddleware); users the requirement
does not apply to are sent on to the next page.
"""

import re
from base64 import b32encode

from django.conf import settings
from django.contrib.auth.views import redirect_to_login
from django.db import transaction
from django.http import HttpResponseRedirect
from django.shortcuts import render, resolve_url
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods, require_POST

from YSE_App import staff_mfa

_TOTP_CODE = re.compile(r'^\d{6}$')


def _next_url(request):
    candidate = request.POST.get('next') or request.GET.get('next')
    if candidate and url_has_allowed_host_and_scheme(
        url=candidate, allowed_hosts={request.get_host()}, require_https=request.is_secure(),
    ):
        return candidate
    return resolve_url('dashboard')


def _clean_code(raw):
    return re.sub(r'[\s-]', '', raw or '').lower()[:32]


def _mark_verified(request, device):
    from django_otp import login as otp_login

    otp_login(request, device)
    request.session.pop(staff_mfa.GRACE_NOTICE_SESSION_KEY, None)
    # New session key once the second factor is in.
    request.session.cycle_key()


def _check_code(user, code):
    """(device, throttled): the confirmed device that accepts ``code``, if any.

    Six digits are checked against the authenticator, anything else against
    the backup codes; each device's own throttle applies (django-otp).
    """
    from django_otp.plugins.otp_static.models import StaticDevice
    from django_otp.plugins.otp_totp.models import TOTPDevice

    if not code:
        return None, False
    model = TOTPDevice if _TOTP_CODE.match(code) else StaticDevice
    throttled = False
    with transaction.atomic():
        for device in model.objects.devices_for_user(user, confirmed=True).select_for_update():
            allowed, _info = device.verify_is_allowed()
            if not allowed:
                throttled = True
                continue
            if device.verify_token(code):
                return device, False
    return None, throttled


def _new_backup_codes(user):
    """Replace the user's backup codes with BACKUP_CODE_COUNT new single-use codes."""
    from django_otp.plugins.otp_static.models import StaticDevice, StaticToken

    StaticDevice.objects.filter(user=user).delete()
    device = StaticDevice.objects.create(user=user, name=staff_mfa.BACKUP_DEVICE_NAME, confirmed=True)
    codes = []
    while len(codes) < staff_mfa.BACKUP_CODE_COUNT:
        code = StaticToken.random_token()
        if code not in codes:
            codes.append(code)
    StaticToken.objects.bulk_create([StaticToken(device=device, token=c) for c in codes])
    return device, codes


def _backup_codes_response(request, codes, next_url):
    return render(request, 'YSE_App/staff_mfa/backup_codes.html', {'codes': codes, 'next': next_url})


@never_cache
@require_http_methods(['GET', 'POST'])
def verify(request):
    user = request.user
    next_url = _next_url(request)
    if not staff_mfa.mfa_applies_to(user) or staff_mfa.is_verified(user):
        return HttpResponseRedirect(next_url)
    if not staff_mfa.has_confirmed_totp(user):
        return redirect_to_login(next_url, resolve_url('staff_mfa_setup'), 'next')

    error = None
    if request.method == 'POST':
        device, throttled = _check_code(user, _clean_code(request.POST.get('token')))
        if device is not None:
            _mark_verified(request, device)
            return HttpResponseRedirect(next_url)
        if throttled:
            error = 'Too many attempts. Wait a minute, then try again.'
        else:
            error = 'That code was not accepted. Try again.'
    return render(request, 'YSE_App/staff_mfa/verify.html', {'next': next_url, 'error': error})


@never_cache
@require_http_methods(['GET', 'POST'])
def setup(request):
    from django_otp.plugins.otp_totp.models import TOTPDevice

    user = request.user
    next_url = _next_url(request)
    if not staff_mfa.mfa_applies_to(user):
        return HttpResponseRedirect(next_url)
    has_device = staff_mfa.has_confirmed_totp(user)
    if has_device and not staff_mfa.is_verified(user):
        # Replacing an authenticator needs the current one (or a backup code) first.
        return redirect_to_login(next_url, resolve_url('staff_mfa_verify'), 'next')

    pending = (TOTPDevice.objects.filter(user=user, confirmed=False).order_by('id').first()
               or TOTPDevice.objects.create(user=user, name=staff_mfa.TOTP_DEVICE_NAME, confirmed=False))

    error = None
    if request.method == 'POST':
        code = _clean_code(request.POST.get('token'))
        confirmed = None
        if _TOTP_CODE.match(code):
            with transaction.atomic():
                device = TOTPDevice.objects.select_for_update().get(pk=pending.pk)
                if device.verify_is_allowed()[0] and device.verify_token(code):
                    device.confirmed = True
                    device.save(update_fields=['confirmed'])
                    TOTPDevice.objects.filter(user=user).exclude(pk=device.pk).delete()
                    confirmed = device
        if confirmed is not None:
            _device, codes = _new_backup_codes(user)
            _mark_verified(request, confirmed)
            return _backup_codes_response(request, codes, next_url)
        error = 'That code was not accepted. Check the time on your phone and enter the current code.'
        pending.refresh_from_db()

    can_skip = (not has_device) and staff_mfa.in_grace_period()
    if can_skip:
        request.session[staff_mfa.GRACE_NOTICE_SESSION_KEY] = True

    import segno

    uri = pending.config_url
    secret = b32encode(pending.bin_key).decode('ascii')
    context = {
        'next': next_url,
        'error': error,
        'qr_svg': segno.make(uri, error='m').svg_inline(scale=5, dark='#000', light='#fff', border=4),
        'otpauth_uri': uri,
        'secret': ' '.join(secret[i:i + 4] for i in range(0, len(secret), 4)),
        'replacing': has_device,
        'can_skip': can_skip,
        'enforce_after': getattr(settings, 'STAFF_MFA_ENFORCE_AFTER', None),
    }
    return render(request, 'YSE_App/staff_mfa/setup.html', context)


@never_cache
@require_POST
def backup_codes(request):
    """Issue a new set of backup codes (the old ones stop working)."""
    from django_otp.plugins.otp_static.models import StaticDevice

    user = request.user
    next_url = _next_url(request)
    if not staff_mfa.mfa_applies_to(user) or not staff_mfa.is_verified(user):
        return HttpResponseRedirect(next_url)
    signed_in_with_backup_code = isinstance(getattr(user, 'otp_device', None), StaticDevice)
    device, codes = _new_backup_codes(user)
    if signed_in_with_backup_code:
        # The session's device was just replaced; keep the session verified.
        from django_otp import login as otp_login

        otp_login(request, device)
    return _backup_codes_response(request, codes, next_url)

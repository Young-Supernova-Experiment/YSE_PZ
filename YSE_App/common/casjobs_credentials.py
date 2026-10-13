"""CasJobs (MAST PS1) login for the ingest jobs that look up a PS1 star/galaxy score.

The WSID and password come from the environment (CASJOBS_WSID, CASJOBS_PW) or from the
``[casjobs]`` section of settings.ini (``wsid`` and ``password``). Nothing is read at import
time, so a missing value stops only the job that needs it, never the web app.
"""
import os

from django.conf import settings as djangoSettings


class CasJobsCredentialsError(RuntimeError):
    """The CasJobs WSID or password is not configured."""


def _clean(value):
    value = (value or '').strip()
    # public_settings.ini placeholders look like <...>
    return '' if value.startswith('<') else value


def get_casjobs_credentials():
    """Return (wsid, password); raise CasJobsCredentialsError naming the missing setting(s)."""
    wsid = _clean(os.environ.get('CASJOBS_WSID')) or _clean(getattr(djangoSettings, 'CASJOBS_WSID', ''))
    password = _clean(os.environ.get('CASJOBS_PW')) or _clean(getattr(djangoSettings, 'CASJOBS_PW', ''))
    missing = [name for name, val in (('wsid', wsid), ('password', password)) if not val]
    if missing:
        raise CasJobsCredentialsError(
            'CasJobs credentials are not configured (missing: %s). Set [casjobs] wsid and password '
            'in settings.ini, or the CASJOBS_WSID and CASJOBS_PW environment variables.'
            % ', '.join(missing))
    return wsid, password


def configure_casjobs_env():
    """Export the credentials for mastrequests.MastCasJobs, which reads them from the environment."""
    wsid, password = get_casjobs_credentials()
    os.environ['CASJOBS_WSID'] = wsid
    os.environ['CASJOBS_PW'] = password

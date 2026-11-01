"""Content-Security-Policy-Report-Only header, behind CSP_REPORT_ONLY_ENABLED (default off).

Report-only: browsers report what the policy would block and block nothing.
The default policy allows the CDNs the templates load scripts, styles and fonts
from; images, XHR and frames may come from any https origin (cutouts, finder
charts, Aladin, survey archives). Set CSP_REPORT_ONLY_POLICY to replace it.
"""

from django.conf import settings

# Hosts the templates load scripts, styles or fonts from (YSE_App/templates).
CDN_HOSTS = (
    'https://ajax.googleapis.com',
    'https://aladin.u-strasbg.fr',
    'https://cdn.jsdelivr.net',
    'https://cdn.rawgit.com',
    'https://cdnjs.cloudflare.com',
    'https://code.jquery.com',
    'https://momentjs.com',
    'https://mpld3.github.io',
    'https://oss.maxcdn.com',
    'https://stackpath.bootstrapcdn.com',
    'https://use.fontawesome.com',
)
FONT_HOSTS = ('https://fonts.googleapis.com', 'https://fonts.gstatic.com')


def default_policy():
    cdns = ' '.join(CDN_HOSTS)
    fonts = ' '.join(FONT_HOSTS)
    return '; '.join((
        "default-src 'self'",
        f"script-src 'self' 'unsafe-inline' 'unsafe-eval' {cdns}",
        f"style-src 'self' 'unsafe-inline' {cdns} {fonts}",
        f"font-src 'self' data: {cdns} {fonts}",
        "img-src 'self' data: blob: https:",
        "connect-src 'self' https:",
        "frame-src 'self' https:",
        "object-src 'none'",
        "base-uri 'self'",
        "form-action 'self'",
        "frame-ancestors 'self'",
    ))


def report_only_policy():
    policy = (getattr(settings, 'CSP_REPORT_ONLY_POLICY', '') or '').strip() or default_policy()
    report_uri = (getattr(settings, 'CSP_REPORT_URI', '') or '').strip()
    if report_uri and 'report-uri' not in policy:
        policy = f'{policy.rstrip("; ")}; report-uri {report_uri}'
    return policy


class ContentSecurityPolicyReportOnlyMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if getattr(settings, 'CSP_REPORT_ONLY_ENABLED', False) and \
                'Content-Security-Policy-Report-Only' not in response:
            response['Content-Security-Policy-Report-Only'] = report_only_policy()
        return response

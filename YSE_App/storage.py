"""Static-file storage with content-hashed names (#482).

Enabled only when YSE_HASHED_STATIC=1 (see YSE_PZ/settings.py and the
"Performance audit 2026-10-10: server settings" section of docs/ziggy-runbook.md).
"""
from django.contrib.staticfiles.storage import ManifestStaticFilesStorage


class LenientManifestStaticFilesStorage(ManifestStaticFilesStorage):
    """Hashed names, but a file missing from the manifest falls back to its
    plain name instead of raising ValueError.  Pages therefore keep rendering
    if collectstatic was not re-run, or if a template references a path that
    is not collected (the vendored bower/plugin trees are not all hash-safe).
    """
    manifest_strict = False

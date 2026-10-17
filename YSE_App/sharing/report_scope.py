"""Which photometry sets and spectra a TNS / Hermes report may include (#691).

Sending data to an external service is a sharing action.

* Manual report (a submitter): public data, plus restricted data the submitter
  may see (``services.visibility``) *and* may share (a PI, deputy or staff for
  that dataset, ``services.data_sharing.share_role``).
* Automatic publishing (no user): public data, plus restricted data whose PI
  opted in (:class:`~YSE_App.models.AutopublishOptIn`).
* Anything else (no scope given): public data only.

Datasets are decided whole (a photometry set or a spectrum); public means no
collaboration group other than Public (``data_sharing.is_public_dataset``).
"""

from __future__ import annotations

from typing import Dict, Set

from YSE_App.models import AutopublishOptIn, TransientPhotometry, TransientSpectrum
from YSE_App.services import data_sharing
from YSE_App.services.visibility import photometry_visible_q, spectrum_visible_q, user_sees_everything

PUBLIC_ONLY = "public"
USER = "user"
AUTOPUBLISH = "autopublish"


class ReportScope:
    def __init__(self, mode: str = PUBLIC_ONLY, user=None):
        self.mode = mode
        self.user = user

    @classmethod
    def public_only(cls) -> "ReportScope":
        return cls(PUBLIC_ONLY)

    @classmethod
    def autopublish(cls) -> "ReportScope":
        return cls(AUTOPUBLISH)

    @classmethod
    def for_user(cls, user) -> "ReportScope":
        if user is None or not getattr(user, "is_authenticated", False):
            return cls(PUBLIC_ONLY)
        return cls(USER, user)

    @classmethod
    def for_submission(cls, submission) -> "ReportScope":
        """The scope a stored submission was made under (rule-created: autopublish, else its creator)."""
        if submission.auto_publisher_id:
            return cls.autopublish()
        return cls.for_user(submission.created_by)

    def _decide(self, model, transient, visible_q_fn, optin_field):
        """``(allowed pks, count of visible datasets left out)`` for one dataset model on ``transient``."""
        rows = list(model.objects.filter(transient=transient).prefetch_related("groups"))
        public = {r.pk for r in rows if data_sharing.is_public_dataset(r)}
        restricted = [r for r in rows if r.pk not in public]
        if self.mode == PUBLIC_ONLY or not restricted:
            return public, 0
        if self.mode == AUTOPUBLISH:
            optin = set(AutopublishOptIn.objects.filter(
                **{optin_field + "__in": [r.pk for r in restricted]}).values_list(optin_field + "_id", flat=True))
            return public | optin, 0
        user = self.user
        if user_sees_everything(user):
            visible = {r.pk for r in restricted}
        else:
            visible = set(model.objects.filter(transient=transient).filter(visible_q_fn(user))
                          .values_list("pk", flat=True))
        pi_ids = data_sharing.own_pi_ids(user)
        designations = data_sharing.designations_of(user)
        allowed = set(public)
        left_out = 0
        for row in restricted:
            if row.pk not in visible:
                continue  # not the submitter's to see: not counted either
            if data_sharing.share_role(user, row, pi_ids=pi_ids, designations=designations) is not None:
                allowed.add(row.pk)
            else:
                left_out += 1
        return allowed, left_out

    def photometry_ids(self, transient) -> Set[int]:
        return self._decide(TransientPhotometry, transient, photometry_visible_q, "photometry")[0]

    def spectrum_ids(self, transient) -> Set[int]:
        return self._decide(TransientSpectrum, transient, spectrum_visible_q, "spectrum")[0]

    def omitted(self, transient) -> Dict[str, int]:
        """Counts of datasets the submitter can see but may not send (no contents)."""
        return {
            "photometry_sets": self._decide(TransientPhotometry, transient, photometry_visible_q, "photometry")[1],
            "spectra": self._decide(TransientSpectrum, transient, spectrum_visible_q, "spectrum")[1],
        }


OMITTED_REASON = ("Restricted data can be sent to an external service only by the dataset's PI or a deputy "
                  "the PI designated (or an administrator).")

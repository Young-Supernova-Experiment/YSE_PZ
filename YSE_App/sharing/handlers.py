"""Import this module to register every sharing job handler (#326, #327).

``YSE_App.signals`` imports it so web and worker processes register
``sharing.submit``, ``sharing.poll``, ``sharing.tns_retrieval`` and
``sharing.autopublish_sweep``; it is also listed in the job runner's default
handler modules through ``JOB_HANDLER_MODULES``.
"""

from YSE_App.sharing import autopublish, retrieval, tns  # noqa: F401

__all__ = ["tns", "retrieval", "autopublish"]

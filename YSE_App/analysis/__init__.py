"""Analysis services (#312): in-process runners and the registry glue (#313).

An in-process runner is a module exposing ``run(payload, params) -> AnalysisResult``
(see :mod:`YSE_App.analysis.base`). The two shipped here are the SALT3 /
sncosmo light-curve fit (:mod:`YSE_App.analysis.sncosmo_fit`, #315) and the
Bazin fit (:mod:`YSE_App.analysis.bazin_fit`, #225). ``BUILTIN_RUNNERS`` maps
the short names accepted by ``AnalysisService.runner_path`` to their modules;
any other value is imported as a dotted path.
"""

BUILTIN_RUNNERS = {
    "sncosmo_fit": "YSE_App.analysis.sncosmo_fit",
    "bazin_fit": "YSE_App.analysis.bazin_fit",
}

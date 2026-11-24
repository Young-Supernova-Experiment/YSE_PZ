"""sncosmo bandpasses that sncosmo does not ship (or ships only in newer versions).

``bandpassdict`` maps YSE bands to sncosmo bandpass names. Most are sncosmo
built-ins (``desg``, ``sdssr``, ...; sncosmo downloads those on first use).
The curves in ``bandpasses/`` come from the SVO Filter Profile Service and are
registered here:

* GOTO L / R / G / B (``GOTO/GOTO.<F>``: filter, optics, detector and a zenith
  atmosphere) as ``gotol``, ``gotor``, ``gotog`` and ``gotob``;
* ATLAS cyan / orange (``Misc/Atlas.<f>``, the curves sncosmo 2.8 ships) as
  ``atlasc`` and ``atlaso``, only when the installed sncosmo lacks them.

Call :func:`register` after ``import sncosmo`` and before fitting; it is
idempotent and reads each curve only when sncosmo first asks for it.
"""

from __future__ import annotations

import os

BANDPASS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bandpasses")

#: sncosmo name -> file in BANDPASS_DIR (wavelength in Angstrom, transmission)
LOCAL_BANDPASSES = {
    "gotol": "goto_l.dat",
    "gotor": "goto_r.dat",
    "gotog": "goto_g.dat",
    "gotob": "goto_b.dat",
}
#: registered only when sncosmo has no bandpass of that name (sncosmo < 2.8)
FALLBACK_BANDPASSES = {
    "atlasc": "atlas_c.dat",
    "atlaso": "atlas_o.dat",
}

_registered = False


def _load(path, name=None):
    import numpy as np
    import sncosmo

    wave, trans = np.loadtxt(path, comments="#", unpack=True)
    return sncosmo.Bandpass(wave, trans, name=name)


def register() -> None:
    global _registered
    if _registered:
        return
    import sncosmo

    for name, fname in LOCAL_BANDPASSES.items():
        sncosmo.register_loader(sncosmo.Bandpass, name, _load, args=(os.path.join(BANDPASS_DIR, fname),),
                                meta={"filterset": "goto", "description": "GOTO from the SVO Filter Profile Service"},
                                force=True)
    for name, fname in FALLBACK_BANDPASSES.items():
        try:
            sncosmo.register_loader(sncosmo.Bandpass, name, _load, args=(os.path.join(BANDPASS_DIR, fname),),
                                    meta={"filterset": "atlas", "description": "ATLAS from the SVO Filter Profile Service"})
        except Exception:  # sncosmo >= 2.8 already has it
            pass
    _registered = True

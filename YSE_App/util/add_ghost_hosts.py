#!/usr/bin/env python
# D. Jones - 9/13/21
# look for objects w/o host galaxies, run GHOST and upload the results

from astro_ghost.ghostHelperFunctions import *
from astro_ghost.photoz_helper import calc_photoz
import numpy as np
from YSE_App.models import *
from astropy.coordinates import SkyCoord
import astropy.units as u
from datetime import datetime
import glob
import logging
import os
import shutil

logger = logging.getLogger(__name__)


def _remove_dated_ghost_dirs():
    """Remove the transients_<YYYYMMDD>* scratch trees GHOST writes in the cwd."""
    stamp = datetime.utcnow().isoformat().split('T')[0].replace('-', '')
    for path in glob.glob('transients_%s*' % stamp):
        try:
            if os.path.isdir(path) and not os.path.islink(path):
                shutil.rmtree(path)
            else:
                os.remove(path)
        except OSError as exc:
            logger.warning("could not remove %s: %s", path, exc)

def getGHOSTData(ghost_host):
    hostdict = {}; hostcoords = ''
    hostdict = {'name':ghost_host['objName'].to_numpy()[0],'ra':ghost_host['raMean'].to_numpy()[0],
                'dec':ghost_host['decMean'].to_numpy()[0]}

    hostcoords = f"ra={hostdict['ra']:.7f}, dec={hostdict['dec']:.7f}\n"
    if 'photo_z' in ghost_host.keys():
        hostdict['photo_z_internal'] = ghost_host['photo_z'].to_numpy()[0]
    if 'NED_redshift' in ghost_host.keys() and ghost_host['NED_redshift'].to_numpy()[0] == ghost_host['NED_redshift'].to_numpy()[0]:
        hostdict['redshift'] = ghost_host['NED_redshift'].to_numpy()[0]

    if 'photo_z_internal' in hostdict.keys():
        if hostdict['photo_z_internal'] != hostdict['photo_z_internal']:
            hostdict['photo_z_internal'] = None
        
    return hostdict,hostcoords


def main(transients=None):
    if transients is None:
        transients = Transient.objects.filter(tags__name='YSE').filter(name__startswith='20') #.filter(Q(host__isnull=True) | Q(host__redshift__isnull=True))
    for t in transients:
        print(t)
        sc = [SkyCoord(t.ra,t.dec,unit=u.deg)]
        ghost_hosts = getTransientHosts([t.name], sc, verbose=True, starcut='gentle', ascentMatch=False)
        if not ghost_hosts.empty:
            ghost_hosts = calc_photoz(ghost_hosts)
            ghost_host = ghost_hosts[ghost_hosts['TransientName'] == t.name]
            if not len(ghost_host): ghost_host = None
            else:
                hostdict,hostcoords = getGHOSTData(ghost_host)
                hostdict['created_by'] = User.objects.get(username='djones')
                hostdict['modified_by'] = User.objects.get(username='djones')
                host = Host.objects.create(**hostdict)
                host.save()
                t.host = host
                t.save()

        _remove_dated_ghost_dirs()

    return

    

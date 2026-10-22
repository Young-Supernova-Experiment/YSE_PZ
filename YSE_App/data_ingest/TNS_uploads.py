import requests
from YSE_App.common.casjobs_credentials import configure_casjobs_env
import time
import imaplib
import socket
import ssl
import getpass
import pprint
import email
import re
import urllib.request
from bs4 import BeautifulSoup
from datetime import datetime
from enum import Enum
import astropy.units as u
from astropy import coordinates
import numpy as np
from astroquery.irsa_dust import IrsaDust
from datetime import timedelta
import json
import logging
import os
import tempfile
from astropy.coordinates import SkyCoord, Angle
from astropy.coordinates import ICRS, Galactic, FK4, FK5
from astropy.time import Time
import coreapi
from urllib.parse import quote,unquote
from requests.auth import HTTPBasicAuth
from YSE_App.common.ingest_http import default_timeout, ingest_session, upload_timeout
from YSE_App.common.ingest_http import http as ingest_http  # timeouts + retries (#473)
import struct
import threading
import queue
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import smtplib
from collections import OrderedDict
from YSE_App.util.TNS_Synopsis import mastrequests
from astropy.io import ascii
from itertools import islice
from astropy.cosmology import FlatLambdaCDM
import sys
from YSE_App.common import mast_query,chandra_query,spitzer_query
from YSE_App.common.data_references import (
    TNS_PHOTOMETRY_REPORT_KEYS,
    TNS_SPECTRUM_REPORT_KEYS,
    tns_reference_with_reports,
    tns_report_id,
)
from YSE_App.common.collaboration_groups import (
    PUBLIC_COLLABORATION_GROUP_NAME,
    TNS_IMPORT_COLLABORATION_GROUPS,
)

logger = logging.getLogger(__name__)


def _remove_quietly(*paths):
    """Delete scratch files; a missing file is fine, anything else is logged."""
    for path in paths:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.warning("could not remove %s: %s", path, exc)


from YSE_App.data_ingest.tns_api_client import tns_marker_user_agent
from django_cron import CronJobBase, Schedule
from django.conf import settings as djangoSettings
import argparse, configparser
import signal

from shutil import rmtree
import pandas as pd
from scipy.stats import gamma, halfnorm, uniform

try:
    from astro_prost.helpers import SnRateAbsmag
    from astro_prost.associate import associate_sample
    HAS_ASTRO_PROST = True
except ImportError:
    SnRateAbsmag = None
    associate_sample = None
    HAS_ASTRO_PROST = False

from tendo import singleton

### new antares search for ZTF matches
try:
    from antares_client.search import cone_search
    HAS_ANTARES = True
except ImportError:
    cone_search = None
    HAS_ANTARES = False

reg_obj = "https://www.wis-tns.org/object/(\w+)"
reg_ra = "\>\sRA[\=\*a-zA-Z\<\>\" ]+(\d{2}:\d{2}:\d{2}\.\d+)"
reg_dec = "DEC[\=\*a-zA-Z\<\>\" ]+((?:\+|\-)\d{2}:\d{2}:\d{2}\.\d+)\<\/em\>\,"

try:
    from dustmaps.sfd import SFDQuery
    sfd = SFDQuery()
except:
    pass

def get_ps_score(RA, DEC):
    '''Get ps1 star/galaxy score from MAST. Provide RA and DEC in degrees.
    Returns an empty string if no match is found witing 3 arcsec.
    '''
    # get the WSID and password if not already defined
    # get your WSID by going to https://mastweb.stsci.edu/ps1casjobs/changedetails.aspx after you login to Casjobs.

    configure_casjobs_env()  # credentials from settings.ini [casjobs] / env; raises if missing
    query = """select top 1 p.ps_score
    from pointsource_magnitudes_view as p
    inner join fGetNearbyObjEq(%.5f, %.5f, 0.05) nb on p.objid=nb.objid
    """ %(RA, DEC)

    jobs = mastrequests.MastCasJobs(context="HLSP_PS1_PSC")
    results = jobs.quick(query, task_name="python cross-match")

    output = results.split('\n')[1]
    if not output:
        output = None
    else:
        output = round(float(output), 3)
        print('PS_SCORE: %.3f' %output)

    return output
    
# Prost scratch handling and the matching itself live in YSE_App.services.prost_hosts (#439);
# these names stay importable from here.
from YSE_App.services.prost_hosts import (  # noqa: E402,F401
    PROST_RUN_PREFIX,
    PROST_STALE_SECONDS,
    make_prost_run_dir,
)
from YSE_App.services import prost_hosts as _prost_hosts  # noqa: E402


class processTNS:
    
    def __init__(self):
        self.verbose = None
        
    def add_options(self, parser=None, usage=None, config=None):

        if parser == None:
            parser = argparse.ArgumentParser(usage=usage, conflict_handler="resolve")

        # The basics
        parser.add_argument('-v', '--verbose', action="count", dest="verbose",default=1)
        parser.add_argument('--clobber', default=False, action="store_true",
                            help='clobber output file')
        parser.add_argument('-s','--settingsfile', default=None, type=str,
                            help='settings file (login/password info)')
        parser.add_argument('--status', default='New', type=str,
                            help='transient status to enter in YS_PZ')
        parser.add_argument('--noupdatestatus', default=False, action="store_true",
                            help='if set, don\'t promote ignore -> new when uploading')
        parser.add_argument('--update', default=False, action="store_true",
                            help='if set, scrape TNS pages to update events already in YSE_PZ')
        parser.add_argument('--updatefromZTF', default=False, action="store_true",
                            help='if set, update photometry from ZTF')
        parser.add_argument('--redohost', default=False, action="store_true",
                            help='if set, repeat search to find host matches (slow-ish)')
        parser.add_argument('--ndays', default=None, type=int,
                            help='number of days before today update events')

        if config:
            parser.add_argument('--login', default=config.get('main','login'), type=str,
                                help='gmail login (default=%default)')
            parser.add_argument('--password', default=config.get('main','password'), type=str,
                                help='gmail password (default=%default)')
            parser.add_argument('--dblogin', default=config.get('main','dblogin'), type=str,
                                help='database login, if post=True (default=%default)')
            parser.add_argument('--dbemail', default=config.get('main','dbemail'), type=str,
                                help='database login, if post=True (default=%default)')
            parser.add_argument('--dbpassword', default=config.get('main','dbpassword'), type=str,
                                help='database password, if post=True (default=%default)')
            parser.add_argument('--dbemailpassword', default=config.get('main','dbemailpassword'), type=str,
                                help='database password, if post=True (default=%default)')
            parser.add_argument('--dburl', default=config.get('main','dburl'), type=str,
                                help='URL to POST transients to a database (default=%default)')
            parser.add_argument('--tnsapi', default=config.get('main','tnsapi'), type=str,
                                help='TNS API URL (default=%default)')
            parser.add_argument('--tnsapikey', default=config.get('main','tnsapikey'), type=str,
                                help='TNS API key (default=%default)')
            parser.add_argument('--tns_bot_id', default=config.get('main','tns_bot_id'), type=str,
                                help='TNS API key (default=%default)')
            parser.add_argument('--tns_bot_name', default=config.get('main','tns_bot_name'), type=str,
                                help='TNS API key (default=%default)')

            parser.add_argument('--tns_recent_ndays', default=config.get('main','tns_recent_ndays'), type=str,
                                help='time interval for grabbing recent TNS events (default=%default)')
            parser.add_argument('--tns_fastupdates_nminutes', default=config.get('main','tns_fastupdates_nminutes'), type=str,
                                help='time interval for grabbing very recent TNS events (default=%default)')
            parser.add_argument('--hostmatchrad', default=config.get('main','hostmatchrad'), type=float,
                                help='matching radius for hosts (arcmin) (default=%default)')
            parser.add_argument('--ghost_path', default=config.get('main','ghost_path'), type=str,
                                help='Prost scratch directory; legacy name, used when prost_path is unset (default=%default)')
            parser.add_argument('--prost_path', default=config.get('main','prost_path',fallback=''), type=str,
                                help='Prost scratch base directory; falls back to ghost_path (default=%default)')

            parser.add_argument('--ztfurl', default=config.get('ztf','ztfurl'), type=str,
                                help='ZTF URL (default=%default)')

            parser.add_argument('--SMTP_LOGIN', default=config.get('SMTP_provider','SMTP_LOGIN'), type=str,
                                help='SMTP login (default=%default)')
            parser.add_argument('--SMTP_HOST', default=config.get('SMTP_provider','SMTP_HOST'), type=str,
                                help='SMTP host (default=%default)')
            parser.add_argument('--SMTP_PORT', default=config.get('SMTP_provider','SMTP_PORT'), type=str,
                                help='SMTP port (default=%default)')

        else:
            parser.add_argument('--login', default="", type=str,
                                help='gmail login (default=%default)')
            parser.add_argument('--password', default="", type=str,
                                help='gmail password (default=%default)')
            parser.add_argument('--dblogin', default="", type=str,
                                help='database login, if post=True (default=%default)')
            parser.add_argument('--dbemail', default="", type=str,
                                help='database login, if post=True (default=%default)')
            parser.add_argument('--dbpassword', default="", type=str,
                                help='database password, if post=True (default=%default)')
            parser.add_argument('--dbemailpassword', default="", type=str,
                                help='database password, if post=True (default=%default)')
            parser.add_argument('--url', default="", type=str,
                                help='URL to POST transients to a database (default=%default)')
            parser.add_argument('--hostmatchrad', default=0.001, type=float,
                                help='matching radius for hosts (arcmin) (default=%default)')
            parser.add_argument('--tnsapi', default="", type=str,
                                help='TNS API URL (default=%default)')
            parser.add_argument('--tnsapikey', default="", type=str,
                                help='TNS API key (default=%default)')
            parser.add_argument('--ztfurl', default="", type=str,
                                help='ZTF URL (default=%default)')
            parser.add_argument('--ghost_path', default="", type=str,
                                help='Prost scratch directory; legacy name, used when prost_path is unset (default=%default)')
            parser.add_argument('--prost_path', default="", type=str,
                                help='Prost scratch base directory; falls back to ghost_path (default=%default)')
            
            parser.add_argument('--SMTP_LOGIN', default='', type=str,
                                help='SMTP login (default=%default)')
            parser.add_argument('--SMTP_HOST', default='', type=str,
                                help='SMTP host (default=%default)')
            parser.add_argument('--SMTP_PORT', default='', type=str,
                                help='SMTP port (default=%default)')
            

        return(parser)

    def getTNSData(self,jd,obj,sc,ebv):

        if obj.startswith('2016'):
            status = 'Ignore'
        else:
            status = self.status

        if getattr(self, 'fixture_skip_ps', False):
            ps_prob = None
        else:
            try:
                ps_prob = get_ps_score(sc.ra.deg,sc.dec.deg)
            except:
                ps_prob = None

        # Space archival flags (not shown on main dashboard). Defer to get_archival.py or page visit.
        if getattr(self, 'fixture_skip_archival', False):
            has_hst = has_chandra = has_spitzer = None
        else:
            try:
                hst=mast_query.hstImages(sc.ra.deg,sc.dec.deg,'Object')
                hst.getObstable()
                if hst.Nimages > 0:
                    has_hst = True
                else:
                    has_hst = False
            except: has_hst = None
            try:
                chr=chandra_query.chandraImages(sc.ra.deg,sc.dec.deg,'Object')
                chr.search_chandra_database()
                if chr.n_obsid > 0:
                    has_chandra = True
                else:
                    has_chandra = False
            except: has_chandra = None
            try:
                if spitzer_query.get_bool_from_coord(sc.ra.deg,sc.dec.deg):
                    has_spitzer = True
                else:
                    has_spitzer = False
            except:
                has_spitzer = None

        TransientDict = {'name':obj,
                         'slug':obj,
                         'ra':sc.ra.deg,
                         'dec':sc.dec.deg,
                         'obs_group':'Unknown',
                         'mw_ebv':ebv,
                         'status':status,
                         'point_source_probability':ps_prob,
                         'has_hst':has_hst,
                         'has_chandra':has_chandra,
                         'has_spitzer':has_spitzer,
                         'tags':[]}

        if jd:
            TransientDict['disc_date'] = jd['discoverydate']
            TransientDict['obs_group'] = jd['reporting_group']['group_name']
            #jd['discovery_data_source']['group_name']

            if not TransientDict['obs_group']:
                TransientDict['obs_group'] = 'Unknown'
            z = jd['redshift']
            if z:
                TransientDict['redshift'] = float(z)
                TransientDict['redshift_source'] = 'TNS'  # provenance (#436, #437)
            evt_type = jd['object_type']['name']
            if evt_type:
                TransientDict['best_spec_class'] = evt_type
                TransientDict['TNS_spec_class'] = evt_type

            if jd['internal_names']:
                TransientDict['internal_names'] = jd['internal_names']
                
        return TransientDict

    def getZTFPhotometry_ANTARES(self,sc):

        if not HAS_ANTARES:
            return None

        for s in cone_search(sc, Angle("5s")):
            PhotUploadAll = {"mjdmatchmin":0.01,
                             "clobber":self.clobber}
            photometrydict = {'instrument':'ZTF-Cam',
                              'obs_group':'ZTF',
                              'photdata':{}}

            for i in range(len(s.lightcurve)):
                PhotUploadDict = {'obs_date':s.lightcurve['time'][i].replace(' ','T'),
                                  'band':'%s-ZTF'%s.lightcurve['ant_passband'][i],
                                  'groups':[]}

                if s.lightcurve['ant_mag'][i] == s.lightcurve['ant_mag'][i]:
                    PhotUploadDict['mag'] = s.lightcurve['ant_mag'][i]
                    PhotUploadDict['mag_err'] = s.lightcurve['ant_magerr'][i]
                    PhotUploadDict['flux'] = 10**(-0.4*(s.lightcurve['ant_mag'][i]-27.5))
                    PhotUploadDict['flux_err'] = 0.4*np.log(10)*PhotUploadDict['flux']*s.lightcurve['ant_magerr'][i]
                    PhotUploadDict['data_quality'] = 0
                    PhotUploadDict['forced'] = None
                    PhotUploadDict['flux_zero_point'] = None
                    PhotUploadDict['discovery_point'] = 0
                    PhotUploadDict['diffim'] = 1
                else:
                    continue

                photometrydict['photdata']['%s_%i'%(s.lightcurve['time'][i],i)] = PhotUploadDict

            PhotUploadAll['ZTF'] = photometrydict
            print('found ZTF data')
            return PhotUploadAll
    
        return None

        

        
    def get_PS_DR2_data(self, sc):
        """
        Returns a dictionary of photometric data
        {'instrument': 'GPC1', 'obs_group': 'Pan-STARRS1', 'photdata': photdata}
        photdata = {{"2019-05-09T04:35:52.002_1": {'obs_data': '2019-05-09T04:35:52.002',
                                                   'band': 'g', 'groups': [], 'mag': float,
                                                   'mag_err': float, 'flux': None, 'flux_err': None,
                                                   'forced': None, 'discovery_point': 0,
                                                   'flux_zero_point': None, } }}
        """

        #radius is in arcmin
        radius = 3./60
        ra, dec = sc.ra.deg, sc.dec.deg
        query_obj_id = """select m.objid, m.gMeanPSFMag, m.rMeanPSFMag, m.iMeanPSFMag, m.zMeanPSFMag, m.yMeanPSFMag
        from fGetNearestObjEq(%.5f,%.5f,%.5f) nb
        inner join MeanObject m on m.objid=nb.objid
        """ %(ra,dec,radius)

        jobs = mastrequests.MastCasJobs(context="PanSTARRS_DR2")
        results = jobs.quick(query_obj_id, task_name="PS1_DR2 objid search")
        results = results.split('\n')
        if results[1]:
            objid = results[1].split(',')[0]
            query = "select objID, detectID, filter=f.filterType, obsTime, ra, dec, psfFlux, psfFluxErr, "
            query += "infoFlag, infoFlag2, infoFlag3 "
            query += "from (select * from Detection where objID=%s) d " %objid
            query += "join Filter f on d.filterID=f.filterID order by d.filterID, obsTime"
            results = jobs.quick(query, task_name="PS1 DR2 detections")

            photdata = {}
            for row in ascii.read(results):
                tobj = Time(row[3], format='mjd')
                obstime = datetime.strftime(tobj.to_datetime(), "%Y-%m-%dT%H:%M:%S.%f")[:-3]
                photdata[obstime] = {}
                photdata[obstime]['obs_date'] = obstime
                photdata[obstime]['band'] = row[2]
                photdata[obstime]['mag'] = -2.5*np.log10(row[6]) + 8.90
                photdata[obstime]['mag_err'] = 2.5/np.log(10)* (row[7]/row[6])
                photdata[obstime]['forced'] = None
                photdata[obstime]['flux_zero_point'] = None
                photdata[obstime]['data_quality'] = 0
                photdata[obstime]['flux'] = None
                photdata[obstime]['flux_err'] = None
                photdata[obstime]['discovery_point'] = 0
                photdata[obstime]['groups'] = []
                photdata[obstime]['diffim'] = None
            transientphot = {'instrument': 'GPC1', 'obs_group': 'Pan-STARRS1', 'photdata': photdata}
            return transientphot
        else:
            return None

    def getTNSPhotometry(self,jd,PhotUploadAll=None):

        if not PhotUploadAll:
            PhotUploadAll = {"mjdmatchmin":0.01,
                             "clobber":self.clobber}

        tmag,tmagerr,tfilt,tinst,tobsdate,obsgroups,mjd =\
            np.array([]),np.array([]),np.array([]),np.array([]),np.array([]),np.array([]),np.array([])

        nondetectmaglim,nondetectdate,nondetectfilt,nondetectins = "","","",""
        report_ids = np.array([], dtype=object)
        from YSE_App.common.tns_photometry_map import TNS_OBS_GROUP_NAME, resolve_tns_photometry

        for p in jd['photometry']:
            resolved = resolve_tns_photometry(
                p['instrument']['name'],
                p['filters']['name'],
            )
            yse_instrument = resolved.instrument
            yse_band = resolved.band
            yse_obs_hint = resolved.obs_group_hint

            if 'mag' in p['flux_unit']['name'].lower():
                tmag = np.append(tmag,p['flux'])
                tmagerr = np.append(tmagerr,p['fluxerr'])
                if not p['flux']:
                    nondetectmaglim = p['limflux']
                    nondetectdate = p['obsdate']
                    nondetectfilt = yse_band
                    nondetectins = yse_instrument
            else:
                tmag = np.append(tmag,-99)
                tmagerr = np.append(tmagerr,-99)

            tobsdate = np.append(tobsdate,p['obsdate'])
            mjd = np.append(mjd,date_to_mjd(p['obsdate']))
            observer = p.get('observer') or yse_obs_hint
            obsgroups = np.append(obsgroups, observer)
            tinst = np.append(tinst, yse_instrument)
            tfilt = np.append(tfilt, yse_band)
            report_ids = np.append(report_ids, tns_report_id(p, TNS_PHOTOMETRY_REPORT_KEYS))

        disc_flag = np.zeros(len(tmag))
        iMag = np.where((tmag != -99) & (tmag != None))[0]
        indx = np.where((mjd[iMag] == np.min(mjd[iMag])))[0]
        disc_flag[iMag[indx]] = 1

        photometrycount = 0
        for ins in np.unique(tinst):

            # #645: TNS-reported points get their own header (a dedicated TNS obs_group), so they are
            # never mixed with the same survey's forced photometry; the reference says where they came from.
            photometrydict = {'instrument':ins,
                              'obs_group':TNS_OBS_GROUP_NAME,
                              'groups': list(TNS_IMPORT_COLLABORATION_GROUPS),
                              'reference': tns_reference_with_reports(jd, report_ids[tinst == ins]),
                              'photdata':{}}

            for f,k in zip(np.unique(tfilt),range(len(np.unique(tfilt)))):
                # put in the photometry
                for m,me,od,df in zip(tmag[(f == tfilt) & (ins == tinst)],
                                             tmagerr[(f == tfilt) & (ins == tinst)],
                                             tobsdate[(f == tfilt) & (ins == tinst)],
                                             disc_flag[(f == tfilt) & (ins == tinst)]):
                    if not m and not me: continue #and not flx and not fe: continue
                    PhotUploadDict = {'obs_date':od.replace(' ','T'),
                                      'band':f,
                                      'groups': list(TNS_IMPORT_COLLABORATION_GROUPS)}
                    if m: PhotUploadDict['mag'] = m
                    else: PhotUploadDict['mag'] = None
                    if me: PhotUploadDict['mag_err'] = me
                    else: PhotUploadDict['mag_err'] = None
                    PhotUploadDict['flux'] = None
                    PhotUploadDict['flux_err'] = None
                    if df: PhotUploadDict['discovery_point'] = 1
                    else: PhotUploadDict['discovery_point'] = 0
                    PhotUploadDict['data_quality'] = 0
                    PhotUploadDict['forced'] = None
                    PhotUploadDict['flux_zero_point'] = None
                    PhotUploadDict['diffim'] = None

                    photometrydict['photdata']['%s_%i'%(od.replace(' ','T'),k)] = PhotUploadDict
            PhotUploadAll[photometrycount] = photometrydict
            photometrycount += 1

        if nondetectdate: nondetectdate = nondetectdate.replace(' ','T')
        return PhotUploadAll,nondetectdate,nondetectmaglim,nondetectfilt,nondetectins

    def getTNSSpectra(self,jd,sc):
        specinst,specobsdate,specobsgroup,specfiles = \
            np.array([]),np.array([]),np.array([]),np.array([])
        specreports = np.array([], dtype=object)

        SpecDictAll = {'clobber':self.clobber}
        keyslist = ['ra','dec','instrument','rlap','obs_date','redshift',
                    'redshift_err','redshift_quality','spectrum_notes',
                    'obs_group','groups','spec_phase','snid','data_quality']
        requiredkeyslist = ['ra','dec','instrument','obs_date','obs_group','snid']

        for i in range(len(jd['spectra'])):
            spec = jd['spectra'][i]
            specinst = np.append(specinst,spec['instrument']['name'])
            specobsdate = np.append(specobsdate,spec['obsdate'])
            if 'source_group_name' in spec.keys():
                specobsgroup = np.append(specobsgroup,spec['source_group_name'])
            elif 'source_group' in spec.keys():
                specobsgroup = np.append(specobsgroup,spec['source_group']['name'])
            else:
                specobsgroup = np.append(specobsgroup,'Unknown')
            specfiles = np.append(specfiles,spec['asciifile'])
            specreports = np.append(specreports,tns_report_id(spec, TNS_SPECTRUM_REPORT_KEYS))
        
        for s,si,so,sog,srid in zip(specfiles,specinst,specobsdate,specobsgroup,specreports):
            Spectrum = {}
            SpecData = {}
            _remove_quietly('spec_tns_upload.txt')

            try:
                dlfileresp = get_file(s,self.tnsapikey,self.tns_bot_id,self.tns_bot_name,getattr(self,'tns_marker_type','bot'))
                if dlfileresp.status_code == 429:
                    print('TNS request failed.  Waiting 60 seconds to try again...')
                    time.sleep(60)
                    dlfileresp = get_file(s,self.tnsapikey,self.tns_bot_id,self.tns_bot_name,getattr(self,'tns_marker_type','bot'))
                dlfile = dlfileresp.text
            except:
                continue

            with open('spec_tns_upload.txt','w') as fout:
                print('# wavelength flux',file=fout)
                print('# instrument %s'%si,file=fout)
                print('# obs_date %s'%so.replace(' ','T'),file=fout)
                print('# obs_group %s'%sog,file=fout)
                print('# ra %s'%sc.ra.deg,file=fout)
                print('# dec %s'%sc.dec.deg,file=fout)
                for line in dlfile.split('\n'):
                    if ':' in line or '=' in line or line.isspace() or 'END' in line or 'GROUPS' in line or 'SNID ' in line: continue
                    print(line.replace('\n',''),file=fout)

            fin = open('spec_tns_upload.txt')
            count = 0
            for line in fin:
                line = line.replace('\n','')
                if not count: header = np.array(line.replace('# ','').split())
                for key in keyslist:
                    if line.lower().startswith('# %s '%key) and key not in Spectrum.keys():
                        Spectrum[key] = line.split()[-1].replace("'","").replace('"','')
                count += 1
            fin.close()
            if ':' in Spectrum['ra']:
                scspec = SkyCoord(Spectrum['ra'],Spectrum['dec'],FK5,unit=(u.hourangle,u.deg))
                Spectrum['ra'] = scspec.ra.deg
                Spectrum['dec'] = scspec.dec.deg
            if 'wavelength' not in header or 'flux' not in header:
                raise RuntimeError("""Error : incorrect file format.
                The header should be # wavelength flux fluxerr, with fluxerr optional.""")

            try:
                spc = {}
                spc['wavelength'] = np.loadtxt('spec_tns_upload.txt',unpack=True,usecols=[np.where(header == 'wavelength')[0][0]])
                spc['flux'] = np.loadtxt('spec_tns_upload.txt',unpack=True,usecols=[np.where(header == 'flux')[0][0]])
            except:
                raise RuntimeError("""Error : incorrect file format.
                The header should be # wavelength flux fluxerr, with fluxerr optional.""")

            if 'fluxerr' in header:
                spc['fluxerr'] = np.loadtxt(self.options.inputfile,unpack=True,usecols=[np.where(header == 'fluxerr')[0][0]])
            for key in keyslist:
                if key not in Spectrum.keys():
                    print('Warning: %s not in spectrum header'%key)

            if 'fluxerr' in spc.keys():
                for w,f,df in zip(spc['wavelength'],spc['flux'],spc['fluxerr']):
                    if f == f:
                        SpecDict = {'wavelength':w,
                                    'flux':f,
                                    'flux_err':df}
                        SpecData[w] = SpecDict
            else:
                for w,f in zip(spc['wavelength'],spc['flux']):
                    if f == f:
                        SpecDict = {'wavelength':w,
                                    'flux':f,
                                    'flux_err':None}
                        SpecData[w] = SpecDict

            #os.system('rm spec_tns_upload.txt')
            Spectrum['specdata'] = SpecData
            Spectrum['instrument'] = si
            Spectrum['obs_date'] = so
            Spectrum['obs_group'] = re.sub(r'[^\x00-\x7f]',r'',sog)
            Spectrum['groups'] = PUBLIC_COLLABORATION_GROUP_NAME
            Spectrum['reference'] = tns_reference_with_reports(jd, [srid])  # #447/#645: object plus the TNS report id, when sent
            SpecDictAll[s] = Spectrum

        return SpecDictAll

    def getProstData(
            self,
            objs,
            transient_positions,
            transient_redshift=None
    ):
        """
        Finds the information about the host galaxy given the position of the supernova.
        Parameters
        ----------
        :position : :class:`~astropy.coordinates.SkyCoord`
        On Sky position of the source to be matched.
        :name : str, default='No name'
        Name of the the object.
        Returns
        -------
        :host_information : ~astropy.coordinates.SkyCoord`
        Host position
        """
        if not HAS_ASTRO_PROST:
            print("astro_prost not installed; skipping host association")
            return None

        # The matching is YSE_App.services.prost_hosts.associate (#439): same priors and
        # catalogues; each run gets its own scratch directory under the configured base (#617).
        return _prost_hosts.associate(
            objs,
            [t.ra.deg for t in transient_positions],
            [t.dec.deg for t in transient_positions],
            getattr(self, 'prost_path', '') or self.ghost_path,
            transient_redshift=transient_redshift,
            associate_fn=associate_sample,
            absmag_cls=SnRateAbsmag,
        )

    def getProstHosts(self,jd,sc,prost_host):
        return _prost_hosts.host_dict(prost_host), ''

    def UpdateFromTNS(self,ndays=None,allowed_statuses=['New','Following','Watch','FollowupRequested','Interesting'],doTNS=True):

        if ndays:
            date_format = '%Y-%m-%d'
            datemin = (datetime.now() - timedelta(days=ndays)).strftime(date_format)
            argstring = 'created_date_gte=%s'%datemin
        else:
            argstring = 'status_in={}'.format(','.join(allowed_statuses))
        offsetcount = 0

        auth = coreapi.auth.BasicAuthentication(
            username=self.dblogin,
            password=self.dbpassword,
        )
        client = coreapi.Client(auth=auth)
        transienturl = '%stransients?limit=1000&format=json&%s&offset=%i'%(self.dburl,argstring,offsetcount)
        print(transienturl)
        schema = client.get(transienturl)

        objs,ras,decs = [],[],[]

        for transient in schema['results']:
            objs.append(transient['name'])
            ras.append(transient['ra'])
            decs.append(transient['dec'])
        
        while len(schema['results']) == 1000:
            offsetcount += 1000
            transienturl = '%stransients?limit=1000&format=json&%s&offset=%i'%(self.dburl,argstring,offsetcount)
            print(transienturl)
            schema = client.get(transienturl)
            for transient in schema['results']:
                objs.append(transient['name'])
                ras.append(transient['ra'])
                decs.append(transient['dec'])
        if self.redohost: nsn = self.GetAndUploadAllData(objs,ras,decs,doProst=True,doEBV=True,doTNS=doTNS)
        else: nsn = self.GetAndUploadAllData(objs,ras,decs,doProst=False,doEBV=False,doTNS=doTNS)
        return nsn

    def GetRecentEvents(self,ndays=None,doTNS=True):

        #date_format = '%Y-%m-%d'
        datemin = (datetime.now() - timedelta(days=ndays)).isoformat() #strftime(date_format)
        search_obj=[("ra",""), ("dec",""), ("radius",""), ("units",""),
                    ("objname",""), ("internal_name",""),("public_timestamp",datemin)]

        try:
            response=search(self.tnsapi, search_obj, self.tnsapikey, self.tns_bot_id, self.tns_bot_name, getattr(self,'tns_marker_type','bot'))
            count = 0
            while response.status_code == 429 and count < 5:
                print('TNS request failed.  Waiting 60 seconds to try again...')
                time.sleep(60)
                response=search(self.tnsapi, search_obj, self.tnsapikey, self.tns_bot_id, self.tns_bot_name, getattr(self,'tns_marker_type','bot'))
                count += 1
        except TNSRequestError as e:
            print('TNS search failed: %s'%e)
            return 0
        json_data = format_to_json(response.text)

        objs,ras,decs = [],[],[]
        if 'data' not in json_data.keys():
            return 0

        for jd in json_data['data']:
            TNSGetSingle = [("objname",jd['objname']),
                            ("photometry","0"),
                            ("spectra","0")]

            try:
                response_single=get(self.tnsapi, TNSGetSingle, self.tnsapikey, self.tns_bot_id, self.tns_bot_name, getattr(self,'tns_marker_type','bot'))
                while response_single.status_code == 429:
                    print('TNS request failed.  Waiting 60 seconds to try again...')
                    time.sleep(60)
                    response_single=get(self.tnsapi, TNSGetSingle, self.tnsapikey, self.tns_bot_id, self.tns_bot_name, getattr(self,'tns_marker_type','bot'))
            except TNSRequestError as e:
                print('TNS get failed for %s: %s'%(jd['objname'],e))
                continue

            json_data_single = format_to_json(response_single.text)

            objs.append(json_data_single['data']['objname'])
            ras.append(json_data_single['data']['ra'])
            decs.append(json_data_single['data']['dec'])
        if len(objs): nsn = self.GetAndUploadAllData(objs,ras,decs,doProst=self.redohost,doEBV=self.redohost,doTNS=doTNS)
        else: nsn = 0
        return nsn

    def GetRecentMissingEvents(self,ndays=None,doTNS=True):
        from YSE_App.models import Transient
        #date_format = '%Y-%m-%d'
        datemin = (datetime.now() - timedelta(days=ndays)).isoformat() #strftime(date_format)
        search_obj=[("ra",""), ("dec",""), ("radius",""), ("units",""),
                    ("objname",""), ("internal_name",""),("public_timestamp",datemin)]
        try:
            response=search(self.tnsapi, search_obj, self.tnsapikey, self.tns_bot_id, self.tns_bot_name, getattr(self,'tns_marker_type','bot'))
            if response.status_code == 429:
                print('TNS request failed.  Waiting 60 seconds to try again...')
                time.sleep(60)
                response=search(self.tnsapi, search_obj, self.tnsapikey, self.tns_bot_id, self.tns_bot_name, getattr(self,'tns_marker_type','bot'))
        except TNSRequestError as e:
            print('TNS search failed: %s'%e)
            return 0
        json_data = format_to_json(response.text)

        objs,ras,decs = [],[],[]
        for jd in json_data['data']:
            if len(Transient.objects.filter(name=jd['objname'])): continue
            TNSGetSingle = [("objname",jd['objname']),
                             ("photometry","1"),
                             ("spectra","0")]

            
            try:
                response_single=get(self.tnsapi, TNSGetSingle, self.tnsapikey, self.tns_bot_id, self.tns_bot_name, getattr(self,'tns_marker_type','bot'))
                if response_single.status_code == 429:
                    print('TNS request failed.  Waiting 60 seconds to try again...')
                    time.sleep(60)
                    response_single=get(self.tnsapi, TNSGetSingle, self.tnsapikey, self.tns_bot_id, self.tns_bot_name, getattr(self,'tns_marker_type','bot'))
            except TNSRequestError as e:
                print('TNS get failed for %s: %s'%(jd['objname'],e))
                continue

            json_data_single = format_to_json(response_single.text)
            objs.append(json_data_single['data']['objname'])
            ras.append(json_data_single['data']['ra'])
            decs.append(json_data_single['data']['dec'])

        if len(objs): nsn = self.GetAndUploadAllData(objs,ras,decs,doProst=self.redohost,doEBV=self.redohost,doTNS=doTNS)
        else: nsn = 0
        return nsn

    
    def ProcessTNSEmails(self):
        body = ""
        html = ""
        tns_objs = []

        ########################################################
        # Get All Email
        ########################################################
        mail =  imaplib.IMAP4_SSL('imap.gmail.com', 993) #, ssl_context=ctx

        ## NOTE: This is not the way to do this. You will want to implement an industry-standard login step ##
        mail.login(self.login, self.password)
        mail.select('TNS', readonly=False)
        retcode, msg_ids_bytes = mail.search(None, '(UNSEEN)')
        msg_ids = msg_ids_bytes[0].decode("utf-8").split(" ")

        try:
            if retcode != "OK" or msg_ids[0] == "":
                raise ValueError("No messages")

        except ValueError as err:
            print("%s. Exiting..." % err.args)
            mail.close()
            mail.logout()
            del mail
            print("Process done.")
            return 0

        objs,ras,decs = [],[],[]
        print('length of msg_ids %s' %len(msg_ids))
        for i in range(len(msg_ids)):
            if i%10 == 0:
              print("Processing emails %d/%d" %(i, len(msg_ids)))
            ########################################################
            # Iterate Over Email
            ########################################################
            typ, data = mail.fetch(msg_ids[i],'(RFC822)')
            msg = email.message_from_bytes(data[0][1])
            if 'AstroNotes' in msg['Subject']: continue
            # Mark messages as "Unseen"
            # result, wdata = mail.store(msg_ids[i], '-FLAGS', '\Seen')

            if msg.is_multipart():
                for part in msg.walk():
                    ctype = part.get_content_type()
                    cdispo = str(part.get('Content-Disposition'))

                    # skip any text/plain (txt) attachments
                    if (ctype == 'text/plain' or ctype == 'text/html') and 'attachment' not in cdispo:
                        body = part.get_payload(decode=True).decode('utf-8')  # decode
                        break
            # not multipart - i.e. plain text, no attachments, keeping fingers crossed
            else:
                body = msg.get_payload(decode=True).decode('utf-8')

            for obj in re.findall(reg_obj,body): objs.append(obj)
            for ra in re.findall(reg_ra,body): ras.append(ra)
            for dec in re.findall(reg_dec,body): decs.append(dec)
            if len(objs) != len(ras):
                raise RuntimeError("ra and dec lists aren't the same length")

            
            # Mark messages as "Seen"
            result, wdata = mail.store(msg_ids[i], '+FLAGS', '\\Seen')
        if len(objs):
            nsn = self.GetAndUploadAllData(objs,ras,decs)
        else: nsn = 0
        return nsn

    def GetAndUploadAllData(self,objs,ras,decs,doProst=True,doEBV=True,doTNS=True):
        TransientUploadDict = {}
        assert len(ras) == len(decs)

        ebvall = []
        ebvtstart = time.time()
        ebv_timeout,ned_timeout = False,False
        if doProst:

            def handler(signum, frame):
                raise Exception("timeout!")
                
            signal.signal(signal.SIGALRM, handler)
            prost_hosts = None

            try:
                signal.alarm(600)
                if type(ras[0]) == float:
                    scall = [SkyCoord(r,d,unit=u.deg) for r,d in zip(ras,decs)]
                else:
                    scall = [SkyCoord(r,d,unit=(u.hourangle,u.deg)) for r,d in zip(ras,decs)]

                prost_hosts = self.getProstData(objs, scall)
                
            except:
                print('Prost timeout!')
                ned_timeout = True

            if type(ras[0]) == float:
                scall = SkyCoord(ras,decs,frame="fk5",unit=u.deg)
            else:
                scall = SkyCoord(ras,decs,frame="fk5",unit=(u.hourangle,u.deg))
            
            signal.signal(signal.SIGALRM, handler)
            try:
                signal.alarm(600)
                ebvall = sfd(scall)*0.86
            except:
                print('MW E(B-V) timeout!')
                ebv_timeout = True

                        
            print('E(B-V)/Prost time: %.1f seconds'%(time.time()-ebvtstart))
            signal.alarm(0)

        if doEBV and not doProst:
            if type(ras[0]) == float:
                scall = SkyCoord(ras,decs,frame="fk5",unit=u.deg)
            else:
                scall = SkyCoord(ras,decs,frame="fk5",unit=(u.hourangle,u.deg))
                        
            def handler(signum, frame):
                raise Exception("timeout!")
            signal.signal(signal.SIGALRM, handler)
            try:
                signal.alarm(600)
                ebvall = sfd(scall)*0.86
            except:
                print('MW E(B-V) timeout!')
                ebv_timeout = True

        if type(ras[0]) == float:
            scall = SkyCoord(ras,decs,frame="fk5",unit=u.deg)
        else:
            scall = SkyCoord(ras,decs,frame="fk5",unit=(u.hourangle,u.deg))

                
            print('E(B-V) time: %.1f seconds'%(time.time()-ebvtstart))
            signal.alarm(0)
        
        tstart = time.time()
        print('getting TNS data')
        TNSData = []
        json_data = []
        total_objs = 0

        for j in range(len(objs)):
            if objs[j].startswith('20'):
                if doTNS:
                    TNSGetSingle = [("objname",objs[j]),
                                    ("photometry","1"),
                                    ("spectra","1")]


                    try:
                        response=get(self.tnsapi, TNSGetSingle, self.tnsapikey, self.tns_bot_id, self.tns_bot_name, getattr(self,'tns_marker_type','bot'))
                        if response.status_code == 429:
                            print('TNS failed!  waiting 60 seconds...')
                            time.sleep(60)
                            response=get(self.tnsapi, TNSGetSingle, self.tnsapikey, self.tns_bot_id, self.tns_bot_name, getattr(self,'tns_marker_type','bot'))
                    except TNSRequestError as e:
                        # no TNS data for this object; proceed as if doTNS were off for it
                        print('TNS get failed for %s: %s'%(objs[j],e))
                        json_data += [None]
                        continue

                    json_data += [format_to_json(response.text)]
                    total_objs += 1
                else:
                    json_data += [None]
            else:
                json_data += [None]
        print(time.time()-tstart)

        print('getting TNS content takes %.1f seconds'%(time.time()-tstart))
        print('updating {} transients!'.format(len(objs)))
        for j,jd in zip(range(len(objs)),json_data):
            tallstart = time.time()
            obj = objs[j]

            iobj = np.where(obj == np.array(objs))[0]
            if len(iobj) > 1: iobj = int(iobj[0])
            else: iobj = int(iobj)

            sc = scall[iobj]
            if doEBV:
                if not ebv_timeout: ebv = '%.3f'%ebvall[iobj]
                else: ebv = None
            else:
                ebv = None

            if doProst and prost_hosts is not None:
                prost_host = prost_hosts[prost_hosts['IAUID'] == objs[j]]
                if not len(prost_host): prost_host = None
            else:
                prost_host = None
                
            
            print("Object: %s\nRA: %s\nDEC: %s" % (obj,ras[iobj],decs[iobj]))

            ########################################################
            # For Item in Email, Get NED
            ########################################################

            if jd is not None:
                if type(jd['data']['objname']) == str:
                    jd = jd['data']
                else:
                    jd = None

            transientdict = self.getTNSData(jd,obj,sc,ebv)
            photdict = None
            if not getattr(self, 'fixture_skip_ztf_antares', False):
                try:
                    photdict = self.getZTFPhotometry_ANTARES(sc)
                except: photdict = None

            try:
                if jd:
                    photdict,nondetectdate,nondetectmaglim,nondetectfilt,nondetectins = \
                        self.getTNSPhotometry(jd,PhotUploadAll=photdict)
                    transientdict['transientphotometry'] = photdict
                    if not getattr(self, 'fixture_skip_spectra', False):
                        specdict = self.getTNSSpectra(jd,sc)
                        transientdict['transientspectra'] = specdict

                    if nondetectdate: transientdict['non_detect_date'] = nondetectdate
                    if nondetectmaglim: transientdict['non_detect_limit'] = nondetectmaglim
                    if nondetectfilt: transientdict['non_detect_band'] =  nondetectfilt
                    if nondetectfilt: transientdict['non_detect_instrument'] =  nondetectins
                elif photdict is not None: transientdict['transientphotometry'] = photdict
            except Exception as e: pass


            if prost_host is not None:
                hostdict,hostcoords = self.getProstHosts(jd,sc,prost_host)
                transientdict['host'] = hostdict
                transientdict['candidate_hosts'] = hostcoords
            

            TransientUploadDict[obj] = transientdict
            batch_size = getattr(self, 'upload_batch_size', 10) or 10
            if not j % batch_size:
                TransientUploadDict['noupdatestatus'] = self.noupdatestatus
                TransientUploadDict['TNS'] = True
                self.UploadTransients(TransientUploadDict)
                TransientUploadDict = {}
        batch_size = getattr(self, 'upload_batch_size', 10) or 10
        if j % batch_size:
            TransientUploadDict['noupdatestatus'] = self.noupdatestatus
            TransientUploadDict['TNS'] = True
            self.UploadTransients(TransientUploadDict)

        return(len(TransientUploadDict))

    @staticmethod
    def _format_add_transient_error(response):
        """Summarize /add_transient/ failures without dumping Django DEBUG HTML."""
        import re

        text = response.text or ''
        if 'text/html' in (response.headers.get('content-type') or '').lower() or text.lstrip().startswith('<!'):
            title = re.search(r'<title>([^<]+)</title>', text, re.I)
            exc_type = re.search(r'Exception Type:</th>\s*<td[^>]*>([^<]+)', text)
            exc_val = re.search(r'Exception Value:</th>\s*<td[^>]*>([^<]+)', text)
            parts = [f'HTTP {response.status_code} from /add_transient/']
            if title:
                parts.append(title.group(1).strip())
            if exc_type:
                parts.append(
                    f'{exc_type.group(1).strip()}: {exc_val.group(1).strip() if exc_val else ""}'.strip(': ')
                )
            parts.append(
                '(Django DEBUG HTML omitted; fix the error above or set DEBUG=False in Docker)'
            )
            return ' '.join(parts)
        return text[:2000]

    def UploadTransients(self,TransientUploadDict):

        url = '%s'%self.dburl.replace('/api','/add_transient')
        try:
            r = ingest_http.post(url = url, data = json.dumps(TransientUploadDict),
                              auth=HTTPBasicAuth(self.dblogin,self.dbpassword),
                              timeout=upload_timeout())

            if r.status_code >= 400:
                print(self._format_add_transient_error(r))
                return
            try:
                print('YSE_PZ says: %s'%json.loads(r.text)['message'])
            except (json.JSONDecodeError, KeyError, TypeError):
                print(self._format_add_transient_error(r))
        except Exception as e:
            exc_type, exc_obj, exc_tb = sys.exc_info()
            nsn = 0
            smtpserver = "%s:%s" % (self.options.SMTP_HOST, self.options.SMTP_PORT)
            from_addr = "%s@gmail.com" % self.options.SMTP_LOGIN
            subject = "TNS Transient Upload Failure in GetRecentEvents"
            print("Sending error email")
            html_msg = "Alert : YSE_PZ Failed to upload transients in TNS_uploads.TNS_recent()\n"
            html_msg += "Error : %s at line number %s"
            sendemail(from_addr, self.options.dbemail, subject,
                      html_msg%(e,exc_tb.tb_lineno),
                      self.options.SMTP_LOGIN, self.options.dbemailpassword, smtpserver)
    
            #raise RuntimeError(e)
        print("Process done.")


def jd_to_date(jd):
    time = Time(jd,scale='utc',format='jd')
    return time.isot

def date_to_mjd(obs_date):
    time = Time(obs_date,scale='utc')
    return time.mjd

def fetch(url):
    urlresponse = None
    count = 0
    while not urlresponse and count < 3:
        try:
            urlresponse = urllib.request.urlopen(url, timeout=default_timeout()[1]).read()
            return url, urlresponse
        except Exception as e:
            pass
        count += 1

    print('URL %s failed after 3 tries'%(url))
    return url, None

def run_parallel_in_threads(target, args_list):
    result = queue.Queue()
    # wrapper to collect return value in a Queue
    def task_wrapper(*args):
        result.put(target(*args))
    threads = [threading.Thread(target=task_wrapper, args=args) for args in args_list]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return result


def sendemail(from_addr, to_addr,
            subject, message,
            login, password, smtpserver, cc_addr=None):

    print("Preparing email")

    msg = MIMEMultipart('alternative')
    msg['Subject'] = subject
    msg['From'] = from_addr
    msg['To'] = to_addr
    payload = MIMEText(message, 'html')
    msg.attach(payload)

    with smtplib.SMTP(smtpserver) as server:
        try:
            server.starttls()
            server.login(login, password)
            resp = server.sendmail(from_addr, [to_addr], msg.as_string())
            print("Send success")
        except Exception as e:
            print("Send fail; %s"%e)

def format_to_json(source):
    # change data to json format and return
    parsed=json.loads(source,object_pairs_hook=OrderedDict)
    #result=json.dumps(parsed,indent=4)
    return parsed #result

class TNSRequestError(Exception):
    """Raised by search()/get() when the TNS request itself fails (no Response)."""

# function for search obj
def search(url,json_list,api_key,tns_bot_id,tns_bot_name,marker_type="bot"):
  try:
    # url for search obj
    search_url=url+'/search'
    # headers
    headers={'User-Agent': tns_marker_user_agent(tns_bot_id, tns_bot_name, marker_type)}
    # change json_list to json format
    json_file=OrderedDict(json_list)
    # construct a dictionary of api key data and search obj data
    search_data={'api_key':api_key, 'data':json.dumps(json_file)}
    # search obj using request module
    response=ingest_http.post(search_url, headers=headers, data=search_data)
    # return response
    return response
  except Exception as e:
    raise TNSRequestError('Error message : \n'+str(e)) from e

# function for get obj
def get(url,json_list,api_key,tns_bot_id,tns_bot_name,marker_type="bot"):
  try:
    # url for get obj
    get_url=url+'/object'
    # headers
    headers={'User-Agent': tns_marker_user_agent(tns_bot_id, tns_bot_name, marker_type)}
    # change json_list to json format
    json_file=OrderedDict(json_list)
    # construct a dictionary of api key data and get obj data
    get_data={'api_key':api_key, 'data':json.dumps(json_file)}
    # get obj using request module
    response=ingest_http.post(get_url, headers=headers, data=get_data)
    # return response
    return response
  except Exception as e:
    raise TNSRequestError('Error message : \n'+str(e)) from e

def get_file(url,api_key,tns_bot_id,tns_bot_name,marker_type="bot"):
  try:
    # take filename
    filename=os.path.basename(url)
    # headers
    headers={'User-Agent': tns_marker_user_agent(tns_bot_id, tns_bot_name, marker_type)}
    # downloading file using request module
    response=ingest_http.post(url, headers=headers, data={'api_key':api_key}, stream=True)
    return response
  except Exception as e:
    print ('Error message : \n'+str(e))
    return None
    
class TNS_emails(CronJobBase):

    RUN_EVERY_MINS = 3

    schedule = Schedule(run_every_mins=RUN_EVERY_MINS)
    code = 'YSE_App.data_ingest.TNS_uploads.TNS_emails'
    
    def do(self):

        code = 'YSE_App.data_ingest.TNS_uploads.TNS_emails'
        
        # execute only if run as a script
        print("running TNS emails at {}".format(datetime.now().isoformat()))
        usagestring = "TNS_Synopsis.py <options>"

        tstart = time.time()
        tnsproc = processTNS()

        # read in the options from the param file and the command line
        # some convoluted syntax here, making it so param file is not required
        parser = tnsproc.add_options(usage=usagestring)
        options,  args = parser.parse_known_args()
        options.settingsfile = "%s/settings.ini"%djangoSettings.PROJECT_DIR

        config = configparser.ConfigParser()
        config.read(options.settingsfile)

        parser = tnsproc.add_options(usage=usagestring,config=config)
        options,  args = parser.parse_known_args()
        tnsproc.hostmatchrad = options.hostmatchrad

        tnsproc.login = options.login
        tnsproc.password = options.password
        tnsproc.dblogin = options.dblogin
        tnsproc.dbpassword = options.dbpassword
        tnsproc.dbemailpassword = options.dbemailpassword
        tnsproc.dburl = options.dburl
        tnsproc.status = options.status
        tnsproc.settingsfile = options.settingsfile
        tnsproc.clobber = options.clobber
        tnsproc.noupdatestatus = options.noupdatestatus
        tnsproc.redohost = options.redohost
        tnsproc.tnsapi = options.tnsapi
        tnsproc.tnsapikey = options.tnsapikey
        tnsproc.tns_bot_id = options.tns_bot_id
        tnsproc.tns_bot_name = options.tns_bot_name
        tnsproc.ztfurl = options.ztfurl
        tnsproc.ghost_path = options.ghost_path
        tnsproc.prost_path = options.prost_path
        
        # in case of code failures
        smtpserver = "%s:%s" % (options.SMTP_HOST, options.SMTP_PORT)
        from_addr = "%s@gmail.com" % options.SMTP_LOGIN
        subject = "TNS Transient Upload Failure"
        html_msg = f"Alert : YSE_PZ Failed to upload transients in {code} \n"
        html_msg += "Error : %s at line number %s"

        
        # check for instance of code already running
        try:
            me = singleton.SingleInstance(flavor_id="1")
        except singleton.SingleInstanceException:
            print("Sending error email")
            sendemail(from_addr, options.dbemail, subject,
                      f"multiple instances of {code} are running",
                      options.SMTP_LOGIN, options.dbemailpassword, smtpserver)
            # return, not sys.exit: runcrons runs every cron in one process (#757)
            print(f"{code}: another instance is already running; skipping this run")
            return f"skipped: another instance of {code} is running"
            
        try:
            if options.update:
                tnsproc.noupdatestatus = True
                nsn = tnsproc.UpdateFromTNS()
            else:
                nsn = tnsproc.ProcessTNSEmails()
        except Exception as e:
            exc_type, exc_obj, exc_tb = sys.exc_info()
            nsn = 0
            print("Sending error email")
            sendemail(from_addr, options.dbemail, subject,
                      html_msg%(e,exc_tb.tb_lineno),
                      options.SMTP_LOGIN, options.dbemailpassword, smtpserver)

        print('TNS -> YSE_PZ took %.1f seconds for %i transients'%(time.time()-tstart,nsn))


class TNS_updates(CronJobBase):

    RUN_AT_TIMES = ['00:00', '08:00']

    schedule = Schedule(run_at_times=RUN_AT_TIMES)
    code = 'YSE_App.data_ingest.TNS_uploads.TNS_updates'
    
    def do(self):

        code = 'YSE_App.data_ingest.TNS_uploads.TNS_updates'
        
        # execute only if run as a script
        print("running TNS updates at {}".format(datetime.now().isoformat()))
        usagestring = "TNS_Synopsis.py <options>"

        tstart = time.time()
        tnsproc = processTNS()

        # read in the options from the param file and the command line
        # some convoluted syntax here, making it so param file is not required
        parser = tnsproc.add_options(usage=usagestring)
        options,  args = parser.parse_known_args()
        options.settingsfile = "%s/settings.ini"%djangoSettings.PROJECT_DIR
        
        config = configparser.ConfigParser()
        config.read(options.settingsfile)

        parser = tnsproc.add_options(usage=usagestring,config=config)
        options,  args = parser.parse_known_args()
        tnsproc.hostmatchrad = options.hostmatchrad

        tnsproc.login = options.login
        tnsproc.password = options.password
        tnsproc.dblogin = options.dblogin
        tnsproc.dbpassword = options.dbpassword
        tnsproc.dbemailpassword = options.dbemailpassword
        tnsproc.dburl = options.dburl
        tnsproc.status = options.status
        tnsproc.settingsfile = options.settingsfile
        tnsproc.clobber = options.clobber
        tnsproc.noupdatestatus = options.noupdatestatus
        tnsproc.redohost = False #True
        tnsproc.tnsapi = options.tnsapi
        tnsproc.tnsapikey = options.tnsapikey
        tnsproc.tns_bot_id = options.tns_bot_id
        tnsproc.tns_bot_name = options.tns_bot_name
        tnsproc.ztfurl = options.ztfurl
        tnsproc.ghost_path = options.ghost_path
        
        # in case of code failures
        smtpserver = "%s:%s" % (options.SMTP_HOST, options.SMTP_PORT)
        from_addr = "%s@gmail.com" % options.SMTP_LOGIN
        subject = "TNS Transient Upload Failure"
        html_msg = f"Alert : YSE_PZ Failed to upload transients in {code}\n"
        html_msg += "Error : %s at line number %s"

        
        # check for instance of code already running
        try:
            me = singleton.SingleInstance(flavor_id="2")
        except singleton.SingleInstanceException:
            print("Sending error email")
            sendemail(from_addr, options.dbemail, subject,
                      f"multiple instances of {code} are running",
                      options.SMTP_LOGIN, options.dbemailpassword, smtpserver)
            # return, not sys.exit: runcrons runs every cron in one process (#757)
            print(f"{code}: another instance is already running; skipping this run")
            return f"skipped: another instance of {code} is running"
        
        try:
            tnsproc.noupdatestatus = True
            nsn = tnsproc.UpdateFromTNS(ndays=options.ndays,doTNS=False)
        except Exception as e:
            exc_type, exc_obj, exc_tb = sys.exc_info()
            nsn = 0
            print("Sending error email")
            sendemail(from_addr, options.dbemail, subject,
                      html_msg%(e,exc_tb.tb_lineno),
                      options.SMTP_LOGIN, options.dbemailpassword, smtpserver)

        print('TNS -> YSE_PZ took %.1f seconds for %i transients'%(time.time()-tstart,nsn))

class TNS_Ignore_updates(CronJobBase):

    RUN_AT_TIMES = ['00:00']
    RUN_EVERY_MINS = 4320
    schedule = Schedule(run_every_mins=RUN_EVERY_MINS,run_at_times=RUN_AT_TIMES)

    code = 'YSE_App.data_ingest.TNS_uploads.TNS_Ignore_updates'
    
    def do(self):

        code = 'YSE_App.data_ingest.TNS_uploads.TNS_Ignore_updates'
        
        # execute only if run as a script
        print("running TNS updates for Ignore transients at {}".format(datetime.now().isoformat()))
        usagestring = "TNS_Synopsis.py <options>"

        tstart = time.time()
        tnsproc = processTNS()

        # read in the options from the param file and the command line
        # some convoluted syntax here, making it so param file is not required
        parser = tnsproc.add_options(usage=usagestring)
        options,  args = parser.parse_known_args()
        options.settingsfile = "%s/settings.ini"%djangoSettings.PROJECT_DIR
        
        config = configparser.ConfigParser()
        config.read(options.settingsfile)

        parser = tnsproc.add_options(usage=usagestring,config=config)
        options,  args = parser.parse_known_args()
        tnsproc.hostmatchrad = options.hostmatchrad

        tnsproc.login = options.login
        tnsproc.password = options.password
        tnsproc.dblogin = options.dblogin
        tnsproc.dbpassword = options.dbpassword
        tnsproc.dbemailpassword = options.dbemailpassword
        tnsproc.dburl = options.dburl
        tnsproc.status = options.status
        tnsproc.settingsfile = options.settingsfile
        tnsproc.clobber = options.clobber
        tnsproc.noupdatestatus = options.noupdatestatus
        tnsproc.redohost = False #True
        tnsproc.tnsapi = options.tnsapi
        tnsproc.tnsapikey = options.tnsapikey
        tnsproc.tns_bot_id = options.tns_bot_id
        tnsproc.tns_bot_name = options.tns_bot_name
        tnsproc.ztfurl = options.ztfurl
        tnsproc.ghost_path = options.ghost_path
        
        # in case of code failures
        smtpserver = "%s:%s" % (options.SMTP_HOST, options.SMTP_PORT)
        from_addr = "%s@gmail.com" % options.SMTP_LOGIN
        subject = "TNS Transient Upload Failure"
        html_msg = f"Alert : YSE_PZ Failed to upload transients in {code}\n"
        html_msg += "Error : %s at line number %s"

        
        # check for instance of code already running
        try:
            me = singleton.SingleInstance(flavor_id="3")
        except singleton.SingleInstanceException:
            print("Sending error email")
            sendemail(from_addr, options.dbemail, subject,
                      f"multiple instances of {code} are running",
                      options.SMTP_LOGIN, options.dbemailpassword, smtpserver)
            # return, not sys.exit: runcrons runs every cron in one process (#757)
            print(f"{code}: another instance is already running; skipping this run")
            return f"skipped: another instance of {code} is running"
        
        try:
            tnsproc.noupdatestatus = True
            nsn = tnsproc.UpdateFromTNS(ndays=30,allowed_statuses=['Ignore'],doTNS=False)
        except Exception as e:
            exc_type, exc_obj, exc_tb = sys.exc_info()
            nsn = 0
            print("Sending error email")
            sendemail(from_addr, options.dbemail, subject,
                      html_msg%(e,exc_tb.tb_lineno),
                      options.SMTP_LOGIN, options.dbemailpassword, smtpserver)

        print('TNS -> YSE_PZ took %.1f seconds for %i transients'%(time.time()-tstart,nsn))
        
class TNS_recent(CronJobBase):

    RUN_AT_TIMES = ['00:00', '08:00']

    schedule = Schedule(run_at_times=RUN_AT_TIMES)
    code = 'YSE_App.data_ingest.TNS_uploads.TNS_recent'
    
    def do(self):

        code = 'YSE_App.data_ingest.TNS_uploads.TNS_recent'
        
        # execute only if run as a script
        print("checking for recent TNS events at {}".format(datetime.now().isoformat()))
        usagestring = "TNS_Synopsis.py <options>"

        tstart = time.time()
        tnsproc = processTNS()

        # read in the options from the param file and the command line
        # some convoluted syntax here, making it so param file is not required
        parser = tnsproc.add_options(usage=usagestring)
        options,  args = parser.parse_known_args()
        options.settingsfile = "%s/settings.ini"%djangoSettings.PROJECT_DIR
        
        config = configparser.ConfigParser()
        config.read(options.settingsfile)

        parser = tnsproc.add_options(usage=usagestring,config=config)
        options,  args = parser.parse_known_args()
        tnsproc.hostmatchrad = options.hostmatchrad

        tnsproc.login = options.login
        tnsproc.password = options.password
        tnsproc.dblogin = options.dblogin
        tnsproc.dbpassword = options.dbpassword
        tnsproc.dbemailpassword = options.dbemailpassword
        tnsproc.dburl = options.dburl
        tnsproc.status = options.status
        tnsproc.settingsfile = options.settingsfile
        tnsproc.clobber = options.clobber
        tnsproc.noupdatestatus = options.noupdatestatus
        tnsproc.redohost = True
        tnsproc.tnsapi = options.tnsapi
        tnsproc.tnsapikey = options.tnsapikey
        tnsproc.tns_bot_id = options.tns_bot_id
        tnsproc.tns_bot_name = options.tns_bot_name
        tnsproc.ztfurl = options.ztfurl
        tnsproc.ghost_path = options.ghost_path
        tnsproc.ndays = float(options.tns_recent_ndays)
        tnsproc.tns_fastupdates_nminutes = float(options.tns_fastupdates_nminutes)

        # in case of errors
        smtpserver = "%s:%s" % (options.SMTP_HOST, options.SMTP_PORT)
        from_addr = "%s@gmail.com" % options.SMTP_LOGIN
        subject = "TNS Transient Upload Failure in GetRecentEvents"
        html_msg = f"Alert : YSE_PZ Failed to upload transients in {code}\n"
        html_msg += "Error : %s at line number %s"

        
        # check for instance of code already running
        try:
            me = singleton.SingleInstance(flavor_id="4")
        except singleton.SingleInstanceException:
            print("Sending error email")
            sendemail(from_addr, options.dbemail, subject,
                      f"multiple instances of {code} are running",
                      options.SMTP_LOGIN, options.dbemailpassword, smtpserver)
            # return, not sys.exit: runcrons runs every cron in one process (#757)
            print(f"{code}: another instance is already running; skipping this run")
            return f"skipped: another instance of {code} is running"
            
        try:
            tnsproc.noupdatestatus = True
            nsn = tnsproc.GetRecentMissingEvents(ndays=tnsproc.ndays)
        except Exception as e:
            exc_type, exc_obj, exc_tb = sys.exc_info()
            nsn = 0
            print("Sending error email")
            sendemail(from_addr, options.dbemail, subject,
                      html_msg%(e,exc_tb.tb_lineno),
                      options.SMTP_LOGIN, options.dbemailpassword, smtpserver)

        print('TNS -> YSE_PZ took %.1f seconds for %i transients'%(time.time()-tstart,nsn))

class TNS_recent_realtime(CronJobBase):
    """Removed (#438); kept only so an old ``runcrons`` crontab line stays harmless.

    It searched TNS for objects made public in the last ``tns_fastupdates_nminutes`` (10)
    minutes but was scheduled only at 00:00 and 08:00, so it covered about 20 minutes a
    day. ``TNS_emails`` (every 3 minutes) already imports new objects within minutes,
    ``TNS_recent`` covers the last ``tns_recent_ndays`` twice a day, and the TNS re-sync
    sweep (``TNS_Resync.TNSResync``) imports anything public in the last
    ``TNS_RESYNC_GAP_DAYS`` that is still missing. Not in ``CRON_CLASSES``.
    """

    RUN_AT_TIMES = ['00:00', '08:00']

    schedule = Schedule(run_at_times=RUN_AT_TIMES)
    code = 'YSE_App.data_ingest.TNS_uploads.TNS_recent_realtime'

    def do(self):
        print("TNS_recent_realtime was removed (#438); remove its crontab line. Nothing to do.")
        return "removed"

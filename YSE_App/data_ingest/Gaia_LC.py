import pandas as pd
import datetime
from astropy.time import Time
import numpy as np
import time
from django_cron import CronJobBase, Schedule
from django.conf import settings as djangoSettings
import json
import requests
import sys
from requests.auth import HTTPBasicAuth
import configparser
from YSE_App.models import Transient, TransientTag, AlternateTransientNames
from YSE_App.models.phot_models import TransientPhotometry
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import smtplib

def date_to_mjd(obs_date):
    time = Time(obs_date,scale='utc')
    return time.mjd

def mjd_to_date(obs_mjd):
    time = Time(obs_mjd,scale='utc',format='mjd')
    return time.isot

def get_gaia_list(look_back_days):
    """Gaia alerts published in the last ``look_back_days`` (``None``: every alert)."""
    gaia_list = pd.read_csv('https://gsaweb.ast.cam.ac.uk/alerts/alerts.csv')
    if look_back_days is None:
        return gaia_list
    datelist = np.array([(datetime.datetime.now()-datetime.datetime.strptime(date, '%Y-%m-%d %H:%M:%S')).\
                         total_seconds()/86400 for date in gaia_list[' Date'].values])
    index = datelist<look_back_days
    targets = gaia_list[index]
    return targets

def get_gaia_phot(name, targets):
    #index = (targets[' TNSid']=='AT'+name) | (targets[' TNSid']=='SN'+name)
    index = targets['#Name'] == name
    if sum(index) >0:
        gaia_name = targets[index]['#Name'].values[0]
        data = pd.read_csv('https://gsaweb.ast.cam.ac.uk/alerts/alert/'+gaia_name+'/lightcurve.csv/')
        return data
    else:
        return

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
        except:
            print("Send fail")

    
def gaia_photometry(gaia_name, targets):
    """The ``transientphotometry`` upload document for one Gaia alert, or None (no alert / no points)."""
    data = get_gaia_phot(gaia_name,targets)
    if data is None: return None
    data = data[gaia_name]

    PhotUploadAll = {"mjdmatchmin":0.01,
                     "clobber":True}
    photometrydict = {'instrument':'Gaia-Photometric',
                      'obs_group':'GaiaAlerts',
                      'photdata':{}}

    for i,d in enumerate(data):
        if not isinstance(data[i],float):
            if data[i] == 'untrusted' or data[i].startswith('average') or \
               data[i] == 'nan': continue
        if float(data[i]) != float(data[i]): continue

        flux = 10**(-0.4*(float(data[i])-27.5))
        flux_err = 1.086*0.01*flux
        phot_upload_dict = {'obs_date':data.index[i][0].replace(' ','T'),
                            'band':'G',
                            'groups':['Public'],
                            'mag':float(data[i]),
                            'mag_err':0.01,
                            'flux':flux,
                            'flux_err':flux_err,
                            'flux_zero_point':27.5,
                            'data_quality':0,
                            'discovery_point':0,
                            'forced':0,
                            'diffim':1}
        photometrydict['photdata'][i] = phot_upload_dict

    PhotUploadAll['Gaia-Photometric'] = photometrydict
    return PhotUploadAll


def _points(PhotUploadAll):
    return sum(len(v.get('photdata', {})) for v in PhotUploadAll.values() if isinstance(v, dict))


def candidate_transients(missing_only=True):
    """Transients with a Gaia alert name; ``missing_only``: those without Gaia photometry."""
    qs = Transient.objects.filter(
        pk__in=AlternateTransientNames.objects.filter(name__startswith='Gaia').values('transient_id'))
    if missing_only:
        qs = qs.exclude(pk__in=TransientPhotometry.objects.filter(
            instrument__name='Gaia-Photometric').values('transient_id'))
    return qs


def make_context():
    return {}


def gaia_one(transient, ctx):
    """Enrichment ``run_one`` for ``gaia-lc`` (backfill outside the cron's 7-day window).

    Same upload document as the cron, through the same ``add_transient`` code path in-process
    as the service account (public Gaia points, as before).
    """
    from YSE_App import data_utils
    from YSE_App.common.authz import bulk_ingest_denial
    from YSE_App.services.enrichment import NO_MATCH, UPDATED, EnrichmentError, Outcome

    if 'targets' not in ctx:
        ctx['targets'] = get_gaia_list(None)
    alt = AlternateTransientNames.objects.filter(transient=transient, name__startswith='Gaia').order_by('pk').first()
    if alt is None:
        return Outcome(NO_MATCH, 'no Gaia alert name')
    PhotUploadAll = gaia_photometry(alt.name, ctx['targets'])
    if PhotUploadAll is None:
        return Outcome(NO_MATCH, 'no light curve for %s' % alt.name)
    user = ctx['user']
    denial = bulk_ingest_denial(user)
    if denial:
        raise EnrichmentError('%s: %s' % (user.username, denial))
    response = data_utils.add_transient_payload(
        {transient.name: {'name': transient.name, 'transientphotometry': PhotUploadAll}, 'noupdatestatus': True},
        user)
    if response.status_code >= 400:
        from YSE_App.services.enrichment import ERROR
        return Outcome(ERROR, error='upload of %s refused: HTTP %d' % (transient.name, response.status_code))
    return Outcome(UPDATED, '%d Gaia points (%s)' % (_points(PhotUploadAll), alt.name))


class GaiaLC(CronJobBase):

    RUN_AT_TIMES = ['00:00','08:00']

    schedule = Schedule(run_at_times=RUN_AT_TIMES)
    code = 'YSE_App.data_ingest.Gaia_LC.GaiaLC'

    def do(self):
        
        print("uploading Gaia LCs at {}".format(datetime.datetime.now().isoformat()))

        tstart = time.time()
            
        parser = self.add_options()
        options,  args = parser.parse_known_args()

        config = configparser.ConfigParser()
        config.read("%s/settings.ini"%djangoSettings.PROJECT_DIR)
        parser = self.add_options(config=config)
        options,  args = parser.parse_known_args()
        self.options = options
        nsn = 0

        try:
            self.main()
        except Exception as e:
            exc_type, exc_obj, exc_tb = sys.exc_info()
            print(e)
            nsn = 0

            smtpserver = "%s:%s" % (options.SMTP_HOST, options.SMTP_PORT)
            from_addr = "%s@gmail.com" % options.SMTP_LOGIN
            subject = "Gaia Upload Failure in Gaia_LC.py"
            print("Sending error email")
            html_msg = "Alert : YSE_PZ Failed to upload Gaia LCs in Gaia_LC.py\n"
            html_msg += "Error : %s at line number %s"
            sendemail(from_addr, options.dbemail, subject,
                      html_msg%(e,exc_tb.tb_lineno),
                      options.SMTP_LOGIN, options.dbpassword, smtpserver)

        print('Gaia -> YSE_PZ took %.1f seconds for %i transients'%(time.time()-tstart,nsn))

    def add_options(self, parser=None, usage=None, config=None):
        import argparse
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

        if config:
            parser.add_argument('--dblogin', default=config.get('main','dblogin'), type=str,
                              help='database login, if post=True (default=%default)')
            parser.add_argument('--dbemail', default=config.get('main','dbemail'), type=str,
                              help='database login, if post=True (default=%default)')
            parser.add_argument('--dbpassword', default=config.get('main','dbpassword'), type=str,
                              help='database password, if post=True (default=%default)')
            parser.add_argument('--dburl', default=config.get('main','dburl'), type=str,
                              help='URL to POST transients to a database (default=%default)')

            
            parser.add_argument('--SMTP_LOGIN', default=config.get('SMTP_provider','SMTP_LOGIN'), type=str,
                              help='SMTP login (default=%default)')
            parser.add_argument('--SMTP_HOST', default=config.get('SMTP_provider','SMTP_HOST'), type=str,
                              help='SMTP host (default=%default)')
            parser.add_argument('--SMTP_PORT', default=config.get('SMTP_provider','SMTP_PORT'), type=str,
                              help='SMTP port (default=%default)')
        else:
            pass


        return(parser)
        
    def main(self):
        """Gaia light curves of transients discovered in the last 7 days (as before #439).

        Uploads go to /add_transient in batches of 10, as before; each transient's outcome is
        also recorded as a run of the ``gaia-lc`` enrichment service.
        """
        transients = AlternateTransientNames.objects.\
                     filter(name__startswith='Gaia').\
                     filter(transient__disc_date__gt=datetime.datetime.now()-datetime.timedelta(days=7))
        targets = get_gaia_list(7)

        batch, outcomes = {}, []
        for t in transients:
            print("Adding Gaia data for %s"%t.transient.name)
            PhotUploadAll = gaia_photometry(t.name, targets)
            if PhotUploadAll is None:
                continue
            batch[t.transient.name] = {'name':t.transient.name, 'transientphotometry':PhotUploadAll}
            outcomes.append((t.transient, _points(PhotUploadAll)))
            if len(batch) >= 10:
                self._send_and_record(batch, outcomes)
                batch, outcomes = {}, []
        self._send_and_record(batch, outcomes)

    def _send_and_record(self, batch, outcomes):
        if not batch:
            return
        ok = self.send_data(batch)
        try:
            from YSE_App.services import enrichment
            service = enrichment.get('gaia-lc')
            user = enrichment.service_user()
            for transient, npoints in outcomes:
                outcome = (enrichment.Outcome(enrichment.UPDATED, '%d Gaia points uploaded' % npoints)
                           if ok else enrichment.Outcome(enrichment.ERROR, error='upload to /add_transient failed'))
                enrichment.record(service, transient, outcome, user)
        except Exception as e:  # recording is bookkeeping; never fail the upload cron for it
            print('Gaia LC: could not record runs: %s' % e)

    def send_data(self,TransientUploadDict):

        TransientUploadDict['noupdatestatus'] = True
        return self.UploadTransients(TransientUploadDict)

    def UploadTransients(self,TransientUploadDict):

        url = '%s'%self.options.dburl.replace('/api','/add_transient')
        try:
            r = requests.post(url = url, data = json.dumps(TransientUploadDict),
                              auth=HTTPBasicAuth(self.options.dblogin,self.options.dbpassword),
                              timeout=60)
            try: print('YSE_PZ says: %s'%json.loads(r.text)['message'])
            except: print(r.text)
            print("Process done.")
            return r.status_code < 400

        except Exception as e:
            print("Error: %s"%e)
            return False

"""SDSS DR16 photo-z of host galaxies (cron ``SDSS_Photo_Z.YSE``).

Ported to the enrichment framework (#439; ``manage.py enrich --service sdss-photoz``): the cron
runs every 4 hours over the same hosts as before (linked to a transient, no ``photo_z``)
through :func:`fetch_photoz`, and every host that came back from the SDSS query is recorded
as a run of the ``sdss-photoz`` service. SciServer is imported only when a query runs.
It writes ``photo_z`` / ``photo_z_err`` / ``photo_z_source`` only, never ``redshift``.
"""
from django_cron import CronJobBase, Schedule
from YSE_App.models.host_models import Host
import datetime
import numpy as np
import pandas as pd
import sys

def jobDescriber(jobDescription):
    # Prints the results of the CasJobs job status functions in a human-readable manner
    # Input: the python dictionary returned by getJobStatus(jobId) or waitForJob(jobId)
    # Output: prints the dictionary to screen with readable formatting
    import pandas
    
    if (jobDescription["Status"] == 0):
        status_word = 'Ready'
    elif (jobDescription["Status"] == 1):
        status_word = 'Started'
    elif (jobDescription["Status"] == 2):
        status_word = 'Cancelling'
    elif (jobDescription["Status"] == 3):
        status_word = 'Cancelled'
    elif (jobDescription["Status"] == 4):
        status_word = 'Failed'
    elif (jobDescription["Status"] == 5):
        status_word = 'Finished'
    else:
        status_word = 'Status not found!!!!!!!!!'

    print('JobID: ', jobDescription['JobID'])
    print('Status: ', status_word, ' (', jobDescription["Status"],')')
    print('Target (context being searched): ', jobDescription['Target'])
    print('Message: ', jobDescription['Message'])
    print('Created_Table: ', jobDescription['Created_Table'])
    print('Rows: ', jobDescription['Rows'])
    wait = pandas.to_datetime(jobDescription['TimeStart']) - pandas.to_datetime(jobDescription['TimeSubmit'])
    duration = pandas.to_datetime(jobDescription['TimeEnd']) - pandas.to_datetime(jobDescription['TimeStart'])
    print('Wait time: ',wait.seconds,' seconds')
    print('Query duration: ',duration.seconds, 'seconds')

def fetch_photoz(hosts, user=None, password=None):
    """``{host.pk: (photo_z, photo_z_err)}`` from SDSS DR16 ``Photoz`` for the hosts that matched."""
    import SciServer
    from SciServer import CasJobs
    from SciServer import SkyQuery  # noqa: F401 - loads SciServer.SkyQuery used below

    hosts = list(hosts)
    if not hosts:
        return {}
    my_index = np.array(range(0,len(hosts))) #dummy index used in DF, then used to create a mapping from matched galaxies back to these hosts
    host_dictionary = dict(zip(my_index,hosts))

    RA = []
    DEC = []
    for i,H in enumerate(hosts): #get rid of fake entries
        if H.ra and H.dec:
            RA.append(H.ra)
            DEC.append(H.dec)
        else:
            host_dictionary.pop(i)
        
    DF_pre=pd.DataFrame()
    DF_pre['myindex'] = list(host_dictionary.keys())
    DF_pre['RA'] = RA
    DF_pre['DEC'] = DEC
    
    from django.conf import settings as _dj_settings
    user = user or _dj_settings.SCISERVER_USER
    password = password or _dj_settings.SCISERVER_PASSWORD
    if not user or not password:
        raise RuntimeError('SciServer credentials are not configured: set sciserveruser and sciserverpass under [main] in settings.ini (or SCISERVER_USER and SCISERVER_PASSWORD).')
    SciServer.Authentication.login(user,password)
    
    try:
        SciServer.SkyQuery.dropTable('SDSSTable1', datasetName='MyDB')
        SciServer.SkyQuery.dropTable('SDSSTable2', datasetName='MyDB')
    except Exception as e:
        print('tables are not in CAS MyDB, continuing')
        print('if system says table is already in DB, contact Andrew Engel')
        
    SciServer.CasJobs.uploadPandasDataFrameToTable(DF_pre, 'SDSSTable1', context='MyDB')
    
    
    myquery = 'SELECT s.myindex, p.z, p.zErr, p.nnAvgZ '
    myquery+= 'INTO MyDB.SDSSTable2 '
    myquery+= 'FROM MyDB.SDSSTable1 s '
    myquery+= 'CROSS APPLY dbo.fGetNearestObjEq(s.RA,s.DEC,1.0/60.0) AS nb '
    myquery+= 'INNER JOIN Photoz p ON p.objID = nb.objID'

    jobID = CasJobs.submitJob(sql=myquery, context="DR16")

    waited=SciServer.CasJobs.waitForJob(jobId=jobID)
    
    jobDescription = CasJobs.getJobStatus(jobID)

    jobDescriber(jobDescription)
    
    PhotoDF = SciServer.SkyQuery.getTable('SDSSTable2', datasetName='MyDB', top=10)

    #change this
    PhotoDF.drop_duplicates(subset='#myindex',inplace=True) #neccessary to patch some bugs
    PhotoDF.dropna(inplace=True)
                
    whats_left = PhotoDF['#myindex'].values
    point_estimates = PhotoDF['z'].values
    error = PhotoDF['zErr'].values
    other_z = PhotoDF['nnAvgZ']
    
    point_estimates[error < -9998] = other_z[error < -9998] #if there is a bad error, then authors write this is more effective

    out = {}
    for i,value in enumerate(whats_left):
        if value in host_dictionary:
            out[host_dictionary[value].pk] = (float(point_estimates[i]), float(error[i]))
    return out


def candidate_hosts():
    """The cron's selection: hosts linked to a transient without ``photo_z``."""
    return Host.objects.filter(transient__isnull=False, photo_z__isnull=True).distinct()


def run_batch(hosts, ctx=None):
    """Enrichment ``run_batch`` for ``sdss-photoz``: one Outcome per host that matched SDSS."""
    from YSE_App.services.enrichment import UPDATED, Outcome

    by_pk = {h.pk: h for h in hosts}
    outcomes = {}
    for pk, (z, z_err) in fetch_photoz(list(by_pk.values())).items():
        host = by_pk[pk]
        host.photo_z = z
        host.photo_z_err = z_err
        #T.host.photo_z_posterior = posterior[i] #Gautham suggested we add it to the host model
        host.photo_z_source = 'SDSS'
        host.save() #takes a long time and then my query needs to be reset which is also a long time
        outcomes[pk] = Outcome(UPDATED, "photo_z %.4f +- %.4f (SDSS)" % (z, z_err),
                               data={"photo_z": z, "photo_z_err": z_err, "photo_z_source": "SDSS"})
    return outcomes


class YSE(CronJobBase):

    RUN_EVERY_MINS = 240

    schedule = Schedule(run_every_mins=RUN_EVERY_MINS)
    code = 'YSE_App.data_ingest.SDSS_Photo_Z.YSE'

    def do(self,user=None,password=None,search=1,path_to_model='YSE_DNN_photoZ_model_315.hdf5',debug=True):
        """SDSS photo-z for hosts without one; same selection and schedule as before #439."""
        try:
            nowdate = datetime.datetime.utcnow() - datetime.timedelta(1)
            print('Entered the photo_z cron')
            from YSE_App.services import enrichment
            hosts = list(candidate_hosts())
            if not hosts:
                print('SDSS photo_z cron: no hosts to do')
                return
            summary = enrichment.run(enrichment.get('sdss-photoz'), hosts, dry_run=False)
            print('SDSS photo_z cron: %s' % summary.counts)
            print('time taken with upload:', datetime.datetime.utcnow() - nowdate)
        except Exception as e:
            exc_type, exc_obj, exc_tb = sys.exc_info()
            print("""Photo-z cron failed with error %s at line number %s"""%(e,exc_tb.tb_lineno))

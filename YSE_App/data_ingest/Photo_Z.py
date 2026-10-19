"""Internal ML photo-z of host galaxies from SDSS DR16 magnitudes (cron ``Photo_Z.YSE``).

Ported to the enrichment framework (#439; ``manage.py enrich --service photoz-ml``): the cron
runs every 30 minutes over the same hosts as before (linked to a transient, no
``photo_z_internal``) through :func:`predict_hosts`, and every host that came back from the
SDSS query is recorded as a run of the ``photoz-ml`` service. TensorFlow and SciServer are
imported only when a prediction runs.
"""
from django_cron import CronJobBase, Schedule
from YSE_App.models.host_models import Host
from django.conf import settings as djangoSettings
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

def predict_hosts(hosts, user=None, password=None):
    """Run the SDSS query and the ML model for ``hosts``; ``{host.pk: (z, z_err) or None}``.

    Only hosts that came back from the SDSS query are in the result: ``None`` means the
    quality filter rejected the galaxy. Raises on configuration or SciServer errors.
    """
    import SciServer
    from SciServer import CasJobs
    from SciServer import SkyQuery  # noqa: F401 - loads SciServer.SkyQuery used below
    from tensorflow import keras

    hosts = list(hosts)
    if not hosts:
        return {}
    model_filepath = 'MLP_EBOSS_1.hdf5'
    filter_filepath = 'MLP_EBOSS_BINARY_FILTER.hdf5'



    NB_BINS = 272 #filter has 2, model has 272
    BATCH_SIZE = 64
    ZMIN = 0.0
    ZMAX = 0.6 #filter has 0.8
    BIN_SIZE = (ZMAX - ZMIN) / NB_BINS
    range_z = np.linspace(ZMIN, ZMAX, NB_BINS + 1)[:NB_BINS]
    
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
        SciServer.SkyQuery.dropTable('PTable1', datasetName='MyDB')
        SciServer.SkyQuery.dropTable('PTable2', datasetName='MyDB')
    except Exception as e:
        print('tables are not in CAS MyDB, continuing')
        print('if system says table is already in DB, contact Andrew Engel')
        
    SciServer.CasJobs.uploadPandasDataFrameToTable(DF_pre, 'PTable1', context='MyDB')
    
    
    myquery = 'SELECT s.myindex, g.dered_u, g.dered_g, g.dered_r, g.dered_i, g.dered_z, '
    myquery+= 'g.dered_u - g.dered_g as ug, g.dered_u - g.dered_r as ur, g.dered_u - g.dered_i as ui, g.dered_u - g.dered_z as uz, '
    myquery+= 'g.dered_g - g.dered_r as gr, g.dered_g - g.dered_i as gi, g.dered_g - g.dered_z as gz, '
    myquery+= 'g.dered_r - g.dered_i as ri, g.dered_r - g.dered_z as rz, g.dered_i - g.dered_z as iz, '
    myquery+= 'g.dered_u / g.dered_z as u_over_z, g.petroR90_r/g.petroR50_r AS C '
    myquery+= 'INTO MyDB.PTable2 '
    myquery+= 'FROM MyDB.PTable1 s '
    myquery+= 'CROSS APPLY dbo.fGetNearestObjEq(s.RA,s.DEC,1.0/60.0) AS nb '
    myquery+= 'INNER JOIN Galaxy g ON g.objID = nb.objID'

    jobID = CasJobs.submitJob(sql=myquery, context="DR16")

    waited=SciServer.CasJobs.waitForJob(jobId=jobID)
    
    jobDescription = CasJobs.getJobStatus(jobID)

    jobDescriber(jobDescription)
    
    PhotoDF = SciServer.SkyQuery.getTable('PTable2', datasetName='MyDB', top=10)

    #change this
    PhotoDF.drop_duplicates(subset='#myindex',inplace=True) #neccessary to patch some bugs
    PhotoDF.dropna(inplace=True)
                
    cols=['dered_u', 'dered_g', 'dered_r', 'dered_i', 'dered_z', 'ug', 'ur', 'ui', 'uz', 'gr', 'gi', 'gz', 'ri', 'rz', 'iz', 'u_over_z', 'C']
    X = PhotoDF[cols]
    #Z = PhotoDF['REDSHIFT'].values
    INDICES = PhotoDF['#myindex'].values
    X = X.values
    
    def Filter():
        IN = keras.layers.Input((17,))
        dense1 = keras.layers.Dense(20,activation=keras.activations.tanh)(IN) #filter has 20, tanh
        drop1 = keras.layers.Dropout(0.05)(dense1)
        
        dense2 = keras.layers.Dense(20,activation=keras.activations.tanh)(drop1) #filter has 20, tanh
        drop2 = keras.layers.Dropout(0.05)(dense2) #0.1
        
        dense3 = keras.layers.Dense(20,activation=keras.activations.tanh)(drop2) #filter has 20, tanh
        drop3 = keras.layers.Dropout(0.05)(dense3)
        
        dense4 = keras.layers.Dense(20,activation=keras.activations.tanh)(drop3) #filter has 20, tanh
        drop4 = keras.layers.Dropout(0.05)(dense4)#0.1
        
        dense5 = keras.layers.Dense(2,activation=keras.activations.softmax)(drop4) #filter has 2
        
        model = keras.Model(inputs=[IN],outputs=[dense5])
        return(model)
    
    def MLP():
        IN = keras.layers.Input((17,))
        dense1 = keras.layers.Dense(45,activation=keras.activations.relu)(IN) #filter has 20, tanh
        drop1 = keras.layers.Dropout(0.05)(dense1)

        dense2 = keras.layers.Dense(45,activation=keras.activations.relu)(drop1) #filter has 20, tanh
        drop2 = keras.layers.Dropout(0.05)(dense2) #0.1

        dense3 = keras.layers.Dense(45,activation=keras.activations.relu)(drop2) #filter has 20, tanh
        drop3 = keras.layers.Dropout(0.05)(dense3)

        dense4 = keras.layers.Dense(45,activation=keras.activations.relu)(drop3) #filter has 20, tanh
        drop4 = keras.layers.Dropout(0.05)(dense4)#0.1

        dense5 = keras.layers.Dense(NB_BINS,activation=keras.activations.softmax)(drop4) #filter has 2

        model = keras.Model(inputs=[IN],outputs=[dense5])
        return(model)
    
    Filter_model = Filter()
    mymodel = MLP()
    
    Filter_model.load_weights('%s/%s'%(djangoSettings.STATIC_ROOT,filter_filepath))
    
    Filtered = Filter_model.predict(X)
    p = 0.01 #results in 97percent true positives, 95percent true negatives...
    
    mymodel.load_weights('%s/%s'%(djangoSettings.STATIC_ROOT,model_filepath))
    
    posterior = mymodel.predict(X[Filtered[:,1] <= 0.01])
    
    point_estimates = np.sum(range_z*posterior,1)
    
    error=np.ones(len(point_estimates))
    for i in range(len(point_estimates)):
        error[i]=np.std(np.random.choice(a=range_z,size=1000,p=posterior[i,:],replace=True)) #this could be parallized i'm sure
    
    #now figure out which are filtered out, ez work
    all_my_indices = PhotoDF['#myindex'].values
    whats_left = all_my_indices[Filtered[:,1]<=0.01]

    out = {host_dictionary[value].pk: None for value in all_my_indices if value in host_dictionary}
    for i, value in enumerate(whats_left):
        out[host_dictionary[value].pk] = (float(point_estimates[i]), float(error[i]))
    return out


def candidate_hosts():
    """The cron's selection: hosts linked to a transient without ``photo_z_internal``."""
    return Host.objects.filter(transient__isnull=False, photo_z_internal__isnull=True).distinct()


def run_batch(hosts, ctx=None):
    """Enrichment ``run_batch`` for ``photoz-ml``: predict and store; one Outcome per returned host."""
    from YSE_App.services.enrichment import NO_MATCH, UPDATED, Outcome

    hosts = list(hosts)
    by_pk = {h.pk: h for h in hosts}
    results = predict_hosts(hosts)
    outcomes, to_update = {}, []
    for pk, value in results.items():
        host = by_pk.get(pk)
        if host is None:
            continue
        if value is None:
            outcomes[pk] = Outcome(NO_MATCH, "rejected by the quality filter")
            continue
        host.photo_z_internal, host.photo_z_err_internal = value
        to_update.append(host)
        outcomes[pk] = Outcome(UPDATED, "photo_z_internal %.4f +- %.4f" % value,
                               data={"photo_z_internal": value[0], "photo_z_err_internal": value[1]})
    if to_update:
        Host.objects.bulk_update(to_update, ['photo_z_internal', 'photo_z_err_internal'])
    return outcomes


class YSE(CronJobBase):

    RUN_EVERY_MINS = 30

    schedule = Schedule(run_every_mins=RUN_EVERY_MINS)
    code = 'YSE_App.data_ingest.Photo_Z.YSE'

    def do(self,user=None,password=None,search=1,path_to_model='YSE_DNN_photoZ_model_315.hdf5',debug=True):
        """Predict photometric redshifts of hosts without one (SDSS u,g,r,i,z -> ML model).

        Same selection and schedule as before #439; the work is :func:`run_batch`, run
        through the enrichment framework so each host's outcome is recorded.
        """
        try:
            nowdate = datetime.datetime.utcnow() - datetime.timedelta(1)
            print('Entered the photo_z cron')
            from YSE_App.services import enrichment
            hosts = list(candidate_hosts())
            if not hosts:
                print('photo_z cron: no hosts to do')
                return
            summary = enrichment.run(enrichment.get('photoz-ml'), hosts, dry_run=False)
            print('photo_z cron: %s' % summary.counts)
            print('time taken with upload:', datetime.datetime.utcnow() - nowdate)
        except Exception as e:
            exc_type, exc_obj, exc_tb = sys.exc_info()
            print("""Photo-z cron failed with error %s at line number %s"""%(e,exc_tb.tb_lineno))

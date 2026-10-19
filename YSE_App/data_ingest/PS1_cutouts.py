"""PS1 stack cutouts of host galaxies (cron ``PS1_cutouts.YSE``).

Ported to the enrichment framework (#439; ``manage.py enrich --service ps1-cutouts``). Same
schedule (30 minutes), same selection (transients with a host north of -31 deg), same files
(``STATIC_ROOT/cutouts/cutout_<name>_<band>.npy``) and the same HostPhotometry / HostPhotData /
HostImage rows, created by the ``admin`` account. It no longer downloads again:

* a host that already has GPC1 cutout images is not selected (another transient of the same
  host, or a cutout whose file was later moved, no longer adds duplicate rows);
* every attempt is recorded as a run of the ``ps1-cutouts`` service, and a cutout that failed
  is retried only after ``PS1_CUTOUT_RETRY_DAYS`` (30) and at most ``PS1_CUTOUT_MAX_ATTEMPTS``
  (3) times in a row, instead of every 30 minutes forever.
"""
from django_cron import CronJobBase, Schedule
from YSE_App.models.transient_models import *
from YSE_App.models import *

from django.conf import settings as djangoSettings
import datetime
import numpy as np
import os

import sys
from scipy import interpolate

from astropy.table import Table

from astropy.io import fits

import http
import urllib

from django.contrib.auth.models import User

BANDS = ['g','r','i','z','y']


def getimages(ra,dec,size=240,filters="grizy", type='stack'):

    """Query ps1filenames.py service to get a list of images

    ra, dec = position in degrees
    size = image size in pixels (0.25 arcsec/pixel)
    filters = string with filters to include
    Returns a table with the results
    """

    service = "https://ps1images.stsci.edu/cgi-bin/ps1filenames.py"
    url = ("{service}?ra={ra}&dec={dec}&size={size}&format=fits"
           "&filters={filters}&type={type}").format(**locals())
    table = Table.read(url, format='ascii')
    return table

def geturl(ra, dec, size=100, output_size=None, filters="grizy", format="jpg", color=False, type='stack'):

    """Get URL for images in the table

    ra, dec = position in degrees
    size = extracted image size in pixels (0.25 arcsec/pixel)
    output_size = output (display) image size in pixels (default = size).
                  output_size has no effect for fits format images.
    filters = string with filters to include
    format = data format (options are "jpg", "png" or "fits")
    color = if True, creates a color image (only for jpg or png format).
            Default is return a list of URLs for single-filter grayscale images.
    Returns a string with the URL
    """

    if color and format == "fits":
        raise ValueError("color images are available only for jpg or png formats")
    if format not in ("jpg","png","fits"):
        raise ValueError("format must be one of jpg, png, fits")
    table = getimages(ra,dec,size=size,filters=filters, type=type)
    print(table)
    url = ("https://ps1images.stsci.edu/cgi-bin/fitscut.cgi?"
           "ra={ra}&dec={dec}&size={size}&format={format}").format(**locals())
    if output_size:
        url = url + "&output_size={}".format(output_size)
    # sort filters from red to blue
    flist = ["yzirg".find(x) for x in table['filter']]
    table = table[np.argsort(flist)]
    if color:
        if len(table) > 3:
            # pick 3 filters
            table = table[[0,len(table)//2,len(table)-1]]
        for i, param in enumerate(["red","green","blue"]):
            url = url + "&{}={}".format(param,table['filename'][i])
    else:
        urlbase = url + "&red="
        url = []
        for filename in table['filename']:
            url.append(urlbase+filename)
    return url
    
def cutout_path(name, band):
    return djangoSettings.STATIC_ROOT+'/cutouts/cutout_{}_{}.npy'.format(name,band)


def hosts_with_cutouts():
    """Host ids that already have PS1 (GPC1) cutout images."""
    return HostImage.objects.filter(phot_data__photometry__instrument__name='GPC1').values(
        'phot_data__photometry__host_id')


def candidate_transients(missing_only=True):
    """The cron's selection (host north of -31 deg); ``missing_only`` drops hosts that have cutouts."""
    from django.db.models import Q
    qs = Transient.objects.filter(Q(host__isnull=False) & Q(host__dec__gt=-31))
    if missing_only:
        qs = qs.exclude(host_id__in=hosts_with_cutouts())
    return qs


class CutoutUnavailable(Exception):
    """PS1 has no usable cutout at this position (HTTP error, all-NaN image, nothing returned)."""


def fetch_cutout(ra, dec):
    """The (104, 104, 5) g,r,i,z,y stack cutout scaled as the image model expects; raises CutoutUnavailable."""
    image = np.zeros((104,104,5)) #holds the final image
    try:
        for i,F in enumerate(BANDS):
            fitsurl = geturl(ra=ra, dec=dec, size=104, filters=F, format="fits")
            if not len(fitsurl): continue
            fh = fits.open(fitsurl[0])
            image[:,:,i] = fh[0].data

        number_nan_pixels = np.sum(np.isnan(image).astype(int))
        if number_nan_pixels > 0:
            for j in range(5): #j is band information
                if np.any(np.isnan(image[:,:,j])):
                    backx = np.arange(0,image.shape[1])
                    backy = np.arange(0, image.shape[0])
                    backxx, backyy = np.meshgrid(backx, backy)
                    array = np.ma.masked_invalid(image[:,:,j])
                    x1 = backxx[np.logical_not(array.mask)]
                    y1 = backyy[np.logical_not(array.mask)]
                    newarr = array[np.logical_not(array.mask)]
                    try: image[:,:,j] = interpolate.griddata((x1, y1), newarr.ravel(), (backxx, backyy), method='cubic')
                    except Exception:
                        print('whole image is NaN, this cutout may never work?')
                        continue

        image = (image)/10000.0 #everything in the same range as SDSS
        image[image>1] = 1 #now max 1 so everything in range -1, 1
    except (urllib.error.HTTPError, http.client.IncompleteRead) as e:
        raise CutoutUnavailable('URL error: %s' % e)
    except Exception as e:
        raise CutoutUnavailable('%s: %s' % (type(e).__name__, e))
    if np.any(np.isnan(image)): #quick last check that after interpolation something hasn't gone terribly wrong
        raise CutoutUnavailable('NaN pixels after interpolation')
    if np.all(image == 0): #quick check that the try loop failed to weed out missing files so my datarray was never updated
        raise CutoutUnavailable('no PS1 image')
    return image


def save_cutouts(T, image, user):
    """Write the per-band .npy files and the HostPhotometry / HostPhotData / HostImage rows (as before)."""
    ID = T.name
    #now save the final processed image as a numpy array, except in each band because thats how the image model works.
    for i,F in enumerate(BANDS):
        np.save(cutout_path(ID,F),image[:,:,i]) #save where? This has to be permanent, b/c link

    h = T.host
    for i,F in enumerate(BANDS):
        hp = HostPhotometry(host=h,instrument=Instrument.objects.filter(name='GPC1')[0],obs_group=ObservationGroup.objects.filter(name='Pan-STARRS1')[0],created_by=user,modified_by=user)#,groups=???)#!!!
        hp.save()

        hpd = HostPhotData(photometry=hp,band=PhotometricBand.objects.filter(instrument__name='GPC1').filter(name=F)[0],created_by=user,modified_by=user,obs_date=datetime.datetime.now()) #!!! these are stack images, no one observing date...?
        hpd.save()

        hi = HostImage(phot_data=hpd,img_file=cutout_path(ID,F),created_by=user,modified_by=user)
        hi.save()

    h.save()
    T.save()


def cutout_user():
    return User.objects.get(username = 'admin') #!!!


def cutout_one(T, ctx=None, fetch=None):
    """Enrichment ``run_one`` for ``ps1-cutouts``: one transient's host cutout."""
    from YSE_App.services.enrichment import ERROR, SKIPPED, UPDATED, Outcome

    ctx = ctx or {}
    if not (T.host_id and T.host.ra and T.host.dec):
        return Outcome(SKIPPED, 'host has no position')
    if os.path.isfile(cutout_path(T.name,'y')):
        return Outcome(SKIPPED, 'cutout files exist')
    if HostImage.objects.filter(phot_data__photometry__host_id=T.host_id,
                                phot_data__photometry__instrument__name='GPC1').exists():
        return Outcome(SKIPPED, 'host already has PS1 cutouts')
    try:
        image = (fetch or fetch_cutout)(T.host.ra, T.host.dec)
    except CutoutUnavailable as e:
        print('keep track of my error: ', T.name, e)
        return Outcome(ERROR, error=str(e)[:500])
    save_cutouts(T, image, ctx.get('user') or cutout_user())
    return Outcome(UPDATED, 'saved g,r,i,z,y cutouts')


class YSE(CronJobBase):

    RUN_EVERY_MINS = 30

    schedule = Schedule(run_every_mins=RUN_EVERY_MINS)
    code = 'YSE_App.data_ingest.PS1_cutouts.YSE'

    def do(self,debug=True):

        try:
            print('Entered the PS cutout Cron')
            from YSE_App.services import enrichment
            service = enrichment.get('ps1-cutouts')
            transients = list(enrichment.select(service, missing_only=True))
            if not transients:
                print('PS1 cutout cron: nothing to do')
                return
            user = cutout_user()
            summary = enrichment.run(service, transients, dry_run=False, user=user, ctx={'user': user})
            print('PS1 cutout cron: %s' % summary.counts)
        except Exception as e:
            exc_type, exc_obj, exc_tb = sys.exc_info()
            print("""PS1 Host cutout cron failed with error %s at line number %s"""%(e,exc_tb.tb_lineno))

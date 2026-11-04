from YSE_App.models import *
from django.db.models import Q
from YSE_App.models import phot_models
from YSE_App.services.visibility import dataset_group_q

def GetAuthorizedTransientPhotometry_ByUser(user):
    allowed_phot = TransientPhotometry.objects.filter(dataset_group_q(TransientPhotometry, user)).distinct()

    return allowed_phot

def GetAuthorizedTransientPhotometry_ByUser_ByTransient(user, transient_id):
    return TransientPhotometry.objects.filter(
        dataset_group_q(TransientPhotometry, user),
        transient_id=transient_id,
    ).distinct()

def GetAuthorizedHostPhotometry_ByUser(user):
    allowed_phot = HostPhotometry.objects.filter(dataset_group_q(HostPhotometry, user)).distinct()

    return allowed_phot

def GetAuthorizedHostPhotometry_ByUser_ByHost(user, host_id):
    return HostPhotometry.objects.filter(
        dataset_group_q(HostPhotometry, user),
        host_id=host_id,
    ).distinct()

def GetAuthorizedTransientPhotData_ByUser(user, includeBadData=True):
    allowed_phot = GetAuthorizedTransientPhotometry_ByUser(user)
    phot_ids = allowed_phot.values('id')
    allowed_phot_data_query = Q(photometry__id__in=phot_ids)
    allowed_phot_data = _GetTransientPhotData(includeBadData, allowed_phot_data_query)

    return allowed_phot_data

def GetAuthorizedTransientPhotData_ByUser_ByTransient(user, transient_id, includeBadData=True):
    allowed_phot = GetAuthorizedTransientPhotometry_ByUser_ByTransient(user, transient_id)
    phot_ids = allowed_phot.values('id')
    allowed_phot_data_query = Q(photometry__id__in=phot_ids)
    allowed_phot_data = _GetTransientPhotData(includeBadData, allowed_phot_data_query)

    return allowed_phot_data

def GetAuthorizedHostPhotData_ByUser(user, includeBadData=True):
    allowed_phot = GetAuthorizedHostPhotometry_ByUser(user)
    phot_ids = allowed_phot.values('id')
    allowed_phot_data_query = Q(photometry__id__in=phot_ids)
    allowed_phot_data = _GetHostPhotData(includeBadData, allowed_phot_data_query)

    return allowed_phot_data

def GetAuthorizedHostPhotData_ByUser_ByHost(user, host_id, includeBadData=True):
    allowed_phot = GetAuthorizedHostPhotometry_ByUser_ByHost(user, host_id)
    phot_ids = allowed_phot.values('id')
    allowed_phot_data_query = Q(photometry__id__in=phot_ids)
    allowed_phot_data = _GetHostPhotData(includeBadData, allowed_phot_data_query)

    return allowed_phot_data

def GetAuthorizedTransientPhotData_ByPhotometry(user, photometry_id, includeBadData=True):
    # First check if they're allowed to access...
    allowed_phot = GetAuthorizedTransientPhotometry_ByUser(user).filter(pk=photometry_id)

    phot_data = None
    if allowed_phot:
        #... then get the data according to the dq flag preference
        query = Q(photometry__id=photometry_id)
        phot_data = _GetTransientPhotData(includeBadData, query)

    return phot_data

def GetAuthorizedHostPhotData_ByPhotometry(user, photometry_id, includeBadData=True):
    # First check if they're allowed to access...
    allowed_phot = GetAuthorizedHostPhotometry_ByUser(user).filter(photometry__id=photometry_id)

    phot_data = None
    if allowed_phot:
        # ... then get the data according to the dq flag preference
        query = Q(pk=photometry_id)
        phot_data = _GetHostPhotData(includeBadData, query)

    return phot_data


# This are private methods. Don't invoke them outside of this module!
def _GetTransientPhotData(includeBadData,filter):
    if includeBadData:
        return TransientPhotData.objects.filter(filter)
    else:
        return TransientPhotData.objects.exclude(data_quality__isnull=False).filter(filter)

def _GetHostPhotData(includeBadData, filter):
    if includeBadData:
        return HostPhotData.objects.filter(filter)
    else:
        return HostPhotData.objects.exclude(data_quality__isnull=False).filter(filter)

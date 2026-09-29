"""Django signals for YSE_App."""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.db.backends.signals import connection_created
from django.db.models.signals import m2m_changed, post_delete, post_save
from django.dispatch import receiver

from YSE_App.common.collaboration_groups import ensure_user_has_public_group
from YSE_App.common.db_time_cap import cap_explorer_connection
from YSE_App.models.phot_models import TransientPhotData
from YSE_App.services import photstat

User = get_user_model()

# Saved Explorer SQL runs under a statement time cap (#233).
connection_created.connect(cap_explorer_connection, dispatch_uid="yse_cap_explorer_connection")


@receiver(post_save, sender=User, dispatch_uid="yse_ensure_user_public_group")
def add_public_group_to_user(sender, instance, created, **kwargs):
    """Every user belongs to the Public collaboration group."""
    ensure_user_has_public_group(instance)


# ---- per-transient photometry statistics (#268) ----------------------------
# Every saved, deleted or re-flagged photometry point refreshes its transient's
# TransientPhotStat row. Ingest paths that write many points wrap their work in
# photstat.deferred_updates() so the recompute runs once per transient.


def _photdata_transient_id(instance):
    """transient_id without a query when the photometry object is already loaded."""
    photometry = instance._state.fields_cache.get('photometry')
    return getattr(photometry, 'transient_id', None)


@receiver(post_save, sender=TransientPhotData, dispatch_uid="yse_photstat_photdata_saved")
def refresh_photstat_on_photdata_save(sender, instance, **kwargs):
    if kwargs.get('raw'):
        return  # loaddata: the fixture may not be complete yet
    photstat.schedule_recompute(_photdata_transient_id(instance), photometry_id=instance.photometry_id)


@receiver(post_delete, sender=TransientPhotData, dispatch_uid="yse_photstat_photdata_deleted")
def refresh_photstat_on_photdata_delete(sender, instance, **kwargs):
    # create=False: during a Transient cascade the stat row must not be (re)created.
    photstat.schedule_recompute(
        _photdata_transient_id(instance), photometry_id=instance.photometry_id, create=False
    )


@receiver(m2m_changed, sender=TransientPhotData.data_quality.through, dispatch_uid="yse_photstat_dq_changed")
def refresh_photstat_on_quality_flag(sender, instance, action, reverse, **kwargs):
    """A data-quality flag turns a detection into an ignored point and back."""
    if reverse:
        # DataQuality.transientphotdata_set.add/remove/clear(...): many points at once.
        # clear() reports no pk_set, so remember the affected photometry before it runs.
        if action == 'pre_clear':
            instance._photstat_photometry_ids = list(
                instance.transientphotdata_set.values_list('photometry_id', flat=True).distinct()
            )
            return
        if action not in ('post_add', 'post_remove', 'post_clear'):
            return
        if action == 'post_clear':
            photometry_ids = getattr(instance, '_photstat_photometry_ids', [])
            instance._photstat_photometry_ids = []
        else:
            pk_set = kwargs.get('pk_set') or ()
            photometry_ids = TransientPhotData.objects.filter(pk__in=pk_set).values_list(
                'photometry_id', flat=True
            ).distinct()
        with photstat.deferred_updates():
            for pk in photometry_ids:
                photstat.schedule_recompute(photometry_id=pk)
        return
    if action not in ('post_add', 'post_remove', 'post_clear'):
        return
    photstat.schedule_recompute(_photdata_transient_id(instance), photometry_id=instance.photometry_id)

"""Django signals for YSE_App."""

from __future__ import annotations

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db.backends.signals import connection_created
from django.db.models.signals import m2m_changed, post_delete, post_save
from django.dispatch import receiver

from YSE_App.common.collaboration_groups import ensure_user_has_public_group
from YSE_App.common.db_time_cap import cap_explorer_connection

# Importing the module registers the external-service job handler (#265) in every process.
import YSE_App.services.external_services  # noqa: E402,F401
# ... and the facility runner + poll job (#298) so every process can execute facility submissions.
import YSE_App.services.facility_requests  # noqa: E402,F401
# ... and the sharing handlers (#326, #327): sharing.submit / poll / tns_retrieval / autopublish_sweep.
import YSE_App.sharing.handlers  # noqa: E402,F401
from YSE_App.models.transient_models import Transient
from YSE_App.sharing.autopublish import on_transient_saved
# ... and the analysis runner (#313) for in-process fits and webhook dispatch.
import YSE_App.analysis.runners  # noqa: E402,F401
# ... and the annotation checks (#318: Gaia DR3, WISE, quasar catalogue) as runners.
import YSE_App.annotation_services  # noqa: E402,F401
# ... the feed providers and feeds.* job handlers (#280).
import YSE_App.feeds  # noqa: E402,F401
from YSE_App.feeds.jobs import enqueue_screen
from YSE_App.services import annotations as annotations_svc
# ... and the AI summariser runner + summaries.refresh_stale job (#296).
import YSE_App.services.summaries  # noqa: E402,F401
from YSE_App.models.phot_models import TransientPhotData
from YSE_App.services import photstat
# Favorite-transient activity (#323): comments, audited transient changes, spectra, follow-ups.
from auditlog.models import LogEntry
from django.contrib.contenttypes.models import ContentType
from django.db.models.signals import pre_save
from YSE_App.models.followup_models import TransientFollowup
from YSE_App.models.log_models import Log
from YSE_App.models.spectra_models import TransientSpectrum
from YSE_App.services import favorites

User = get_user_model()

# Saved Explorer SQL runs under a statement time cap (#233).
connection_created.connect(cap_explorer_connection, dispatch_uid="yse_cap_explorer_connection")

# Auto-publisher rules (#325) look at every saved transient; a no-op unless a rule is enabled.
post_save.connect(on_transient_saved, sender=Transient, dispatch_uid="yse_sharing_autopublish")


@receiver(post_save, sender=Transient, dispatch_uid="yse_annotation_autorun")
def autorun_annotation_checks(sender, instance, created, **kwargs):
    """Queue the catalogue checks named in ANNOTATION_AUTORUN_SERVICES for a new transient (#318; off by default)."""
    if not created or kwargs.get('raw'):
        return
    if annotations_svc.autorun_slugs():
        annotations_svc.autorun_for_new_transient(instance)


@receiver(post_save, sender=Transient, dispatch_uid="yse_feeds_mpc_screen")
def screen_new_transient_for_minor_planets(sender, instance, created, **kwargs):
    """Queue the sb_ident minor-planet check for a new transient (#283; FEEDS_MPC_SCREEN_ON_CREATE, off by default)."""
    if not created or kwargs.get('raw'):
        return
    if getattr(settings, 'FEEDS_MPC_SCREEN_ON_CREATE', False):
        enqueue_screen(instance)


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


# ---- favorite-transient activity (#323) ------------------------------------
# Each handler is cheap when nobody starred the transient (one EXISTS query)
# and never raises into the save that triggered it (``favorites._safely``).


@receiver(post_save, sender=Log, dispatch_uid="yse_favorite_comment")
def favorite_activity_on_comment(sender, instance, created, **kwargs):
    if getattr(instance, '_favorites_defer', False):
        return  # create_transient_comment records it once the audience groups are set
    if created and instance.transient_id and not kwargs.get('raw'):
        favorites._safely(favorites.on_comment, instance)


@receiver(post_save, sender=LogEntry, dispatch_uid="yse_favorite_transient_changed")
def favorite_activity_on_transient_change(sender, instance, created, **kwargs):
    """Status / class / redshift changes come from the auditlog entry the Transient save writes."""
    if not created or kwargs.get('raw') or instance.action != LogEntry.Action.UPDATE:
        return
    if instance.content_type_id != ContentType.objects.get_for_model(Transient).pk:
        return
    changes = instance.changes_dict
    if not any(field in changes for field in favorites.WATCHED_TRANSIENT_FIELDS):
        return
    favorites._safely(favorites.on_transient_changed, instance.object_id, changes, actor=instance.actor)


@receiver(post_save, sender=TransientSpectrum, dispatch_uid="yse_favorite_spectrum")
def favorite_activity_on_spectrum(sender, instance, created, **kwargs):
    if created and not kwargs.get('raw'):
        favorites._safely(favorites.on_spectrum, instance)


@receiver(pre_save, sender=TransientFollowup, dispatch_uid="yse_favorite_followup_old_status")
def remember_followup_status(sender, instance, **kwargs):
    """Keep the stored status name so the post_save handler can tell a status change from another edit."""
    instance._favorite_old_status = ""
    if instance.pk and not kwargs.get('raw'):
        old = TransientFollowup.objects.filter(pk=instance.pk).values_list('status__name', flat=True).first()
        instance._favorite_old_status = old or ""


@receiver(post_save, sender=TransientFollowup, dispatch_uid="yse_favorite_followup")
def favorite_activity_on_followup(sender, instance, created, **kwargs):
    if kwargs.get('raw'):
        return
    favorites._safely(favorites.on_followup, instance, created=created,
                      old_status=getattr(instance, '_favorite_old_status', ''))

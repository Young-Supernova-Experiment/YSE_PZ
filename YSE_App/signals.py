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
from YSE_App.models.phot_models import HostPhotData, TransientPhotData, TransientPhotometry
from YSE_App.services import photstat
# Favorite-transient activity (#323): comments, audited transient changes, spectra, follow-ups.
from auditlog.models import LogEntry
from django.contrib.contenttypes.models import ContentType
from django.db.models.signals import pre_save
from YSE_App.models.followup_models import TransientFollowup
from YSE_App.models.log_models import Log
from YSE_App.models.spectra_models import HostSpectrum, TransientSpectrum
from YSE_App.common import quality_mask_sync
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


@receiver(post_save, sender=TransientFollowup, dispatch_uid="yse_followup_usage_accounting")
def account_followup_usage(sender, instance, created, **kwargs):
    """A follow-up reaching Successful charges its ToO / queued resource once; leaving it refunds (#304)."""
    if kwargs.get('raw') or not (instance.too_resource_id or instance.queued_resource_id):
        return
    from YSE_App.services.allocations import sync_followup_usage

    sync_followup_usage(instance)


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


# ---- quality_mask follows the DataQuality ManyToMany (#81) ------------------
# Every add / remove / clear on a data_quality relation recomputes the
# DataQuality-managed bits of the rows involved (quality_mask.merge_data_quality).
# These receivers are connected before the PhotStat one below, so a PhotStat
# recompute triggered by the same change already sees the new mask.
# Writes that bypass m2m_changed (bulk_create of through rows) set the mask
# themselves (data_utils._write_photdata_points); `backfill_quality_mask`
# finds anything that slipped through.


def _sync_quality_mask(model, instance, action, reverse, pk_set):
    if reverse:
        # DataQuality.<model>_set.add/remove/clear(...): many rows at once; clear()
        # reports no pk_set, so remember the affected rows before it runs.
        memo = '_quality_mask_%s_pks' % model._meta.model_name
        if action == 'pre_clear':
            setattr(instance, memo, list(
                getattr(instance, '%s_set' % model._meta.model_name).values_list('pk', flat=True)
            ))
            return
        if action == 'post_clear':
            pks = getattr(instance, memo, [])
            setattr(instance, memo, [])
        elif action in ('post_add', 'post_remove'):
            pks = pk_set or ()
        else:
            return
        quality_mask_sync.recompute(model, pks)
        return
    if action not in ('post_add', 'post_remove', 'post_clear') or instance.pk is None:
        return
    new = quality_mask_sync.recompute(model, [instance.pk]).get(instance.pk)
    if new is not None:
        # Keep the in-memory row current, so a later instance.save() writes the new mask.
        instance.quality_mask = new


def _connect_quality_mask_sync(model):
    def receiver_fn(sender, instance, action, reverse, **kwargs):
        _sync_quality_mask(model, instance, action, reverse, kwargs.get('pk_set'))

    m2m_changed.connect(
        receiver_fn, sender=model.data_quality.through, weak=False,
        dispatch_uid='yse_quality_mask_dq_%s' % model._meta.model_name,
    )


for _model in (TransientPhotData, HostPhotData, TransientSpectrum, HostSpectrum):
    _connect_quality_mask_sync(_model)


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


@receiver(m2m_changed, sender=TransientPhotometry.groups.through, dispatch_uid="yse_photstat_scope_groups_changed")
def refresh_photstat_scopes_on_group_change(sender, instance, action, reverse, **kwargs):
    """A photometry set's collaboration groups decide which visibility scopes count it (#607)."""
    if action not in ('post_add', 'post_remove', 'post_clear'):
        return
    if reverse:
        # Group.transientphotometry_set.add/remove/clear(...): many sets at once.
        if action == 'post_clear':
            return  # pk_set unknown; ``backfill_photstat_scopes`` repairs a cleared group
        with photstat.deferred_updates():
            for pk in list(kwargs.get('pk_set') or ()):
                photstat.schedule_recompute(photometry_id=pk)
        return
    photstat.schedule_recompute(instance.transient_id, photometry_id=instance.pk)


@receiver(pre_save, sender=TransientPhotometry, dispatch_uid="yse_photstat_photometry_moving")
def remember_photometry_transient(sender, instance, raw=False, update_fields=None, **kwargs):
    """Note the old transient when a photometry set is moved to another one (#269).

    ``QUB_data.CheckDuplicates`` (and an API update) re-point a
    ``TransientPhotometry`` at another transient; no point is saved, so the
    point signals above never run. One pk lookup per update of an existing row.
    """
    instance._photstat_old_transient_id = None
    if raw or instance.pk is None:
        return
    if update_fields is not None and not {'transient', 'transient_id'} & set(update_fields):
        return
    old = TransientPhotometry.objects.filter(pk=instance.pk).values_list('transient_id', flat=True).first()
    if old is not None and old != instance.transient_id:
        instance._photstat_old_transient_id = old


@receiver(post_save, sender=TransientPhotometry, dispatch_uid="yse_photstat_photometry_moved")
def refresh_photstat_on_photometry_move(sender, instance, created, raw=False, **kwargs):
    """Both transients' stat rows follow a moved photometry set (#269)."""
    old = getattr(instance, '_photstat_old_transient_id', None)
    if raw or created or old is None:
        return
    instance._photstat_old_transient_id = None
    # The old transient may be deleted right after (duplicate clean-up): update, never create.
    photstat.schedule_recompute(old, create=False)
    photstat.schedule_recompute(instance.transient_id)


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


# ---- group profiles (#445, #440) -------------------------------------------
# Every new auth Group gets a GroupProfile (active, no owners); system groups by
# name (settings.YSE_SYSTEM_GROUP_NAMES) are marked is_system.

from django.contrib.auth.models import Group as _AuthGroup  # noqa: E402


@receiver(post_save, sender=_AuthGroup, dispatch_uid="yse_group_profile_on_create")
def create_group_profile(sender, instance, created, **kwargs):
    if not created or kwargs.get('raw'):
        return
    from YSE_App.services.groups import profile_for

    profile_for(instance)


# ---- AstroScheduler execution feedback (#674) -------------------------------
# New data prove the observation: the matching data-taken observation becomes a success and
# its classical request Successful. Never fails the upload (errors are only logged).

def _data_uploaded(instance, obs_date):
    import logging

    from YSE_App.services.observations import data_uploaded

    try:
        data_uploaded(instance.transient_id, instance.followup_id, obs_date,
                      getattr(instance, "created_by", None))
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).exception("observation success from uploaded data failed")


@receiver(post_save, sender=TransientSpectrum, dispatch_uid="yse_observation_success_spectrum")
def observation_success_on_spectrum(sender, instance, created, **kwargs):
    if created and not kwargs.get('raw'):
        _data_uploaded(instance, instance.obs_date)


@receiver(post_save, sender=TransientPhotometry, dispatch_uid="yse_observation_success_photometry")
def observation_success_on_photometry(sender, instance, created, **kwargs):
    # A photometry set has no date of its own: only an upload naming its request counts.
    if created and not kwargs.get('raw') and instance.followup_id:
        _data_uploaded(instance, None)


# ---- Access history for read-time rules (#648, PR 1b) ------------------------
# Dated group membership and the time each group was added to a dataset's own
# groups, recorded whether or not DATA_ACCESS_GRANTS_ENABLED is on
# (services.access_history). A failure is logged and never blocks the change.

def _record_access_history(fn, *args):
    import logging

    from django.db import transaction

    try:
        with transaction.atomic():  # a savepoint: a failure never breaks the caller's transaction
            fn(*args)
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).exception("access history not recorded")


@receiver(m2m_changed, sender=get_user_model().groups.through, dispatch_uid="yse_access_history_membership")
def record_group_membership(sender, instance, action, reverse, **kwargs):
    from YSE_App.services import access_history, access_rules

    _record_access_history(access_history.record_membership_change, instance, action, reverse,
                           kwargs.get("pk_set"))
    if action in ("post_add", "post_remove", "post_clear") and access_rules.enabled():
        # Which group rules apply to a user, and which groups they left, changed (#648, PR 2).
        _record_access_history(access_rules.bump_version)


@receiver(m2m_changed, sender=TransientPhotometry.groups.through, dispatch_uid="yse_access_history_phot_groups")
def record_photometry_group_share(sender, instance, action, reverse, **kwargs):
    from YSE_App.services import access_history

    _record_access_history(access_history.record_dataset_share_change, TransientPhotometry, instance, action,
                           reverse, kwargs.get("pk_set"))


@receiver(m2m_changed, sender=TransientSpectrum.groups.through, dispatch_uid="yse_access_history_spec_groups")
def record_spectrum_group_share(sender, instance, action, reverse, **kwargs):
    from YSE_App.services import access_history

    _record_access_history(access_history.record_dataset_share_change, TransientSpectrum, instance, action,
                           reverse, kwargs.get("pk_set"))


# ---- Per-point access rules (#648, PR 2) ---------------------------------------
# Any rule or standing grant written outside an action (the admin, a shell) bumps
# the rules version, so cached rule summaries and plots are rebuilt; actions bump
# once themselves (access_actions.signals_quiet). New points and spectra of a
# program get the program's standing grants at ingest.

from YSE_App.models.data_access_grant_models import DataAccessRule, StandingAccessGrant  # noqa: E402


@receiver(post_save, sender=DataAccessRule, dispatch_uid="yse_access_rule_saved")
def access_rule_saved(sender, instance, **kwargs):
    from YSE_App.services import access_actions, access_rules, photstat

    if access_actions.signals_quiet() or kwargs.get("raw"):
        return
    access_rules.bump_version()
    if instance.grantee_group_id and instance.transient_phot_data_id and access_rules.enabled():
        photstat.invalidate_scopes([(instance.transient_id, instance.grantee_group_id)])


@receiver(post_delete, sender=DataAccessRule, dispatch_uid="yse_access_rule_deleted")
def access_rule_deleted(sender, instance, **kwargs):
    # Version only: a point's deletion recomputes its transient's PhotStat scopes already, and a staff
    # removal (access_actions.delete_rule, the admin) invalidates the scopes itself.
    from YSE_App.services import access_actions, access_rules

    if not access_actions.signals_quiet():
        _record_access_history(access_rules.bump_version)


@receiver(post_save, sender=StandingAccessGrant, dispatch_uid="yse_standing_grant_saved")
def standing_grant_saved(sender, instance, **kwargs):
    from YSE_App.services import access_rules

    if not kwargs.get("raw"):
        access_rules.bump_version()


def _apply_standing_grants(fn, instance):
    import logging

    from django.db import transaction

    try:
        with transaction.atomic():
            fn(instance)
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).exception("standing access grants not applied")


@receiver(post_save, sender=TransientPhotData, dispatch_uid="yse_standing_grants_point")
def standing_grants_on_point(sender, instance, created, **kwargs):
    if not created or kwargs.get("raw"):
        return
    photometry = instance._state.fields_cache.get("photometry")
    if photometry is not None and not photometry.allocation_id:
        return  # the usual case: no program link, no query
    from YSE_App.services import access_actions

    _apply_standing_grants(access_actions.on_point_created, instance)


@receiver(post_save, sender=TransientSpectrum, dispatch_uid="yse_standing_grants_spectrum")
def standing_grants_on_spectrum(sender, instance, created, **kwargs):
    if created and not kwargs.get("raw") and instance.allocation_id:
        from YSE_App.services import access_actions

        _apply_standing_grants(access_actions.apply_standing_grants_to_spectrum, instance)


# ---- Program links at ingest (#648, PR 3) -------------------------------------
# A new photometry set or spectrum with no program gets the one allocation its follow-up
# (or a matching AstroScheduler observation) points to; nothing when none or several
# (services.program_links). No query without a follow-up unless the planner is on.

def _link_new_dataset(instance):
    import logging

    from django.db import transaction

    from YSE_App.services import program_links

    try:
        with transaction.atomic():
            program_links.link_at_ingest(instance)
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).exception("program link at ingest failed")


def _program_link_possible(instance) -> bool:
    if instance.allocation_id:
        return False
    if instance.followup_id:
        return True
    from YSE_App.services.night_planner import enabled

    return isinstance(instance, TransientSpectrum) and instance.obs_date is not None and enabled()


@receiver(post_save, sender=TransientPhotometry, dispatch_uid="yse_program_link_photometry")
def program_link_on_photometry(sender, instance, created, **kwargs):
    if created and not kwargs.get("raw") and _program_link_possible(instance):
        _link_new_dataset(instance)


@receiver(post_save, sender=TransientSpectrum, dispatch_uid="yse_program_link_spectrum")
def program_link_on_spectrum(sender, instance, created, **kwargs):
    if created and not kwargs.get("raw") and _program_link_possible(instance):
        _link_new_dataset(instance)

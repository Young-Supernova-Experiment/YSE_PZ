"""Alerts on Selections (#844): who may set them, what they find, and how they are delivered.

Rules (Ryan, 2026-10-25):

* **Triggers:** a new match, a new spectrum, a class change, and/or the latest detection
  crossing a magnitude (brighter than, or fading below, ``mag_threshold``; any band).
* **Recipients:** anyone may alert **themselves** (``target='me'``). Only the team's owners
  (``GroupProfile.owners``) may set an alert for a whole team (``'team'``: each member, owner
  and deputy, through their own notification preferences) or its Slack channel (``'slack'``).
  :func:`check_target` enforces this when an alert is saved *and* on every run, so an alert stops
  when its owner stops owning the team.
* **Frequency:** immediately (every pass), or an hourly or daily digest (at most one message per
  hour or day per alert and recipient). Pending digest events wait in the snapshot.
* **Delivery:** through ``YSE_App.services.notify.notify`` (kind ``selection_alert``, preference
  group "Alerts"), so the recipient's in-app / email / Slack switches apply. No external address
  is ever added: every recipient is a YSE-PZ account (or the team's Slack channel, through the
  site's Slack app on the job queue).
* **Duplicates:** an object matching several alerts of the same recipient in one pass is sent
  once, in one message per recipient, listing every alert it matched.
* **Access:** a Selection is evaluated **as the recipient** (``selection_queryset``), and every
  spectrum, photometry point and class is checked per point for that recipient. A Slack channel
  has no single reader: it gets only what *every* member of the team may see. Before an event is
  sent it is checked again (the object must still match, the spectrum or point still be visible).

The pass (:func:`run_alerts`, job kind ``selection_alerts.run``, queued by the
``SelectionAlerts`` cron) does nothing unless ``SELECTION_ALERTS_ENABLED`` is on. It is
idempotent: a second pass right after the first finds no difference and sends nothing; each
alert's comparison and each recipient's delivery runs in a transaction with the rows locked, so
two runners never send the same event twice. The first pass after an alert is created only takes
the baseline (nothing already matching is announced).

:func:`estimate_volume` counts what an alert would have sent in the last 30 days, shown before
an alert is saved.
"""

from __future__ import annotations

import datetime
import logging
import re
from collections import OrderedDict, defaultdict

from django.conf import settings
from django.contrib.auth.models import Group, User
from django.db import transaction
from django.db.models import Count, Max, OuterRef, Subquery
from django.utils import timezone

from YSE_App.jobs import JobRetry, enqueue, job
from YSE_App.models import Selection, SelectionAlert, SelectionAlertSnapshot
from YSE_App.services import selections as selection_svc

logger = logging.getLogger(__name__)

__all__ = [
    'AlertError',
    'AlertAccessError',
    'NOTIFICATION_KIND',
    'RUN_KIND',
    'SLACK_KIND',
    'alerts_enabled',
    'can_alert_team',
    'check_target',
    'create_alert',
    'delete_alert',
    'estimate_volume',
    'owned_teams',
    'run_alerts',
    'update_alert',
    'user_alerts',
    'validate_alert',
]

RUN_KIND = 'selection_alerts.run'
SLACK_KIND = 'selection_alerts.slack'
NOTIFICATION_KIND = 'selection_alert'

NEW_MATCH = 'new_match'
NEW_SPECTRUM = 'new_spectrum'
CLASS_CHANGE = 'class_change'
MAG_CROSS = 'mag_cross'
EVENT_LABELS = OrderedDict((
    (NEW_MATCH, 'new match'),
    (NEW_SPECTRUM, 'new spectrum'),
    (CLASS_CHANGE, 'class change'),
    (MAG_CROSS, 'magnitude crossing'),
))

#: Class value in a snapshot when the recipient may not see the object's data.
HIDDEN = 'h'
#: Events kept per snapshot while a digest waits; older ones beyond this are dropped (counted).
MAX_PENDING = 500
#: Objects listed in one message; the rest are counted ("and N more").
MAX_LISTED = 50
ESTIMATE_DAYS = 30
SLACK_CHANNEL_RE = re.compile(r'^[CG][A-Z0-9]{6,20}$')
MAG_LIMITS = (-5.0, 35.0)
DIGEST_PERIODS = {
    SelectionAlert.FREQUENCY_IMMEDIATE: datetime.timedelta(0),
    SelectionAlert.FREQUENCY_HOURLY: datetime.timedelta(hours=1),
    SelectionAlert.FREQUENCY_DAILY: datetime.timedelta(days=1),
}
#: A pass that starts a little early (cron jitter) still delivers the digest.
DIGEST_TOLERANCE = datetime.timedelta(minutes=2)


class AlertError(ValueError):
    """An alert could not be saved; ``str(exc)`` is user-facing."""


class AlertAccessError(AlertError):
    """The user may not set (or change) this alert."""


class TooManyMatches(AlertError):
    """The Selection has more matches than an alert follows."""


def alerts_enabled():
    return bool(getattr(settings, 'SELECTION_ALERTS_ENABLED', False))


def _max_matches():
    try:
        return max(1, int(getattr(settings, 'SELECTION_ALERTS_MAX_MATCHES', 2000)))
    except (TypeError, ValueError):
        return 2000


def _authenticated(user):
    return user is not None and getattr(user, 'is_authenticated', False)


# --------------------------------------------------------------------------- who may alert whom

def can_alert_team(user, team):
    """True when ``user`` is one of ``team``'s owners (``GroupProfile.owners``; deputies and staff are not)."""
    if not _authenticated(user) or team is None:
        return False
    from YSE_App.models.group_models import GroupProfile

    return GroupProfile.objects.filter(group=team, owners=user).exists()


def owned_teams(user):
    """Teams ``user`` owns (the teams an alert may notify), by name."""
    if not _authenticated(user):
        return []
    return list(Group.objects.filter(group_profile__owners=user).order_by('name').distinct())


def team_members(team):
    """Active members, owners and deputies of ``team``, by id."""
    if team is None:
        return []
    ids = set(team.user_set.values_list('pk', flat=True))
    from YSE_App.models.group_models import GroupProfile

    profile = GroupProfile.objects.filter(group=team).first()
    if profile is not None:
        ids |= set(profile.owners.values_list('pk', flat=True))
        ids |= set(profile.deputies.values_list('pk', flat=True))
    return list(User.objects.filter(pk__in=ids, is_active=True).order_by('pk'))


def check_target(alert):
    """Raise :class:`AlertAccessError` unless ``alert``'s owner may notify its target."""
    if alert.target == SelectionAlert.TARGET_ME:
        return
    if alert.target not in (SelectionAlert.TARGET_TEAM, SelectionAlert.TARGET_SLACK):
        raise AlertError('Choose who gets the alert.')
    if alert.team_id is None:
        raise AlertError('Choose the team to alert.')
    if not can_alert_team(alert.owner, alert.team):
        raise AlertAccessError("Only the team's owners can set an alert for the whole team or its Slack channel.")
    if alert.target == SelectionAlert.TARGET_SLACK and not SLACK_CHANNEL_RE.match(alert.slack_channel or ''):
        raise AlertError('Give the Slack channel id (it starts with C or G, for example C0123ABCD).')


def user_alerts(user):
    """The alerts ``user`` set, with their Selections."""
    if not _authenticated(user):
        return SelectionAlert.objects.none()
    return SelectionAlert.objects.filter(owner=user).select_related('selection', 'team').order_by(
        'selection__name', 'pk')


def can_change(user, alert):
    return _authenticated(user) and alert.owner_id == user.pk


# --------------------------------------------------------------------------- create / update / delete

_UNSET = object()
FIELDS = ('label', 'target', 'team', 'slack_channel', 'on_new_match', 'on_new_spectrum', 'on_class_change',
          'mag_threshold', 'mag_direction', 'frequency', 'enabled')


def _clean_team(team):
    if isinstance(team, Group) or team is None:
        return team
    text = str(team).strip()
    if not text:
        return None
    if not text.isdigit():
        raise AlertError('That team does not exist.')
    found = Group.objects.filter(pk=int(text)).first()
    if found is None:
        raise AlertError('That team does not exist.')
    return found


def _clean_mag(value):
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        mag = float(value)
    except (TypeError, ValueError):
        raise AlertError('The magnitude must be a number.')
    if not (MAG_LIMITS[0] <= mag <= MAG_LIMITS[1]):
        raise AlertError('The magnitude must be between %g and %g.' % MAG_LIMITS)
    return mag


def _apply(alert, values):
    for key, value in values.items():
        if value is _UNSET:
            continue
        if key == 'team':
            alert.team = _clean_team(value)
        elif key == 'mag_threshold':
            alert.mag_threshold = _clean_mag(value)
        elif key == 'label':
            alert.label = ' '.join(str(value or '').split())[:120]
        elif key == 'slack_channel':
            alert.slack_channel = str(value or '').strip().upper()[:32]
        elif key in ('on_new_match', 'on_new_spectrum', 'on_class_change', 'enabled'):
            setattr(alert, key, bool(value))
        else:
            setattr(alert, key, value)
    if alert.target not in dict(SelectionAlert.TARGET_CHOICES):
        raise AlertError('Choose who gets the alert.')
    if alert.frequency not in dict(SelectionAlert.FREQUENCY_CHOICES):
        raise AlertError('Choose how often the alert is sent.')
    if alert.mag_direction not in dict(SelectionAlert.MAG_DIRECTION_CHOICES):
        raise AlertError('Choose brighter or fainter.')
    if not (alert.on_new_match or alert.on_new_spectrum or alert.on_class_change or alert.mag_threshold is not None):
        raise AlertError('Choose at least one thing to be alerted about.')
    if alert.target == SelectionAlert.TARGET_ME:
        alert.team, alert.slack_channel = None, ''
    elif alert.target == SelectionAlert.TARGET_TEAM:
        alert.slack_channel = ''
    check_target(alert)


def validate_alert(user, selection, alert=None, **values):
    """Run every check :func:`create_alert` (or, with ``alert``, :func:`update_alert`) runs, without saving."""
    unknown = set(values) - set(FIELDS)
    if unknown:
        raise TypeError('unknown alert field(s): %s' % ', '.join(sorted(unknown)))
    if alert is None:
        if not _authenticated(user):
            raise AlertAccessError('Log in to set an alert.')
        if not selection_svc.can_view(user, selection):
            raise AlertAccessError('You cannot see this Selection.')
        probe = SelectionAlert(selection=selection, owner=user)
    else:
        if not can_change(user, alert):
            raise AlertAccessError('Only the person who set this alert can change it.')
        # A fresh instance (copy.copy would share the related-object cache with ``alert``).
        probe = SelectionAlert(**{f.attname: getattr(alert, f.attname) for f in SelectionAlert._meta.concrete_fields})
    _apply(probe, values)
    return probe


def create_alert(user, selection, **values):
    """Save a new alert by ``user`` on ``selection``; raises :class:`AlertError` / :class:`AlertAccessError`.

    Keywords: ``label``, ``target`` (me / team / slack), ``team`` (a ``Group`` or its id),
    ``slack_channel``, ``on_new_match``, ``on_new_spectrum``, ``on_class_change``,
    ``mag_threshold``, ``mag_direction``, ``frequency``.
    """
    alert = validate_alert(user, selection, **values)
    alert.save()
    return alert


def _baseline_key(alert):
    return (alert.target, alert.team_id, alert.slack_channel, alert.on_new_spectrum, alert.on_class_change,
            alert.mag_threshold, alert.mag_direction)


def update_alert(user, alert, **values):
    """Change ``alert`` (its owner only); the target rules are checked again."""
    if not can_change(user, alert):
        raise AlertAccessError('Only the person who set this alert can change it.')
    unknown = set(values) - set(FIELDS)
    if unknown:
        raise TypeError('unknown alert field(s): %s' % ', '.join(sorted(unknown)))
    before = _baseline_key(alert)
    _apply(alert, values)
    alert.last_error = ''
    with transaction.atomic():
        alert.save()
        if _baseline_key(alert) != before:
            # New recipients or triggers start from a fresh baseline (an old snapshot does not hold
            # the values a newly chosen trigger compares, so it would announce everything).
            alert.snapshots.all().delete()
    return alert


def delete_alert(user, alert):
    if not can_change(user, alert):
        raise AlertAccessError('Only the person who set this alert can delete it.')
    alert.delete()


# --------------------------------------------------------------------------- evaluation, per viewer

def _visible_spectra(viewers, transient_ids):
    """``TransientSpectrum`` rows of ``transient_ids`` that every one of ``viewers`` may see."""
    from YSE_App.models import TransientSpectrum
    from YSE_App.services.visibility import spectrum_visible_q, user_sees_everything

    qs = TransientSpectrum.objects.filter(transient_id__in=transient_ids)
    for viewer in viewers:
        if not user_sees_everything(viewer):
            qs = qs.filter(pk__in=TransientSpectrum.objects.filter(spectrum_visible_q(viewer)).values('pk'))
    return qs


def _visible_detections(viewers, transient_ids=None):
    """Good-quality detections (``mag`` and ``mag_err`` set) that every one of ``viewers`` may see, per point."""
    from YSE_App.common.quality_mask import legacy_good
    from YSE_App.models import TransientPhotData
    from YSE_App.services.visibility import visible_photometry_ids_q

    qs = TransientPhotData.objects.filter(mag__isnull=False, mag_err__isnull=False).filter(
        legacy_good('TransientPhotData'))
    if transient_ids is not None:
        qs = qs.filter(photometry__transient_id__in=transient_ids)
    for viewer in viewers:
        condition = visible_photometry_ids_q(viewer)
        if condition is not None:
            qs = qs.filter(condition)
    return qs


def _data_visible_ids(viewers, transient_ids):
    """Ids among ``transient_ids`` whose data (and so their class) every viewer may see."""
    from YSE_App.models import Transient
    from YSE_App.services.visibility import filter_transients_by_data_access

    qs = Transient.objects.filter(pk__in=transient_ids)
    for viewer in viewers:
        qs = filter_transients_by_data_access(viewer, qs)
    return set(qs.values_list('pk', flat=True))


def matching_ids(selection, viewers):
    """Ids of the transients ``selection`` selects for *every* viewer (raises ``SelectionError``)."""
    limit = _max_matches()
    common = None
    for viewer in viewers:
        ids = list(selection_svc.selection_queryset(selection, viewer).values_list('pk', flat=True)[:limit + 1])
        if len(ids) > limit:
            raise TooManyMatches('This Selection has more than %d matches; alerts follow at most %d. Narrow it first.'
                                 % (limit, limit))
        common = set(ids) if common is None else common & set(ids)
        if not common:
            return set()
    return common or set()


def current_state(alert, viewers):
    """``({tid: [class, newest spectrum id, latest point id, latest mag]}, {tid: (name, slug)})``.

    Every value is what all of ``viewers`` may see: the class only where they see the object's
    data (else ``HIDDEN``), spectra and points per point. Parts the alert does not use are None.
    """
    from YSE_App.models import Transient

    ids = matching_ids(alert.selection, viewers)
    if not ids:
        return {}, {}
    rows = list(Transient.objects.filter(pk__in=ids).values_list('pk', 'name', 'slug', 'best_spec_class_id'))
    names = {pk: (name, slug) for pk, name, slug, _cls in rows}
    data_ids = _data_visible_ids(viewers, ids) if alert.on_class_change else set()
    spectra = {}
    if alert.on_new_spectrum:
        spectra = {row['transient_id']: row['newest'] for row in
                   _visible_spectra(viewers, ids).order_by().values('transient_id')
                   .annotate(newest=Max('pk'), n=Count('pk'))}
    latest = {}
    if alert.mag_threshold is not None:
        points = _visible_detections(viewers)
        newest = Transient.objects.filter(pk__in=ids).annotate(point=Subquery(
            points.filter(photometry__transient_id=OuterRef('pk')).order_by('-obs_date', '-pk').values('pk')[:1]
        )).values_list('pk', 'point')
        point_ids = {tid: pid for tid, pid in newest if pid}
        from YSE_App.models import TransientPhotData

        mags = dict(TransientPhotData.objects.filter(pk__in=point_ids.values()).values_list('pk', 'mag'))
        latest = {tid: (pid, mags.get(pid)) for tid, pid in point_ids.items()}
    state = {}
    for pk, _name, _slug, cls in rows:
        klass = None
        if alert.on_class_change:
            klass = cls if pk in data_ids else HIDDEN
        point, mag = latest.get(pk, (None, None))
        state[pk] = [klass, spectra.get(pk), point, mag]
    return state, names


def _crossed(alert, before, after):
    if after is None or alert.mag_threshold is None:
        return False
    threshold = alert.mag_threshold
    if alert.mag_direction == SelectionAlert.FAINTER:
        return before is not None and before < threshold <= after
    return (before is None or before > threshold) and after <= threshold


def _class_names(ids):
    from YSE_App.models import TransientClass

    ids = {i for i in ids if isinstance(i, int)}
    return dict(TransientClass.objects.filter(pk__in=ids).values_list('pk', 'name')) if ids else {}


def diff_events(alert, previous, current, names, viewers, now):
    """The events between two states of one recipient (``previous`` from the snapshot, string keys)."""
    previous = {int(k): v for k, v in (previous or {}).items()}
    events = []
    at = now.isoformat()
    changed_classes = []
    new_spectra_for = {}
    for tid in sorted(current):
        name, slug = names.get(tid, ('', ''))
        base = {'t': tid, 'n': name, 's': slug, 'a': alert.pk, 'at': at}
        cur = current[tid]
        if tid not in previous:
            if alert.on_new_match:
                events.append(dict(base, k=NEW_MATCH, d='now matches'))
            continue
        old = list(previous[tid]) + [None] * (4 - len(previous[tid]))
        if alert.on_class_change and HIDDEN not in (old[0], cur[0]) and cur[0] is not None and old[0] != cur[0]:
            changed_classes.append((base, old[0], cur[0]))
        if alert.on_new_spectrum and cur[1] and (old[1] or 0) < cur[1]:
            new_spectra_for[tid] = (base, old[1] or 0)
        if alert.mag_threshold is not None and cur[2] and cur[2] != old[2] and _crossed(alert, old[3], cur[3]):
            word = 'brighter than' if alert.mag_direction == SelectionAlert.BRIGHTER else 'fainter than'
            events.append(dict(base, k=MAG_CROSS, d='latest detection %.2f mag, %s %g' % (cur[3], word,
                                                                                            alert.mag_threshold),
                               r=[cur[2]]))
    if changed_classes:
        labels = _class_names([c for _b, o, n in changed_classes for c in (o, n)])
        for base, old, new in changed_classes:
            events.append(dict(base, k=CLASS_CHANGE, d='class %s (was %s)' % (
                labels.get(new, 'unknown'), labels.get(old, 'unclassified') if old else 'unclassified')))
    if new_spectra_for:
        spectra = _visible_spectra(viewers, list(new_spectra_for)).values_list('transient_id', 'pk')
        found = defaultdict(list)
        for tid, pk in spectra:
            if pk > new_spectra_for[tid][1]:
                found[tid].append(pk)
        for tid, pks in sorted(found.items()):
            base = new_spectra_for[tid][0]
            events.append(dict(base, k=NEW_SPECTRUM,
                               d='%d new spectrum' % len(pks) if len(pks) == 1 else '%d new spectra' % len(pks),
                               r=sorted(pks)))
    return events


def still_visible(event, viewers):
    """The spectra or point an event names are still visible to every viewer."""
    refs = event.get('r') or []
    if not refs:
        return True
    if event.get('k') == NEW_SPECTRUM:
        return _visible_spectra(viewers, [event['t']]).filter(pk__in=refs).exists()
    if event.get('k') == MAG_CROSS:
        return _visible_detections(viewers, [event['t']]).filter(pk__in=refs).exists()
    return True


# --------------------------------------------------------------------------- recipients

class Recipient:
    """One delivery target: a user (``user``) or a Slack channel (``channel``), with the viewers to evaluate as."""

    def __init__(self, user=None, channel='', viewers=()):
        self.user = user
        self.channel = channel
        self.viewers = list(viewers)

    @property
    def key(self):
        return 'user:%d' % self.user.pk if self.user is not None else 'slack:%s' % self.channel

    @property
    def user_id(self):
        return self.user.pk if self.user is not None else None


def recipients_for(alert):
    """Who ``alert`` notifies now (raises :class:`AlertAccessError` when its owner may no longer)."""
    check_target(alert)
    if alert.target == SelectionAlert.TARGET_ME:
        return [Recipient(user=alert.owner, viewers=[alert.owner])] if alert.owner.is_active else []
    members = team_members(alert.team)
    if alert.target == SelectionAlert.TARGET_TEAM:
        return [Recipient(user=member, viewers=[member]) for member in members]
    return [Recipient(channel=alert.slack_channel, viewers=members)] if members else []


def _due(alert, snapshot, now):
    period = DIGEST_PERIODS.get(alert.frequency, DIGEST_PERIODS[SelectionAlert.FREQUENCY_DAILY])
    if not period:
        return True
    last = snapshot.last_delivered_at or snapshot.baseline_at
    return last is None or now - last >= period - DIGEST_TOLERANCE


# --------------------------------------------------------------------------- the pass

class PassResult:
    def __init__(self):
        self.alerts = 0
        self.baselines = 0
        self.events = 0
        self.messages = 0
        self.errors = 0

    def as_dict(self):
        return {'alerts': self.alerts, 'baselines': self.baselines, 'events': self.events,
                'messages': self.messages, 'errors': self.errors}


def _record_error(alert, message):
    SelectionAlert.objects.filter(pk=alert.pk).update(last_error=str(message)[:500])


def _evaluate_alert(alert, now, result, deliveries):
    """Compare ``alert`` with its snapshots, store the new state and queue due events in ``deliveries``."""
    try:
        recipients = recipients_for(alert)
    except AlertError as exc:
        _record_error(alert, exc)
        result.errors += 1
        return
    errors = []
    with transaction.atomic():
        list(SelectionAlert.objects.select_for_update().filter(pk=alert.pk).order_by('pk').values_list('pk'))
        snapshots = {s.recipient_id: s for s in
                     SelectionAlertSnapshot.objects.select_for_update().filter(alert=alert).order_by('pk')}
        wanted = {r.user_id for r in recipients}
        stale = [s.pk for rid, s in snapshots.items() if rid not in wanted]
        if stale:
            SelectionAlertSnapshot.objects.filter(pk__in=stale).delete()
        for recipient in recipients:
            try:
                state, names = current_state(alert, recipient.viewers)
            except selection_svc.SelectionAccessError:
                continue  # this recipient may not see the Selection: nothing for them
            except (selection_svc.SelectionError, AlertError) as exc:
                errors.append(str(exc))
                continue
            snapshot = snapshots.get(recipient.user_id)
            stored = {str(k): v for k, v in state.items()}
            if snapshot is None or snapshot.baseline_at is None:
                snapshot = snapshot or SelectionAlertSnapshot(alert=alert, recipient=recipient.user)
                snapshot.state, snapshot.pending = stored, []
                snapshot.baseline_at = snapshot.last_delivered_at = now
                snapshot.save()
                result.baselines += 1
                continue
            events = diff_events(alert, snapshot.state, state, names, recipient.viewers, now)
            pending = list(snapshot.pending or []) + events
            if len(pending) > MAX_PENDING:
                pending = pending[-MAX_PENDING:]
            snapshot.state, snapshot.pending = stored, pending
            snapshot.save()
            result.events += len(events)
            if pending and _due(alert, snapshot, now):
                deliveries[recipient.key].append((alert, recipient, snapshot.pk, set(state)))
        SelectionAlert.objects.filter(pk=alert.pk).update(last_run_at=now, last_error='; '.join(errors)[:500])
    if errors:
        result.errors += 1


def _message(recipient, merged, extra):
    """``(subject, text, html)`` of one message: each object once, with every alert it matched."""
    from django.utils.html import escape

    from YSE_App.services.notify import absolute_url

    count = len(merged)
    subject = 'Selection alerts: %d object%s' % (count, '' if count == 1 else 's')
    lines, items = [], []
    for tid, entry in list(merged.items())[:MAX_LISTED]:
        url = '/transient_detail/%s/' % entry['slug']
        reasons = '; '.join(entry['details'])
        alerts = ', '.join(entry['alerts'])
        lines.append('%s: %s [%s]' % (entry['name'], reasons, alerts))
        items.append('<li><a href="%s">%s</a>: %s <small>(%s)</small></li>' % (
            escape(absolute_url(url)), escape(entry['name']), escape(reasons), escape(alerts)))
    more = count - min(count, MAX_LISTED) + extra
    if more > 0:
        lines.append('... and %d more.' % more)
        items.append('<li>... and %d more.</li>' % more)
    text = 'Your Selection alerts found:\n' + '\n'.join(lines)
    html = '<p>Your Selection alerts found:</p><ul>%s</ul>' % ''.join(items)
    return subject, text, html


def _deliver(key, entries, now, result):
    """Send one message for one recipient key, from every due snapshot in ``entries``."""
    from YSE_App.services.notify import notify

    recipient = entries[0][1]
    with transaction.atomic():
        snapshot_ids = [snapshot_id for _a, _r, snapshot_id, _ids in entries]
        snapshots = {s.pk: s for s in
                     SelectionAlertSnapshot.objects.select_for_update().filter(pk__in=snapshot_ids).order_by('pk')}
        merged = OrderedDict()
        for alert, entry_recipient, snapshot_id, current_ids in entries:
            snapshot = snapshots.get(snapshot_id)
            if snapshot is None or not snapshot.pending:
                continue  # another runner delivered it
            for event in snapshot.pending:
                tid = event.get('t')
                if tid not in current_ids or not still_visible(event, entry_recipient.viewers):
                    continue
                entry = merged.setdefault(tid, {'name': event.get('n') or str(tid), 'slug': event.get('s') or '',
                                                'details': [], 'alerts': []})
                detail = '%s: %s' % (EVENT_LABELS.get(event.get('k'), event.get('k')), event.get('d') or '')
                if detail not in entry['details']:
                    entry['details'].append(detail)
                if alert.display_name not in entry['alerts']:
                    entry['alerts'].append(alert.display_name)
            snapshot.pending = []
            snapshot.last_delivered_at = now
            snapshot.save(update_fields=['pending', 'last_delivered_at', 'updated_at'])
        if not merged:
            return
        subject, text, html = _message(recipient, merged, 0)
        if recipient.user is not None:
            from django.urls import reverse

            notify([recipient.user], text, reverse('selection_alert_list'), NOTIFICATION_KIND,
                   subject=subject, html=html, payload={'transient_ids': list(merged)[:MAX_LISTED]})
        else:
            enqueue(SLACK_KIND, {'channel': recipient.channel, 'text': '*%s*\n%s' % (subject, text)})
        result.messages += 1


def run_alerts(now=None, *, force=False):
    """One pass over every enabled alert. Does nothing unless ``SELECTION_ALERTS_ENABLED`` (or ``force``)."""
    if not (alerts_enabled() or force):
        return {'disabled': True}
    now = now or timezone.now()
    result = PassResult()
    deliveries = defaultdict(list)
    alerts = SelectionAlert.objects.filter(enabled=True).select_related('selection', 'owner', 'team').order_by('pk')
    for alert in alerts:
        result.alerts += 1
        try:
            _evaluate_alert(alert, now, result, deliveries)
        except Exception as exc:  # noqa: BLE001 - one broken alert must not stop the others
            logger.exception('selection alert %s failed', alert.pk)
            _record_error(alert, exc)
            result.errors += 1
    for key, entries in deliveries.items():
        try:
            _deliver(key, entries, now, result)
        except Exception:  # noqa: BLE001 - events stay pending for the next pass
            logger.exception('selection alert delivery to %s failed', key)
            result.errors += 1
    out = result.as_dict()
    logger.info('selection alerts: %s', out)
    return out


@job(RUN_KIND, max_attempts=1)
def run_alerts_job(payload=None, job=None):
    """Job handler for :data:`RUN_KIND`; does nothing unless ``SELECTION_ALERTS_ENABLED``."""
    return run_alerts()


@job(SLACK_KIND, max_attempts=5, backoff_seconds=120)
def post_slack_job(payload, job=None):
    """Job handler: post one alert message to a team's Slack channel through the site's Slack app."""
    from YSE_App.integrations.slack.client import chat_post_message
    from YSE_App.services.notify import slack_dm_enabled

    if not slack_dm_enabled():
        return {'skipped': 'Slack is not configured'}
    response = chat_post_message(payload['channel'], payload['text'])
    if not response.get('ok'):
        raise JobRetry('Slack post failed: %s' % (response.get('error') or 'unknown error'))
    return {'channel': payload['channel']}


# --------------------------------------------------------------------------- volume estimate

def _buckets(times, frequency):
    """How many messages ``times`` (datetimes of events) would make at ``frequency``."""
    if frequency == SelectionAlert.FREQUENCY_DAILY:
        size = 86400
    elif frequency == SelectionAlert.FREQUENCY_HOURLY:
        size = 3600
    else:
        size = 60 * max(1, int(getattr(settings, 'SELECTION_ALERTS_CRON_MINUTES', 15) or 15))
    return len({int(t.timestamp()) // size for t in times if t is not None})


def estimate_volume(selection, user, *, on_new_match=True, on_new_spectrum=False, on_class_change=False,
                    mag_threshold=None, mag_direction=SelectionAlert.BRIGHTER,
                    frequency=SelectionAlert.FREQUENCY_DAILY, now=None, days=ESTIMATE_DAYS):
    """What an alert like this would have sent ``user`` in the last ``days`` days.

    Counted over today's matches for ``user`` (evaluated as ``user``, per point): objects added in
    the window (new match), visible spectra added in the window, recorded class changes of objects
    whose data ``user`` sees, and crossings of the threshold by visible detections in the window.
    ``messages`` is the number of messages at ``frequency`` (events in the same pass, hour or day
    share one). Raises ``SelectionError`` when the Selection cannot be listed.
    """
    from YSE_App.models import Transient
    from YSE_App.models.change_log_models import ValueChange

    now = now or timezone.now()
    cutoff = now - datetime.timedelta(days=days)
    viewers = [user]
    probe = SelectionAlert(mag_threshold=_clean_mag(mag_threshold), mag_direction=mag_direction)
    ids = matching_ids(selection, viewers)
    times = {NEW_MATCH: [], NEW_SPECTRUM: [], CLASS_CHANGE: [], MAG_CROSS: []}
    if ids and on_new_match:
        times[NEW_MATCH] = list(Transient.objects.filter(pk__in=ids, created_date__gte=cutoff)
                                .values_list('created_date', flat=True))
    if ids and on_new_spectrum:
        times[NEW_SPECTRUM] = list(_visible_spectra(viewers, ids).filter(created_date__gte=cutoff)
                                   .values_list('created_date', flat=True))
    if ids and on_class_change:
        visible = _data_visible_ids(viewers, ids)
        times[CLASS_CHANGE] = list(ValueChange.objects.filter(
            transient_id__in=visible, category=ValueChange.CATEGORY_CLASSIFICATION, created_at__gte=cutoff)
            .values_list('created_at', flat=True))
    if ids and probe.mag_threshold is not None:
        # The window plus the 60 days before it, for the magnitude each crossing starts from.
        points = (_visible_detections(viewers, ids).filter(obs_date__gte=cutoff - datetime.timedelta(days=60))
                  .order_by('photometry__transient_id', 'obs_date', 'pk')
                  .values_list('photometry__transient_id', 'obs_date', 'mag'))
        last = {}
        for tid, obs_date, mag in points:
            if tid in last and obs_date >= cutoff and _crossed(probe, last[tid], mag):
                times[MAG_CROSS].append(obs_date)
            last[tid] = mag
    all_times = [t for values in times.values() for t in values]
    return {
        'days': days,
        'matches': len(ids),
        'events': {key: len(values) for key, values in times.items()},
        'event_labels': [(EVENT_LABELS[key], len(values)) for key, values in times.items()],
        'total': len(all_times),
        'messages': _buckets(all_times, frequency),
        'frequency': dict(SelectionAlert.FREQUENCY_CHOICES).get(frequency, frequency),
    }


def selection_for_alerts(user, slug):
    """The Selection ``slug`` if ``user`` may see it (for the pages), else ``None``."""
    selection = Selection.objects.select_related('owner', 'team', 'query').filter(slug=slug).first()
    if selection is None or not selection_svc.can_view(user, selection):
        return None
    return selection

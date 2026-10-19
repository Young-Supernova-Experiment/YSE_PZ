"""Set or clear a hand-entered redshift override on a transient or host (#436, #437).

An override wins over every catalogue (NED, DESI, TNS, uploads) and is never
overwritten by a later query. The same can be done through the API
(``PATCH /api/transients/<id>/`` or ``/api/hosts/<id>/`` with ``redshift``,
``redshift_source`` and ``redshift_override``).

Examples::

  manage.py redshift_override --transient 2025abc --z 0.0312 --err 0.0005
  manage.py redshift_override --host 1234 --z 0.051 --source "YSE spectrum"
  manage.py redshift_override --transient 2025abc --clear     # back to normal precedence
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from YSE_App.models.host_models import Host
from YSE_App.models.transient_models import Transient
from YSE_App.services import redshift_precedence as rp


class Command(BaseCommand):
    help = "Set (or clear) a redshift override on a transient or host."

    def add_arguments(self, parser):
        target = parser.add_mutually_exclusive_group(required=True)
        target.add_argument("--transient", help="Transient name.")
        target.add_argument("--host", type=int, help="Host id.")
        parser.add_argument("--z", type=float, default=None, help="The redshift.")
        parser.add_argument("--err", type=float, default=None, help="Its uncertainty.")
        parser.add_argument("--source", default=rp.SOURCE_MANUAL, help="Source label (default: manual).")
        parser.add_argument("--clear", action="store_true", help="Remove the override flag (the value stays).")

    def handle(self, *args, **opts):
        if opts["transient"]:
            obj = Transient.objects.filter(name=opts["transient"]).first()
        else:
            obj = Host.objects.filter(pk=opts["host"]).first()
        if obj is None:
            raise CommandError("not found")
        if opts["clear"]:
            rp.clear_override(obj)
            self.stdout.write("override cleared; z=%r (%s)" % (obj.redshift, obj.redshift_source or "no source"))
            return
        if opts["z"] is None:
            raise CommandError("--z is required unless --clear")
        rp.set_override(obj, opts["z"], opts["err"], source=opts["source"])
        self.stdout.write("override set: z=%r +/- %r (%s)" % (obj.redshift, obj.redshift_err, obj.redshift_source))

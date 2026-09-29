"""Django signals for YSE_App."""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.db.backends.signals import connection_created
from django.db.models.signals import post_save
from django.dispatch import receiver

from YSE_App.common.collaboration_groups import ensure_user_has_public_group
from YSE_App.common.db_time_cap import cap_explorer_connection

User = get_user_model()

# Saved Explorer SQL runs under a statement time cap (#233).
connection_created.connect(cap_explorer_connection, dispatch_uid="yse_cap_explorer_connection")


@receiver(post_save, sender=User, dispatch_uid="yse_ensure_user_public_group")
def add_public_group_to_user(sender, instance, created, **kwargs):
    """Every user belongs to the Public collaboration group."""
    ensure_user_has_public_group(instance)

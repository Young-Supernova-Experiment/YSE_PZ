"""Favorite transients (#323, part of #320): the transients a user follows.

A :class:`UserFavoriteTransient` row is one user's star on one transient.
Starring is personal (no comment, no audit trail beyond the row) and drives
the ``favorite_activity`` notifications: new comments, status / class /
redshift changes, new spectra and photometry and follow-up changes on a
starred transient reach its favoriters through ``notify()``, batched per
transient and recipient (``YSE_App.services.favorites``).

The idea follows SkyPortal's ``Favorites`` (BSD-3-Clause); no code is copied.
"""

from django.contrib.auth.models import User
from django.db import models

from YSE_App.models.transient_models import Transient

__all__ = ["UserFavoriteTransient"]


class UserFavoriteTransient(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="favorite_transients")
    transient = models.ForeignKey(Transient, on_delete=models.CASCADE, related_name="favorited_by")
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = (("user", "transient"),)
        ordering = ("-created", "-id")
        indexes = [
            models.Index(fields=["transient", "user"], name="yse_fav_transient_user_idx"),
        ]
        verbose_name = "favorite transient"

    def __str__(self):
        return "%s * %s" % (self.user.username, self.transient.name)

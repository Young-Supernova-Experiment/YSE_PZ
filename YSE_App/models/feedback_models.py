"""User feedback from the site-wide Feedback button (#443).

A :class:`Feedback` row is what a signed-in user typed into the Feedback
dialog, with the page it was sent from. Feedback can contain unpublished
science or personal details and the GitHub repository is public, so a row is
only ever stored here: staff review it (admin or ``/feedback/review/``) and
may post an edited version as a GitHub issue by hand. Nothing is posted
automatically. The reporter is kept only on this row, never in the issue.
"""

from django.contrib.auth.models import User
from django.db import models

__all__ = ["Feedback"]


class Feedback(models.Model):
    STATUS_NEW = "new"
    STATUS_REVIEWED = "reviewed"
    STATUS_POSTED = "posted"
    STATUS_DISMISSED = "dismissed"
    STATUS_CHOICES = (
        (STATUS_NEW, "New"),
        (STATUS_REVIEWED, "Reviewed"),
        (STATUS_POSTED, "Posted to GitHub"),
        (STATUS_DISMISSED, "Dismissed"),
    )

    KIND_BUG = "bug"
    KIND_IDEA = "idea"
    KIND_QUESTION = "question"
    KIND_CHOICES = (
        (KIND_BUG, "Bug"),
        (KIND_IDEA, "Idea"),
        (KIND_QUESTION, "Question"),
    )

    user = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.SET_NULL, related_name="feedback_items",
        help_text="Who sent it (kept here only; never included in a GitHub issue).",
    )
    kind = models.CharField(max_length=16, choices=KIND_CHOICES, default=KIND_BUG)
    title = models.CharField(max_length=200, blank=True, default="")
    text = models.TextField()
    page_url = models.CharField(
        max_length=500, blank=True, default="",
        help_text="Site path and query the feedback was sent from (credential-like parameters removed).",
    )
    created = models.DateTimeField(auto_now_add=True)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_NEW)
    github_issue_url = models.URLField(max_length=500, blank=True, default="")
    reviewed_by = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-created", "-id")
        indexes = [
            models.Index(fields=["user", "created"], name="yse_feedback_user_created"),
            models.Index(fields=["status", "created"], name="yse_feedback_status_created"),
        ]
        verbose_name = "feedback"
        verbose_name_plural = "feedback"

    def __str__(self):
        return "Feedback #%s (%s, %s)" % (self.pk, self.get_kind_display(), self.get_status_display())

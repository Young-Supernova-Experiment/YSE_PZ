"""Read-only API representation of feed sources (#280). The credential is exposed by id only."""

from rest_framework import serializers

from YSE_App.models.feed_models import FeedSource

__all__ = ["FeedSourceSerializer"]


class FeedSourceSerializer(serializers.ModelSerializer):
    config = serializers.JSONField(read_only=True)
    credential_name = serializers.CharField(source="credential.name", read_only=True, default=None)
    has_credential = serializers.BooleanField(read_only=True)
    candidate_counts = serializers.SerializerMethodField()

    class Meta:
        model = FeedSource
        fields = (
            "id", "name", "slug", "kind", "topic", "description", "enabled", "credential", "credential_name",
            "has_credential", "config", "last_polled", "last_summary", "last_error", "candidate_counts",
            "created_at", "updated_at",
        )
        read_only_fields = fields

    def get_candidate_counts(self, obj):
        counts = (self.context or {}).get("candidate_counts") or {}
        return counts.get(obj.kind, {})

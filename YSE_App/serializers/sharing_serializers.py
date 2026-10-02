"""Read-only serializers for sharing services and submissions (#325). No credential values."""

from rest_framework import serializers

from YSE_App.models.sharing_models import SharingService, SharingSubmission

__all__ = ["SharingServiceSerializer", "SharingSubmissionSerializer"]


class SharingServiceSerializer(serializers.ModelSerializer):
    allowed_instruments = serializers.SlugRelatedField(many=True, read_only=True, slug_field="name")
    allowed_obs_groups = serializers.SlugRelatedField(many=True, read_only=True, slug_field="name")
    groups = serializers.SlugRelatedField(many=True, read_only=True, slug_field="name")
    has_credential = serializers.BooleanField(read_only=True)
    api_base_url = serializers.SerializerMethodField()

    class Meta:
        model = SharingService
        fields = ("id", "name", "slug", "kind", "description", "tns_group_id", "tns_group_name",
                  "default_coauthors", "allowed_instruments", "allowed_obs_groups", "groups", "enabled",
                  "testing", "hermes_topic", "has_credential", "api_base_url")

    def get_api_base_url(self, obj):
        return obj.api_base_url() if obj.kind == SharingService.KIND_TNS else ""


class SharingSubmissionSerializer(serializers.ModelSerializer):
    service = serializers.SlugRelatedField(read_only=True, slug_field="slug")
    transient = serializers.SlugRelatedField(read_only=True, slug_field="name")
    created_by = serializers.SlugRelatedField(read_only=True, slug_field="username")
    tns_object_url = serializers.CharField(read_only=True)

    class Meta:
        model = SharingSubmission
        fields = ("id", "service", "transient", "kind", "status", "external_id", "tns_name", "tns_object_url",
                  "error", "attempts", "payload", "response", "created_by", "created_date", "submitted_at",
                  "finished_at")

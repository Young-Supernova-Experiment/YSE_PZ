"""DRF serializers for broker filters and candidates (issue #277)."""

from django.contrib.auth.models import Group
from rest_framework import serializers

from YSE_App.brokers import registry
from YSE_App.brokers.filters import CriteriaError, validate_criteria
from YSE_App.models.candidate_models import BrokerFilter, Candidate

__all__ = ["BrokerFilterSerializer", "CandidateSerializer", "BrokerSerializer"]


class BrokerFilterSerializer(serializers.ModelSerializer):
    group = serializers.PrimaryKeyRelatedField(queryset=Group.objects.all(), allow_null=True, required=False)
    group_name = serializers.CharField(source="group.name", read_only=True, default=None)
    query = serializers.JSONField(required=False, allow_null=True)
    criteria = serializers.JSONField(required=False, allow_null=True)
    created_by = serializers.PrimaryKeyRelatedField(read_only=True)

    class Meta:
        model = BrokerFilter
        fields = (
            "id", "name", "broker", "group", "group_name", "description", "query", "criteria", "enabled",
            "auto_save", "save_status", "save_obs_group", "import_photometry", "max_alerts",
            "last_run_at", "last_run_summary", "created_by", "created_at", "updated_at",
        )
        read_only_fields = ("last_run_at", "last_run_summary", "created_by", "created_at", "updated_at")

    def validate_broker(self, value):
        if registry.provider_class(value) is None:
            raise serializers.ValidationError("unknown broker %r (known: %s)" % (value, ", ".join(registry.registered_slugs())))
        return value

    def validate_criteria(self, value):
        try:
            return validate_criteria(value)
        except CriteriaError as exc:
            raise serializers.ValidationError(str(exc))

    def validate(self, attrs):
        broker = attrs.get("broker") or getattr(self.instance, "broker", None)
        cls = registry.provider_class(broker) if broker else None
        if cls is not None and "query" in attrs:
            try:
                attrs["query"] = cls().validate_query(attrs.get("query"))
            except ValueError as exc:
                raise serializers.ValidationError({"query": str(exc)})
        return attrs


class CandidateSerializer(serializers.ModelSerializer):
    filters = serializers.PrimaryKeyRelatedField(many=True, read_only=True)
    filter_names = serializers.SerializerMethodField()
    transient_name = serializers.CharField(source="transient.name", read_only=True, default=None)
    transient_slug = serializers.CharField(source="transient.slug", read_only=True, default=None)
    payload = serializers.JSONField(read_only=True)
    passed_filter_names = serializers.JSONField(read_only=True)
    cutout_urls = serializers.SerializerMethodField()
    url = serializers.SerializerMethodField()
    gal_b = serializers.SerializerMethodField()

    class Meta:
        model = Candidate
        fields = (
            "id", "broker", "alert_id", "latest_alert_id", "ra", "dec", "gal_b", "discovery_mjd", "last_mjd", "last_mag",
            "last_band", "rb", "classification", "n_alerts", "filters", "filter_names", "passed_filter_names",
            "status", "transient", "transient_name", "transient_slug", "status_changed_by", "status_changed_at",
            "note", "first_seen", "last_seen", "url", "cutout_urls", "payload",
        )
        read_only_fields = fields

    def get_filter_names(self, obj):
        return [f.name for f in obj.filters.all()]

    def get_cutout_urls(self, obj):
        return obj.cutout_urls

    def get_url(self, obj):
        return obj.url

    def get_gal_b(self, obj):
        return obj.gal_b


class BrokerSerializer(serializers.Serializer):
    slug = serializers.CharField()
    name = serializers.CharField()
    description = serializers.CharField()
    capabilities = serializers.ListField(child=serializers.CharField())
    available = serializers.BooleanField()
    unavailable_reason = serializers.CharField(allow_blank=True)
    query_keys = serializers.ListField(child=serializers.CharField())

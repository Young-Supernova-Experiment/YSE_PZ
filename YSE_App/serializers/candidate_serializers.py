"""DRF serializers for broker filters and candidates (issue #277)."""

from django.contrib.auth.models import Group
from rest_framework import serializers

from YSE_App.brokers import registry
from YSE_App.brokers.filters import CriteriaError, validate_criteria
from YSE_App.models.candidate_models import BrokerConnection, BrokerFilter, BrokerFilterVersion, Candidate, IngestHeartbeat
from YSE_App.models.tag_models import TransientTag

__all__ = ["BrokerFilterSerializer", "BrokerFilterVersionSerializer", "CandidateSerializer", "BrokerSerializer",
           "BrokerConnectionSerializer", "IngestHeartbeatSerializer"]


class BrokerFilterSerializer(serializers.ModelSerializer):
    group = serializers.PrimaryKeyRelatedField(queryset=Group.objects.all(), allow_null=True, required=False)
    group_name = serializers.CharField(source="group.name", read_only=True, default=None)
    query = serializers.JSONField(required=False, allow_null=True)
    criteria = serializers.JSONField(required=False, allow_null=True)
    topics = serializers.JSONField(required=False, allow_null=True)
    default_tags = serializers.SlugRelatedField(slug_field="name", queryset=TransientTag.objects.all(), many=True,
                                                required=False)
    created_by = serializers.PrimaryKeyRelatedField(read_only=True)

    class Meta:
        model = BrokerFilter
        fields = (
            "id", "name", "broker", "group", "group_name", "description", "query", "criteria", "topics", "enabled",
            "auto_save", "save_status", "save_obs_group", "import_photometry", "default_tags", "notify_group",
            "max_alerts", "version", "last_run_at", "last_run_summary", "created_by", "created_at", "updated_at",
        )
        read_only_fields = ("version", "last_run_at", "last_run_summary", "created_by", "created_at", "updated_at")

    def validate_topics(self, value):
        if value in (None, ""):
            return None
        if isinstance(value, str):
            value = [v.strip() for v in value.split(",") if v.strip()]
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise serializers.ValidationError("topics must be a list of topic names")
        return value

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


class BrokerFilterVersionSerializer(serializers.ModelSerializer):
    query = serializers.JSONField(read_only=True)
    criteria = serializers.JSONField(read_only=True)
    topics = serializers.JSONField(read_only=True)
    saved_by = serializers.CharField(source="saved_by.username", read_only=True, default=None)
    is_current = serializers.BooleanField(read_only=True)

    class Meta:
        model = BrokerFilterVersion
        fields = ("id", "filter", "version", "is_current", "query", "criteria", "topics", "saved_by", "saved_at", "note")
        read_only_fields = fields


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
    rejected_by_groups = serializers.SlugRelatedField(slug_field="name", many=True, read_only=True)

    class Meta:
        model = Candidate
        fields = (
            "id", "broker", "alert_id", "latest_alert_id", "ra", "dec", "gal_b", "discovery_mjd", "last_mjd", "last_mag",
            "last_band", "rb", "classification", "n_alerts", "topic", "filters", "filter_names", "passed_filter_names",
            "status", "rejected_by_groups", "transient", "transient_name", "transient_slug", "status_changed_by",
            "status_changed_at", "note", "first_seen", "last_seen", "url", "cutout_urls", "payload",
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
    stream = serializers.DictField(allow_null=True, required=False)


class BrokerConnectionSerializer(serializers.ModelSerializer):
    topics = serializers.JSONField(read_only=True)
    config = serializers.JSONField(read_only=True)
    has_credential = serializers.SerializerMethodField()

    class Meta:
        model = BrokerConnection
        fields = ("id", "name", "slug", "broker", "kind", "bootstrap_servers", "topics", "group_id", "message_format",
                  "has_credential", "config", "enabled", "description", "last_message_at", "last_error", "created_at",
                  "updated_at")
        read_only_fields = fields

    def get_has_credential(self, obj):
        return bool(obj.credential_id)


class IngestHeartbeatSerializer(serializers.ModelSerializer):
    connection = serializers.CharField(source="connection.slug", read_only=True)
    broker = serializers.CharField(source="connection.broker", read_only=True)
    age_seconds = serializers.SerializerMethodField()
    stale = serializers.SerializerMethodField()

    class Meta:
        model = IngestHeartbeat
        fields = ("id", "connection", "broker", "topic", "worker", "hostname", "pid", "status", "started_at", "last_seen",
                  "age_seconds", "stale", "messages", "candidates", "saved", "errors", "last_offset", "lag",
                  "last_alert_id", "last_error", "last_error_at")
        read_only_fields = fields

    def get_age_seconds(self, obj):
        return obj.age_seconds()

    def get_stale(self, obj):
        from YSE_App.brokers.streams import stale_minutes

        return obj.is_stale(stale_minutes())

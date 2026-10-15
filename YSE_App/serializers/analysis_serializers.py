"""DRF serializers for analysis services and runs (#313, #314)."""

from rest_framework import serializers

from YSE_App.models import AnalysisResultFile, AnalysisService, ExternalServiceRun


class AnalysisServiceSerializer(serializers.ModelSerializer):
    slug = serializers.CharField(source="service.slug", read_only=True)
    name = serializers.CharField(source="service.name", read_only=True)
    description = serializers.CharField(source="service.description", read_only=True)
    enabled = serializers.BooleanField(source="service.enabled", read_only=True)
    max_runs_per_user_per_day = serializers.IntegerField(source="service.max_runs_per_user_per_day", read_only=True)
    groups = serializers.SlugRelatedField(source="service.groups", slug_field="name", many=True, read_only=True)
    has_credential = serializers.SerializerMethodField()
    param_fields = serializers.SerializerMethodField()
    cap = serializers.SerializerMethodField()
    url = serializers.HyperlinkedIdentityField(view_name="analysisservice-detail", lookup_field="pk")

    class Meta:
        model = AnalysisService
        fields = ("url", "slug", "name", "description", "enabled", "runner_kind", "runner_path", "input_spec",
                  "output_spec", "param_schema", "param_fields", "timeout_seconds", "max_runs_per_user_per_day",
                  "cap", "groups", "has_credential", "display_order", "summary_keys")
        read_only_fields = fields

    def get_has_credential(self, obj):
        return bool(obj.service.credential_id)

    def get_param_fields(self, obj):
        from YSE_App.services.analysis_services import form_fields

        return form_fields(obj)

    def get_cap(self, obj):
        from YSE_App.services.analysis_services import cap_status

        request = self.context.get("request")
        return cap_status(obj.service, getattr(request, "user", None))


class AnalysisResultFileSerializer(serializers.ModelSerializer):
    url = serializers.SerializerMethodField()

    class Meta:
        model = AnalysisResultFile
        fields = ("id", "name", "kind", "content_type", "size", "meta", "url")
        read_only_fields = fields

    def get_url(self, obj):
        from django.urls import reverse

        path = reverse("analysis_run_file", kwargs={"run_uuid": str(obj.run.uuid), "file_id": obj.pk, "name": obj.name})
        request = self.context.get("request")
        return request.build_absolute_uri(path) if request is not None else path


class AnalysisRunSerializer(serializers.ModelSerializer):
    """Read view of an analysis run; ``AnalysisRunCreateSerializer`` starts one."""

    url = serializers.HyperlinkedIdentityField(view_name="analysisrun-detail", lookup_field="uuid")
    service = serializers.CharField(source="service.slug", read_only=True)
    service_name = serializers.CharField(source="service.name", read_only=True)
    transient = serializers.HyperlinkedRelatedField(read_only=True, view_name="transient-detail")
    transient_name = serializers.CharField(source="transient.name", read_only=True, default="")
    created_by = serializers.CharField(source="created_by.username", read_only=True, default="")
    params = serializers.SerializerMethodField()
    duration_seconds = serializers.SerializerMethodField()
    files = AnalysisResultFileSerializer(many=True, read_only=True)

    class Meta:
        model = ExternalServiceRun
        fields = ("url", "uuid", "service", "service_name", "transient", "transient_name", "status", "created_by",
                  "created_date", "started_at", "finished_at", "duration_seconds", "params", "result", "error",
                  "external_id", "files")
        read_only_fields = fields

    def get_params(self, obj):
        from YSE_App.services.analysis_services import run_params

        return run_params(obj)

    def get_duration_seconds(self, obj):
        return obj.duration.total_seconds() if obj.duration else None


class AnalysisRunCreateSerializer(serializers.Serializer):
    service = serializers.CharField(help_text="analysis service slug")
    transient = serializers.CharField(help_text="transient id or name")
    params = serializers.DictField(required=False, default=dict)

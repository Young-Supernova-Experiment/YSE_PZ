"""DRF serializers for transient summaries (#295, #297)."""

from rest_framework import serializers

from YSE_App.models import TransientSummaryHistory


class TransientSummaryVersionSerializer(serializers.ModelSerializer):
    created_by = serializers.SlugRelatedField(slug_field="username", read_only=True)
    run = serializers.SlugRelatedField(slug_field="uuid", read_only=True)

    class Meta:
        model = TransientSummaryHistory
        fields = ("id", "text", "source", "provider", "model_name", "prompt_version", "inputs_hash", "run",
                  "is_current", "created_by", "created_date")
        read_only_fields = fields


class TransientSummaryWriteSerializer(serializers.Serializer):
    """``PATCH /api/transients/<id>/summary/`` body."""

    text = serializers.CharField(max_length=4000, allow_blank=False, trim_whitespace=True)

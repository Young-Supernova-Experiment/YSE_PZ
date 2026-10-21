"""DRF serializers for transient annotations (#317)."""

from django.contrib.auth.models import Group
from rest_framework import serializers

from YSE_App.models import TransientAnnotation


class TransientAnnotationSerializer(serializers.ModelSerializer):
    """Read shape shared by the viewset and the legacy shim (``services.annotations.legacy_annotation``)."""

    url = serializers.HyperlinkedIdentityField(view_name="transientannotation-detail", lookup_field="pk")
    transient = serializers.PrimaryKeyRelatedField(read_only=True)
    transient_name = serializers.CharField(source="transient.name", read_only=True)
    transient_url = serializers.HyperlinkedRelatedField(source="transient", view_name="transient-detail", read_only=True)
    groups = serializers.SlugRelatedField(slug_field="name", many=True, read_only=True)
    service = serializers.SlugRelatedField(slug_field="slug", read_only=True)
    run = serializers.SlugRelatedField(slug_field="uuid", read_only=True)
    created_by = serializers.SlugRelatedField(slug_field="username", read_only=True)
    modified_by = serializers.SlugRelatedField(slug_field="username", read_only=True)
    data = serializers.JSONField(read_only=True)
    verdict = serializers.CharField(read_only=True)
    read_only = serializers.SerializerMethodField()

    class Meta:
        model = TransientAnnotation
        fields = ("id", "url", "transient", "transient_name", "transient_url", "origin", "data", "verdict", "groups",
                  "service", "run", "created_by", "modified_by", "created_date", "modified_date", "read_only")
        read_only_fields = fields

    def get_read_only(self, obj):
        return False


class TransientAnnotationWriteSerializer(serializers.Serializer):
    """``POST`` / ``PUT`` / ``PATCH`` body: ``{transient: id or name, origin, data: {...}, groups: [names]}``."""

    transient = serializers.CharField(required=False, allow_blank=False)
    origin = serializers.CharField(required=False, max_length=64)
    data = serializers.DictField(required=False)
    groups = serializers.ListField(child=serializers.CharField(), required=False, allow_empty=True)
    merge = serializers.BooleanField(required=False, default=False)

    def validate_groups(self, names):
        groups = list(Group.objects.filter(name__in=names))
        missing = set(names) - {g.name for g in groups}
        if missing:
            raise serializers.ValidationError("unknown group(s): %s" % ", ".join(sorted(missing)))
        return groups

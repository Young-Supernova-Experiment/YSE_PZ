"""Serializers for transient interests (#289) and data access requests (#292)."""

from rest_framework import serializers

from YSE_App.models import DataAccessRequest, TransientInterest


class TransientInterestSerializer(serializers.ModelSerializer):
    user = serializers.StringRelatedField(read_only=True)
    user_id = serializers.IntegerField(read_only=True)
    user_display = serializers.CharField(read_only=True)
    transient_name = serializers.CharField(source="transient.name", read_only=True)
    group_name = serializers.CharField(source="group.name", read_only=True, default=None)

    class Meta:
        model = TransientInterest
        fields = (
            "id", "transient", "transient_name", "user", "user_id", "user_display", "group", "group_name",
            "title", "description", "role", "status", "doi", "created_date", "modified_date",
        )
        read_only_fields = ("id", "user", "user_id", "user_display", "transient_name", "group_name",
                            "created_date", "modified_date")


class DataAccessRequestSerializer(serializers.ModelSerializer):
    requester = serializers.StringRelatedField(read_only=True)
    requester_id = serializers.IntegerField(read_only=True)
    transient_name = serializers.CharField(source="transient.name", read_only=True)
    owner_group_name = serializers.CharField(source="owner_group.name", read_only=True)
    target_group_name = serializers.CharField(source="target_group.name", read_only=True)
    decided_by = serializers.StringRelatedField(read_only=True)
    granted_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = DataAccessRequest
        fields = (
            "id", "requester", "requester_id", "transient", "transient_name", "dataset_kind", "dataset_id",
            "owner_group", "owner_group_name", "target_group", "target_group_name", "message", "status",
            "decided_by", "decided_at", "note", "granted_dataset_ids", "granted_count",
            "created_date", "modified_date",
        )
        read_only_fields = (
            "id", "requester", "requester_id", "transient_name", "owner_group_name", "target_group_name",
            "status", "decided_by", "decided_at", "note", "granted_dataset_ids", "granted_count",
            "created_date", "modified_date",
        )
        extra_kwargs = {"target_group": {"required": False, "allow_null": True}}

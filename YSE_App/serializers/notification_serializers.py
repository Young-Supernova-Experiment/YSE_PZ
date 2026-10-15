"""Serializers for the in-app notifications API (#321) and favorite transients (#323)."""

from rest_framework import serializers

from YSE_App.models import Notification, Transient, UserFavoriteTransient


class NotificationSerializer(serializers.ModelSerializer):
    kind_label = serializers.CharField(read_only=True)
    is_read = serializers.BooleanField(read_only=True)
    transient_name = serializers.CharField(source="transient.name", read_only=True, default=None)

    class Meta:
        model = Notification
        fields = ("id", "kind", "kind_label", "subject", "text", "url", "transient", "transient_name", "payload",
                  "delivered", "created", "read_at", "is_read")
        read_only_fields = fields


class FavoriteTransientSerializer(serializers.ModelSerializer):
    transient = serializers.PrimaryKeyRelatedField(queryset=Transient.objects.all())
    transient_name = serializers.CharField(source="transient.name", read_only=True)
    transient_slug = serializers.CharField(source="transient.slug", read_only=True)
    user = serializers.StringRelatedField(read_only=True)

    class Meta:
        model = UserFavoriteTransient
        fields = ("id", "user", "transient", "transient_name", "transient_slug", "created")
        read_only_fields = ("id", "user", "transient_name", "transient_slug", "created")

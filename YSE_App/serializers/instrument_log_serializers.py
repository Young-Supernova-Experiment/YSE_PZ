"""DRF serializer for instrument logs (#310)."""

from rest_framework import serializers

from YSE_App.models import Instrument, InstrumentLog


class InstrumentLogSerializer(serializers.HyperlinkedModelSerializer):
    instrument = serializers.HyperlinkedRelatedField(queryset=Instrument.objects.all(), view_name='instrument-detail',
                                                     lookup_field='id')
    instrument_name = serializers.CharField(source="instrument.name", read_only=True)
    telescope_name = serializers.CharField(source="instrument.telescope.name", read_only=True)
    log = serializers.JSONField(required=False)
    entries = serializers.ListField(read_only=True)
    run = serializers.UUIDField(source="run.uuid", read_only=True, allow_null=True)
    created_by = serializers.HyperlinkedRelatedField(read_only=True, view_name='user-detail')
    modified_by = serializers.HyperlinkedRelatedField(read_only=True, view_name='user-detail')

    class Meta:
        model = InstrumentLog
        fields = ("url", "id", "instrument", "instrument_name", "telescope_name", "start", "end", "message", "log",
                  "entries", "source", "source_name", "fingerprint", "run", "created_by", "created_date",
                  "modified_by", "modified_date")
        read_only_fields = ("fingerprint", "run", "created_date", "modified_date")

    def validate(self, attrs):
        if not (attrs.get("message") or "").strip() and not attrs.get("log"):
            raise serializers.ValidationError({"message": "a message or structured log entries are required"})
        if attrs.get("end") and attrs.get("start") and attrs["end"] < attrs["start"]:
            raise serializers.ValidationError({"end": "end must not be before start"})
        return attrs

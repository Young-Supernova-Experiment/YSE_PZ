from rest_framework import serializers

from YSE_App.models import TransientPhotStat


class TransientPhotStatSerializer(serializers.HyperlinkedModelSerializer):
    """Read-only view of a transient's stored photometry statistics (#268)."""

    transient = serializers.HyperlinkedRelatedField(read_only=True, view_name='transient-detail')
    transient_name = serializers.CharField(source='transient.name', read_only=True)
    first_detected_band = serializers.HyperlinkedRelatedField(
        read_only=True, view_name='photometricband-detail', lookup_field='id')
    last_detected_band = serializers.HyperlinkedRelatedField(
        read_only=True, view_name='photometricband-detail', lookup_field='id')
    peak_band = serializers.HyperlinkedRelatedField(
        read_only=True, view_name='photometricband-detail', lookup_field='id')
    first_detected_band_name = serializers.CharField(source='first_detected_band.name', read_only=True, default=None)
    last_detected_band_name = serializers.CharField(source='last_detected_band.name', read_only=True, default=None)
    peak_band_name = serializers.CharField(source='peak_band.name', read_only=True, default=None)
    per_band = serializers.JSONField(read_only=True)

    class Meta:
        model = TransientPhotStat
        exclude = ('per_band_json', 'phot_hash')
        read_only_fields = [f.name for f in TransientPhotStat._meta.fields]
        extra_kwargs = {
            'url': {'view_name': 'transientphotstat-detail'},
        }

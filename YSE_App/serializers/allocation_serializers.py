"""DRF serializers for allocations and facility requests (#305, #299)."""

from rest_framework import serializers

from YSE_App.models import Allocation, FacilityRequest


class AllocationSerializer(serializers.HyperlinkedModelSerializer):
    telescope = serializers.HyperlinkedRelatedField(queryset=Allocation._meta.get_field("telescope").remote_field.model.objects.all(),
                                                    view_name='telescope-detail')
    instrument = serializers.HyperlinkedRelatedField(queryset=Allocation._meta.get_field("instrument").remote_field.model.objects.all(),
                                                     allow_null=True, required=False, view_name='instrument-detail')
    principal_investigator = serializers.HyperlinkedRelatedField(
        queryset=Allocation._meta.get_field("principal_investigator").remote_field.model.objects.all(),
        allow_null=True, required=False, view_name='principalinvestigator-detail')
    groups = serializers.HyperlinkedRelatedField(many=True, required=False,
                                                 queryset=Allocation._meta.get_field("groups").remote_field.model.objects.all(),
                                                 view_name='group-detail')
    created_by = serializers.HyperlinkedRelatedField(read_only=True, view_name='user-detail')
    modified_by = serializers.HyperlinkedRelatedField(read_only=True, view_name='user-detail')
    # never the credential row itself, only whether one with a secret is bound
    has_credential = serializers.BooleanField(read_only=True)
    hours_remaining = serializers.FloatField(read_only=True)
    percent_used = serializers.IntegerField(read_only=True)
    facility_name = serializers.SerializerMethodField()

    class Meta:
        model = Allocation
        exclude = ("credential", "service")
        read_only_fields = ("hours_used",)

    def get_facility_name(self, obj):
        from YSE_App.facilities import get_facility

        adapter = get_facility(obj.facility) if obj.facility else None
        return adapter.name if adapter else ""


class FacilityRequestSerializer(serializers.HyperlinkedModelSerializer):
    allocation = serializers.HyperlinkedRelatedField(read_only=True, view_name='allocation-detail')
    transient = serializers.HyperlinkedRelatedField(read_only=True, view_name='transient-detail')
    transient_name = serializers.CharField(source="transient.name", read_only=True)
    allocation_name = serializers.CharField(source="allocation.name", read_only=True)
    facility = serializers.CharField(source="allocation.facility", read_only=True)
    submitted_by = serializers.HyperlinkedRelatedField(read_only=True, view_name='user-detail')
    run = serializers.UUIDField(source="run.uuid", read_only=True, allow_null=True)
    created_by = serializers.HyperlinkedRelatedField(read_only=True, view_name='user-detail')
    modified_by = serializers.HyperlinkedRelatedField(read_only=True, view_name='user-detail')

    class Meta:
        model = FacilityRequest
        exclude = ("followup",)
        read_only_fields = [f.name for f in FacilityRequest._meta.fields]

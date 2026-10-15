from rest_framework import serializers
from YSE_App.models import *

# Do not import YSE_App.services.followup_requests at module top.
# URLconf load: views -> yse_pa -> table_utils -> view_utils -> serializers;
# a top-level service import races the models package and raises
# ImportError: cannot import name TransientFollowupRequest.

DEFAULT_PRIORITY = 4.0


class TransientFollowupRequestSerializer(serializers.HyperlinkedModelSerializer):
	requestor = serializers.HyperlinkedRelatedField(read_only=True, view_name='user-detail')
	created_by = serializers.HyperlinkedRelatedField(read_only=True, view_name='user-detail')
	modified_by = serializers.HyperlinkedRelatedField(read_only=True, view_name='user-detail')

	class Meta:
		model = TransientFollowupRequest
		fields = (
			'id',
			'requestor',
			'requested_at',
			'priority',
			'comment',
			'created_by',
			'modified_by',
		)
		read_only_fields = fields


class TransientFollowupSerializer(serializers.HyperlinkedModelSerializer):
	transient = serializers.HyperlinkedRelatedField(queryset=Transient.objects.all(), view_name='transient-detail')
	status = serializers.HyperlinkedRelatedField(queryset=FollowupStatus.objects.all(), view_name='followupstatus-detail')

	too_resource = serializers.HyperlinkedRelatedField(queryset=ToOResource.objects.all(), allow_null=True, required=False, view_name='tooresource-detail')
	classical_resource = serializers.HyperlinkedRelatedField(queryset=ClassicalResource.objects.all(), allow_null=True, required=False, view_name='classicalresource-detail') #, lookup_field="id")
	queued_resource = serializers.HyperlinkedRelatedField(queryset=QueuedResource.objects.all(), allow_null=True, required=False, view_name='queuedresource-detail')

	created_by = serializers.HyperlinkedRelatedField(read_only=True, view_name='user-detail')
	modified_by = serializers.HyperlinkedRelatedField(read_only=True, view_name='user-detail')
	requests = TransientFollowupRequestSerializer(many=True, read_only=True)

	class Meta:
		model = TransientFollowup
		fields = "__all__"

	def create(self, validated_data):
		# Circular: see module comment. Service import must stay in this method.
		from YSE_App.services.followup_requests import create_or_attach_request

		user = validated_data.pop('created_by', None)
		validated_data.pop('modified_by', None)
		if user is None:
			user = self.context['request'].user
		priority = validated_data.pop('priority', None)
		if priority is None:
			priority = DEFAULT_PRIORITY
		is_public = validated_data.pop('is_public', None)
		groups = validated_data.pop('groups', None)
		parent, _child, _created = create_or_attach_request(
			user,
			validated_data.pop('transient'),
			status=validated_data.pop('status'),
			valid_start=validated_data.pop('valid_start'),
			valid_stop=validated_data.pop('valid_stop'),
			priority=priority,
			classical_resource=validated_data.pop('classical_resource', None),
			too_resource=validated_data.pop('too_resource', None),
			queued_resource=validated_data.pop('queued_resource', None),
			offset_star_ra=validated_data.pop('offset_star_ra', None),
			offset_star_dec=validated_data.pop('offset_star_dec', None),
			offset_north=validated_data.pop('offset_north', None),
			offset_east=validated_data.pop('offset_east', None),
		)
		if is_public is not None and parent.is_public != is_public:
			parent.is_public = is_public
			parent.save(update_fields=['is_public'])
		if groups is not None:
			parent.groups.set(groups)
		return parent

	def update(self, instance, validated_data):
		instance.transient_id = validated_data.get('transient', instance.transient)
		instance.status_id = validated_data.get('status', instance.status)
		instance.too_resource_id = validated_data.get('too_resource', instance.too_resource)
		instance.classical_resource_id = validated_data.get('classical_resource', instance.classical_resource)
		instance.queued_resource_id = validated_data.get('queued_resource', instance.queued_resource)

		instance.valid_start = validated_data.get('valid_start', instance.valid_start)
		instance.valid_stop = validated_data.get('valid_stop', instance.valid_stop)
		instance.offset_star_ra = validated_data.get('offset_star_ra', instance.offset_star_ra)
		instance.offset_star_dec = validated_data.get('offset_star_dec', instance.offset_star_dec)
		instance.offset_north = validated_data.get('offset_north', instance.offset_north)
		instance.offset_east = validated_data.get('offset_east', instance.offset_east)

		instance.modified_by_id = validated_data.get('modified_by', instance.modified_by)
		
		instance.save()

		return instance

class HostFollowupSerializer(serializers.HyperlinkedModelSerializer):
	host = serializers.HyperlinkedRelatedField(queryset=Host.objects.all(), view_name='host-detail', lookup_field="id")
	status = serializers.HyperlinkedRelatedField(queryset=FollowupStatus.objects.all(), view_name='followupstatus-detail')

	too_resource = serializers.HyperlinkedRelatedField(queryset=ToOResource.objects.all(), allow_null=True, required=False, view_name='tooresource-detail')
	classical_resource = serializers.HyperlinkedRelatedField(queryset=ClassicalResource.objects.all(), allow_null=True, required=False, view_name='classicalresource-detail', lookup_field="id")
	queued_resource = serializers.HyperlinkedRelatedField(queryset=QueuedResource.objects.all(), allow_null=True, required=False, view_name='queuedresource-detail')

	created_by = serializers.HyperlinkedRelatedField(read_only=True, view_name='user-detail')
	modified_by = serializers.HyperlinkedRelatedField(read_only=True, view_name='user-detail')

	class Meta:
		model = HostFollowup
		fields = "__all__"

	def create(self, validated_data):
		return HostFollowup.objects.create(**validated_data)

	def update(self, instance, validated_data):
		instance.host_id = validated_data.get('transient', instance.transient)
		instance.status_id = validated_data.get('status', instance.status)
		instance.too_resource_id = validated_data.get('too_resource', instance.too_resource)
		instance.classical_resource_id = validated_data.get('classical_resource', instance.classical_resource)
		instance.queued_resource_id = validated_data.get('queued_resource', instance.queued_resource)

		instance.valid_start = validated_data.get('valid_start', instance.valid_start)
		instance.valid_stop = validated_data.get('valid_stop', instance.valid_stop)
		instance.priority = validated_data.get('priority', instance.priority)
		instance.offset_star_ra = validated_data.get('offset_star_ra', instance.offset_star_ra)
		instance.offset_star_dec = validated_data.get('offset_star_dec', instance.offset_star_dec)
		instance.offset_north = validated_data.get('offset_north', instance.offset_north)
		instance.offset_east = validated_data.get('offset_east', instance.offset_east)

		instance.modified_by_id = validated_data.get('modified_by', instance.modified_by)
		
		instance.save()

		return instance

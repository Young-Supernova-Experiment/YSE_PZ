from rest_framework import serializers
from YSE_App.models import *
from django.contrib.auth.models import User

def _is_staff(request):
	user = getattr(request, 'user', None)
	return bool(user is not None and user.is_authenticated and (user.is_staff or user.is_superuser))


class PrincipalInvestigatorSerializer(serializers.HyperlinkedModelSerializer):
	obs_group = serializers.HyperlinkedRelatedField(queryset=ObservationGroup.objects.all(), many=True, allow_null=True, required=False, view_name='observationgroup-detail')

	created_by = serializers.HyperlinkedRelatedField(read_only=True, view_name='user-detail')
	modified_by = serializers.HyperlinkedRelatedField(read_only=True, view_name='user-detail')

	class Meta:
		model = PrincipalInvestigator
		# The PI's YSE-PZ account link (#446) is set by staff in the admin and not exposed here.
		exclude = ('user',)

	id = serializers.IntegerField(read_only=True)

	# Contact details are personal data (#643). Non-staff responses carry only the PI's
	# id, URL and name; staff see every field. Non-staff API users (ingest scripts) can
	# still send the other fields on create/update: they become write-only for them.
	PUBLIC_FIELDS = ('url', 'id', 'name')

	def get_fields(self):
		fields = super().get_fields()
		if _is_staff(self.context.get('request')):
			return fields
		for name in list(fields):
			if name in self.PUBLIC_FIELDS:
				continue
			if fields[name].read_only:
				del fields[name]
			else:
				fields[name].write_only = True
		return fields

	def create(self, validated_data):
		ogs = validated_data.pop('obs_group')

		pi = PrincipalInvestigator.objects.create(**validated_data)
		pi.save()

		for og in ogs:
			og_result = ObservationGroup.objects.filter(pk=og.id)
			if og_result.exists():
				o = og_result.first()
				pi.obs_group.add(o)
		
		pi.save()
		return pi

	def update(self, instance, validated_data):
		# Disassociate existing `Observation Groups`
		ogs = instance.obs_group.all()
		for og in ogs:
			instance.obs_group.remove(og)

		ogs = validated_data.pop('obs_group')
		for og in ogs:
			og_result = ObservationGroup.objects.filter(pk=og.id)
			if og_result.exists():
				o = og_result.first()
				instance.obs_group.add(o)

		instance.name = validated_data.get('name', instance.name)
		instance.phone = validated_data.get('phone', instance.phone)
		instance.email = validated_data.get('email', instance.email)
		instance.institution = validated_data.get('institution', instance.institution)
		instance.description = validated_data.get('description', instance.description)

		instance.modified_by_id = validated_data.get('modified_by', instance.modified_by)

		instance.save()
		
		return instance
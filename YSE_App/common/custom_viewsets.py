from rest_framework import mixins
from rest_framework import viewsets

from YSE_App.common import custom_mixins
from YSE_App.common.authz import LegacyApiWritePermission, legacy_write_permission_applies

class ListCreateRetrieveUpdateViewSet(mixins.CreateModelMixin,
						mixins.RetrieveModelMixin,
						mixins.UpdateModelMixin,
						mixins.ListModelMixin,
						custom_mixins.UpdateModelMixin,
						viewsets.GenericViewSet):
	def get_permissions(self):
		# Model permissions on unsafe methods (flag-gated, see YSE_App.common.authz).
		perms = super().get_permissions()
		if legacy_write_permission_applies(self):
			perms.append(LegacyApiWritePermission())
		return perms

	def perform_create(self, serializer):
		serializer.save(created_by=self.request.user, modified_by=self.request.user)

	def perform_update(self, serializer):
		serializer.save(modified_by=self.request.user.id)

	def perform_partial_update(self, serializer):
		serializer.save(modified_by=self.request.user.id)
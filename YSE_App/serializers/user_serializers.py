from rest_framework import serializers
from django.contrib.auth.models import User


class UserSerializer(serializers.ModelSerializer):
	"""Read-oriented user payload for /api/users/.

	Avoid HyperlinkedModelSerializer ``fields = "__all__"``: that tries to
	reverse ``permission-detail`` for ``user_permissions`` and 500s for staff.
	"""

	url = serializers.HyperlinkedIdentityField(view_name="user-detail")
	groups = serializers.SlugRelatedField(many=True, read_only=True, slug_field="name")

	class Meta:
		model = User
		fields = (
			"id",
			"url",
			"username",
			"email",
			"first_name",
			"last_name",
			"is_staff",
			"is_superuser",
			"is_active",
			"groups",
		)

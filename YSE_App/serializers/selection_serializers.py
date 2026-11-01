"""Read-only serializer for Selections (#841). The SQL text of an Explorer query is never included."""

from rest_framework import serializers

from YSE_App.models import Selection


class SelectionSerializer(serializers.ModelSerializer):
    owner = serializers.SerializerMethodField()
    team = serializers.SerializerMethodField()
    query_title = serializers.SerializerMethodField()
    criteria = serializers.SerializerMethodField()
    can_edit = serializers.SerializerMethodField()
    transients_url = serializers.SerializerMethodField()

    class Meta:
        model = Selection
        fields = (
            'id', 'slug', 'name', 'description', 'kind', 'visibility', 'owner', 'team',
            'search_params', 'rules', 'query', 'query_title', 'python_query', 'user_query',
            'criteria', 'can_edit', 'created_date', 'modified_date', 'transients_url',
        )
        read_only_fields = fields

    def _user(self):
        request = self.context.get('request')
        return getattr(request, 'user', None)

    def get_owner(self, obj):
        return obj.owner.username

    def get_team(self, obj):
        return obj.team.name if obj.team_id else None

    def get_query_title(self, obj):
        return obj.query.title if obj.query_id else None

    def get_criteria(self, obj):
        from YSE_App.services.selections import criteria_summary

        return [{'label': str(label), 'value': str(value)} for label, value in criteria_summary(obj, self._user())]

    def get_can_edit(self, obj):
        from YSE_App.services.selections import can_edit

        return can_edit(self._user(), obj)

    def get_transients_url(self, obj):
        from rest_framework.reverse import reverse

        return reverse('selection-transients', kwargs={'slug': obj.slug}, request=self.context.get('request'))

"""Hyperlinked related field that keeps DRF's pk-only optimization (#456).

``HyperlinkedRelatedField(lookup_field="id")`` disables the optimization
(DRF only applies it when ``lookup_field == "pk"``), so every related object
is loaded per row just to read its ``id``. For models whose primary key is the
``id`` column this field builds the same URL from the raw foreign-key value
without loading the related row, so the JSON output is unchanged.
"""
from rest_framework import serializers
from rest_framework.relations import PKOnlyObject


class IdHyperlinkedRelatedField(serializers.HyperlinkedRelatedField):
    def use_pk_only_optimization(self):
        return self.lookup_field in ("pk", "id")

    def get_url(self, obj, view_name, request, format):
        if isinstance(obj, PKOnlyObject) and self.lookup_field == "id":
            obj.id = obj.pk
        return super().get_url(obj, view_name, request, format)

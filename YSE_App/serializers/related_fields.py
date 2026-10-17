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


class _VisibleDatasetMixin:
    """Limit a related field to rows the requesting user may see (central read rule).

    ``dataset_kind`` is ``"photometry"``, ``"spectrum"`` or ``"photdata"`` (a
    point, visible when its parent set is). Staff and superusers see every row.
    Without a request in the serializer context the field behaves as an
    anonymous user: only public rows.
    """

    dataset_kind = None

    def _requesting_user(self):
        from django.contrib.auth.models import AnonymousUser

        request = self.context.get("request") if self.parent is not None else None
        return getattr(request, "user", None) or AnonymousUser()

    def _visible(self, queryset, user):
        from YSE_App.services import visibility

        if visibility.user_sees_everything(user):
            return queryset
        if self.dataset_kind == "photometry":
            ids = queryset.model.objects.filter(visibility.photometry_visible_q(user)).values("pk")
            return queryset.filter(pk__in=ids)
        if self.dataset_kind == "spectrum":
            ids = queryset.model.objects.filter(visibility.spectrum_visible_q(user)).values("pk")
            return queryset.filter(pk__in=ids)
        if self.dataset_kind == "photdata":
            return queryset.filter(visibility.visible_photometry_ids_q(user))
        return queryset

    def get_queryset(self):
        queryset = super().get_queryset()
        if queryset is None:
            return queryset
        from YSE_App.common.authz import is_trusted_writer

        user = self._requesting_user()
        if is_trusted_writer(user):
            # Staff and the API-writers service group (crons, brokers) may reference any set.
            return queryset
        return self._visible(queryset, user)


class VisibleDatasetRelatedField(_VisibleDatasetMixin, serializers.HyperlinkedRelatedField):
    def __init__(self, dataset_kind=None, **kwargs):
        self.dataset_kind = dataset_kind
        super().__init__(**kwargs)


class VisibleDatasetIdRelatedField(_VisibleDatasetMixin, IdHyperlinkedRelatedField):
    def __init__(self, dataset_kind=None, **kwargs):
        self.dataset_kind = dataset_kind
        super().__init__(**kwargs)


class VisibleSpectrumOutputField(VisibleDatasetIdRelatedField):
    """Like the id field, but the output is ``None`` when the spectrum is not visible."""

    def to_representation(self, value):
        user = self._requesting_user()
        from YSE_App.services import visibility

        if not visibility.user_sees_everything(user):
            cache = self.__dict__.setdefault("_visible_cache", {})
            pk = value.pk
            if pk not in cache:
                base = self.queryset.model.objects.filter(pk=pk)
                cache[pk] = self._visible(base, user).exists()
            if not cache[pk]:
                return None
        return super().to_representation(value)

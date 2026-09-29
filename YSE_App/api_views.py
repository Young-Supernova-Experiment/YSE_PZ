from django.http import HttpResponse, HttpResponseRedirect, Http404, JsonResponse
from rest_framework import serializers, viewsets, status, permissions, mixins
from rest_framework.response import Response
from rest_framework.decorators import action, api_view
from rest_framework import generics
from YSE_App.common import custom_viewsets
from django.core.exceptions import PermissionDenied
from django.db.models import Q
from rest_framework.reverse import reverse

from .models import *
from .serializers import *
from .data import PhotometryService, SpectraService, ObservingResourceService
from YSE_App.services.visibility import filter_transients_by_user_access
from YSE_App.filters.transient_search import TransientSearchFilterSet

from django_filters.rest_framework import DjangoFilterBackend,filters
import django_filters

### `Additional Info` ViewSets ###
class TransientWebResourceViewSet(custom_viewsets.ListCreateRetrieveUpdateViewSet):
    queryset = TransientWebResource.objects.all()
    serializer_class = TransientWebResourceSerializer
    permission_classes = (permissions.IsAuthenticated,)

class HostWebResourceViewSet(custom_viewsets.ListCreateRetrieveUpdateViewSet):
    queryset = HostWebResource.objects.all()
    serializer_class = HostWebResourceSerializer
    permission_classes = (permissions.IsAuthenticated,)

### `Enum` ViewSets ###
# Only exposing GET
class TransientStatusViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = TransientStatus.objects.all()
    serializer_class = TransientStatusSerializer
    permission_classes = (permissions.IsAuthenticated,)

class FollowupStatusViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = FollowupStatus.objects.all()
    serializer_class = FollowupStatusSerializer
    permission_classes = (permissions.IsAuthenticated,)

### `SurveyField` Filter Set ###
class SurveyFieldFilter(django_filters.FilterSet):
    field_id = django_filters.Filter(field_name="field_id")
    obs_group = django_filters.Filter(field_name="obs_group__name")
    instrument = django_filters.Filter(field_name="instrument__name")
    class Meta:
        model = SurveyField
        fields = ()

### `SurveyFieldMSB` Filter Set ###
class SurveyFieldMSBFilter(django_filters.FilterSet):
    name = django_filters.Filter(field_name="name")
    active = django_filters.Filter(field_name="active")
    instrument = django_filters.Filter(field_name='survey_fields__instrument__name')
    class Meta:
        model = SurveyFieldMSB
        fields = ('name','active')

### `SurveyField ViewSets` ###
class SurveyFieldViewSet(custom_viewsets.ListCreateRetrieveUpdateViewSet):
    queryset = SurveyField.objects.all()
    serializer_class = SurveyFieldSerializer
    permission_classes = (permissions.IsAuthenticated,)
    filter_backends = (DjangoFilterBackend,)
    filter_class = SurveyFieldFilter


class SurveyFieldMSBViewSet(custom_viewsets.ListCreateRetrieveUpdateViewSet): #viewsets.ReadOnlyModelViewSet):
    queryset = SurveyFieldMSB.objects.all()
    serializer_class = SurveyFieldMSBSerializer
    permission_classes = (permissions.IsAuthenticated,)
    filter_backends = (DjangoFilterBackend,)
    filter_class = SurveyFieldMSBFilter


### `Transient` Filter Set ###
class SurveyObsFilter(django_filters.FilterSet):
    status_in = django_filters.BaseInFilter(field_name="status__name")#, lookup_expr='in')
    obs_mjd_gte = django_filters.Filter(field_name="obs_mjd", lookup_expr='gte')
    obs_mjd_lte = django_filters.Filter(field_name="obs_mjd", lookup_expr='lte')
    mjd_requested_gte = django_filters.Filter(field_name="mjd_requested", lookup_expr='gte')
    mjd_requested_lte = django_filters.Filter(field_name="mjd_requested", lookup_expr='lte')
    survey_field = django_filters.BaseInFilter(field_name="survey_field__field_id")
    obs_group = django_filters.BaseInFilter(field_name="survey_field__obs_group__name")
    ra_gt = django_filters.Filter(field_name="survey_field__ra_cen", lookup_expr='gt')
    ra_lt = django_filters.Filter(field_name="survey_field__ra_cen", lookup_expr='lt')
    dec_gt = django_filters.Filter(field_name="survey_field__dec_cen", lookup_expr='gt')
    dec_lt = django_filters.Filter(field_name="survey_field__dec_cen", lookup_expr='lt')
    instrument = django_filters.Filter(field_name="survey_field__instrument__name")
    
    class Meta:
        model = SurveyObservation
        fields = ()


class SurveyObservationViewSet(custom_viewsets.ListCreateRetrieveUpdateViewSet):
    queryset = SurveyObservation.objects.all()
    serializer_class = SurveyObservationSerializer
    permission_classes = (permissions.IsAuthenticated,)
    filter_backends = (DjangoFilterBackend,)
    filter_class = SurveyObsFilter

class TaskStatusViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = TaskStatus.objects.all()
    serializer_class = TaskStatusSerializer
    permission_classes = (permissions.IsAuthenticated,)

class AntaresClassificationViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = AntaresClassification.objects.all()
    serializer_class = AntaresClassificationSerializer
    permission_classes = (permissions.IsAuthenticated,)

class InternalSurveyViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = InternalSurvey.objects.all()
    serializer_class = InternalSurveySerializer
    permission_classes = (permissions.IsAuthenticated,)

### `ObservationGroup` Filter Set ###
class ObservationGroupFilter(django_filters.FilterSet):
    name = django_filters.Filter(field_name="name")

    class Meta:
        model = ObservationGroup
        fields = ()
    
class ObservationGroupViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = ObservationGroup.objects.all()
    serializer_class = ObservationGroupSerializer
    permission_classes = (permissions.IsAuthenticated,)
    filter_backends = (DjangoFilterBackend,)
    filter_class = ObservationGroupFilter
    
class SEDTypeViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = SEDType.objects.all()
    serializer_class = SEDTypeSerializer
    permission_classes = (permissions.IsAuthenticated,)

class HostMorphologyViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = HostMorphology.objects.all()
    serializer_class = HostMorphologySerializer
    permission_classes = (permissions.IsAuthenticated,)

class PhaseViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Phase.objects.all()
    serializer_class = PhaseSerializer
    permission_classes = (permissions.IsAuthenticated,)

class TransientClassViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = TransientClass.objects.all()
    serializer_class = TransientClassSerializer
    lookup_field = "id"
    permission_classes = (permissions.IsAuthenticated,)

class ClassicalNightTypeViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = ClassicalNightType.objects.all()
    serializer_class = ClassicalNightTypeSerializer
    permission_classes = (permissions.IsAuthenticated,)

class WebAppColorViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = WebAppColor.objects.all()
    serializer_class = WebAppColorSerializer
    permission_classes = (permissions.IsAuthenticated,)

class UnitViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Unit.objects.all()
    serializer_class = UnitSerializer
    permission_classes = (permissions.IsAuthenticated,)

class DataQualityViewSet(viewsets.ReadOnlyModelViewSet):
        queryset = DataQuality.objects.all()
        serializer_class = DataQualitySerializer
        permission_classes = (permissions.IsAuthenticated,)

class InformationSourceViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = InformationSource.objects.all()
    serializer_class = InformationSourceSerializer
    permission_classes = (permissions.IsAuthenticated,)

### `Followup` ViewSets ###
#class SimpleTransientSpecRequestViewSet(custom_viewsets.ListCreateRetrieveUpdateViewSet):
#	queryset = SimpleTransientSpecRequest.objects.all()
#	serializer_class = SimpleTransientSpecRequestSerializer
#	permission_classes = (permissions.IsAuthenticated,)

class TransientFollowupViewSet(custom_viewsets.ListCreateRetrieveUpdateViewSet):
    queryset = TransientFollowup.objects.all()
    serializer_class = TransientFollowupSerializer
    permission_classes = (permissions.IsAuthenticated,)

    def get_queryset(self):
        from YSE_App.services.visibility import filter_transient_followups_for_user

        qs = TransientFollowup.objects.all().prefetch_related("groups")
        return filter_transient_followups_for_user(qs, self.request.user)

class HostFollowupViewSet(custom_viewsets.ListCreateRetrieveUpdateViewSet):
    queryset = HostFollowup.objects.all()
    serializer_class = HostFollowupSerializer
    permission_classes = (permissions.IsAuthenticated,)

### `Host` ViewSets ###
class HostViewSet(custom_viewsets.ListCreateRetrieveUpdateViewSet):
    queryset = Host.objects.all()
    serializer_class = HostSerializer
    lookup_field = "id"
    permission_classes = (permissions.IsAuthenticated,)

class HostSEDViewSet(custom_viewsets.ListCreateRetrieveUpdateViewSet):
    queryset = HostSED.objects.all()
    serializer_class = HostSEDSerializer
    permission_classes = (permissions.IsAuthenticated,)

### `Instrument` Filter Set ###
class InstrumentFilter(django_filters.FilterSet):
    name = django_filters.Filter(field_name="name")
    class Meta:
        model = Instrument
        fields = ()
    
### `Instrument` ViewSets ###
class InstrumentViewSet(custom_viewsets.ListCreateRetrieveUpdateViewSet):
    queryset = Instrument.objects.all()
    serializer_class = InstrumentSerializer
    lookup_field = "id"
    permission_classes = (permissions.IsAuthenticated,)
    filter_backends = (DjangoFilterBackend,)
    filter_class = InstrumentFilter

    
class InstrumentConfigViewSet(custom_viewsets.ListCreateRetrieveUpdateViewSet):
    queryset = InstrumentConfig.objects.all()
    serializer_class = InstrumentConfigSerializer
    permission_classes = (permissions.IsAuthenticated,)

class ConfigElementViewSet(custom_viewsets.ListCreateRetrieveUpdateViewSet):
    queryset = ConfigElement.objects.all()
    serializer_class = ConfigElementSerializer
    permission_classes = (permissions.IsAuthenticated,)

### `Log` ViewSets ###
class LogViewSet(custom_viewsets.ListCreateRetrieveUpdateViewSet):
    queryset = Log.objects.all()
    serializer_class = LogSerializer
    permission_classes = (permissions.IsAuthenticated,)

    def get_queryset(self):
        from YSE_App.services.visibility import filter_transient_comments_for_user

        qs = Log.objects.all().prefetch_related("groups")
        return filter_transient_comments_for_user(qs, self.request.user)

### `Observation Task` ViewSets ###
class TransientObservationTaskViewSet(custom_viewsets.ListCreateRetrieveUpdateViewSet):
    queryset = TransientObservationTask.objects.all()
    serializer_class = TransientObservationTaskSerializer
    permission_classes = (permissions.IsAuthenticated,)

class HostObservationTaskViewSet(custom_viewsets.ListCreateRetrieveUpdateViewSet):
    queryset = HostObservationTask.objects.all()
    serializer_class = HostObservationTaskSerializer
    permission_classes = (permissions.IsAuthenticated,)

### `Observatory` ViewSets ###
class ObservatoryViewSet(custom_viewsets.ListCreateRetrieveUpdateViewSet):
    queryset = Observatory.objects.all()
    serializer_class = ObservatorySerializer
    permission_classes = (permissions.IsAuthenticated,)

### `On Call Date` ViewSets ###
class OnCallDateViewSet(custom_viewsets.ListCreateRetrieveUpdateViewSet):
    queryset = OnCallDate.objects.all()
    serializer_class = OnCallDateSerializer
    permission_classes = (permissions.IsAuthenticated,)

class YSEOnCallDateViewSet(custom_viewsets.ListCreateRetrieveUpdateViewSet):
    queryset = YSEOnCallDate.objects.all()
    serializer_class = YSEOnCallDateSerializer
    permission_classes = (permissions.IsAuthenticated,)

### `Phot` ViewSets ###
class TransientPhotometryViewSet(custom_viewsets.ListCreateRetrieveUpdateViewSet):
    serializer_class = TransientPhotometrySerializer
    permission_classes = (permissions.IsAuthenticated,)
    lookup_field = "id"

    def get_queryset(self):
        allowed_phot = PhotometryService.GetAuthorizedTransientPhotometry_ByUser(self.request.user)
        return allowed_phot

class HostPhotometryViewSet(custom_viewsets.ListCreateRetrieveUpdateViewSet):
    serializer_class = HostPhotometrySerializer
    permission_classes = (permissions.IsAuthenticated,)
    lookup_field = "id"

    def get_queryset(self):
        allowed_phot = PhotometryService.GetAuthorizedHostPhotometry_ByUser(self.request.user)
        return allowed_phot

class TransientPhotDataViewSet(custom_viewsets.ListCreateRetrieveUpdateViewSet):
    serializer_class = TransientPhotDataSerializer
    permission_classes = (permissions.IsAuthenticated,)

    def get_queryset(self):
        allowed_phot_data = PhotometryService.GetAuthorizedTransientPhotData_ByUser(self.request.user)
        return allowed_phot_data


class HostPhotDataViewSet(custom_viewsets.ListCreateRetrieveUpdateViewSet):
    serializer_class = HostPhotDataSerializer
    permission_classes = (permissions.IsAuthenticated,)

    def get_queryset(self):
        allowed_phot_data = PhotometryService.GetAuthorizedHostPhotData_ByUser(self.request.user)
        return allowed_phot_data



class TransientImageViewSet(custom_viewsets.ListCreateRetrieveUpdateViewSet):
    queryset = TransientImage.objects.all()
    serializer_class = TransientImageSerializer
    permission_classes = (permissions.IsAuthenticated,)

class HostImageViewSet(custom_viewsets.ListCreateRetrieveUpdateViewSet):
    queryset = HostImage.objects.all()
    serializer_class = HostImageSerializer
    permission_classes = (permissions.IsAuthenticated,)

### `Photometric Band` ViewSets ###
class PhotometricBandViewSet(custom_viewsets.ListCreateRetrieveUpdateViewSet):
    queryset = PhotometricBand.objects.all()
    serializer_class = PhotometricBandSerializer
    lookup_field = "id"
    permission_classes = (permissions.IsAuthenticated,)

### `Principal Investigator` ViewSets ###
class PrincipalInvestigatorViewSet(custom_viewsets.ListCreateRetrieveUpdateViewSet):
    queryset = PrincipalInvestigator.objects.all()
    serializer_class = PrincipalInvestigatorSerializer
    permission_classes = (permissions.IsAuthenticated,)

### `Profile` ViewSets ###
class ProfileViewSet(custom_viewsets.ListCreateRetrieveUpdateViewSet):
    queryset = Profile.objects.all()
    serializer_class = ProfileSerializer
    permission_classes = (permissions.IsAuthenticated,)

    def get_queryset(self):
        qs = Profile.objects.all()
        user = self.request.user
        if user.is_staff or user.is_superuser:
            return qs
        return qs.filter(user=user)

### `UserQuery` ViewSets ###
class UserQueryViewSet(custom_viewsets.ListCreateRetrieveUpdateViewSet):
    queryset = UserQuery.objects.all()
    serializer_class = UserQuerySerializer
    permission_classes = (permissions.IsAuthenticated,)

### `UserTelescopeToFollow` ViewSets ###
class UserTelescopeToFollowViewSet(custom_viewsets.ListCreateRetrieveUpdateViewSet):
    queryset = UserTelescopeToFollow.objects.all()
    serializer_class = UserTelescopeToFollowSerializer
    permission_classes = (permissions.IsAuthenticated,)


### `Spectra` ViewSets ###
class TransientSpectrumViewSet(custom_viewsets.ListCreateRetrieveUpdateViewSet):
    serializer_class = TransientSpectrumSerializer
    permission_classes = (permissions.IsAuthenticated,)
    lookup_field = "id"

    def get_queryset(self):
        allowed_spec = SpectraService.GetAuthorizedTransientSpectrum_ByUser(self.request.user)
        return allowed_spec

class HostSpectrumViewSet(custom_viewsets.ListCreateRetrieveUpdateViewSet):
    serializer_class = HostSpectrumSerializer
    permission_classes = (permissions.IsAuthenticated,)
    lookup_field = "id"

    def get_queryset(self):
        allowed_spec = SpectraService.GetAuthorizedHostSpectrum_ByUser(self.request.user)
        return allowed_spec

class TransientSpecDataViewSet(custom_viewsets.ListCreateRetrieveUpdateViewSet):
    serializer_class = TransientSpecDataSerializer
    permission_classes = (permissions.IsAuthenticated,)

    def get_queryset(self):
        allowed_spec_data = SpectraService.GetAuthorizedTransientSpecData_ByUser(self.request.user)
        return allowed_spec_data

class HostSpecDataViewSet(custom_viewsets.ListCreateRetrieveUpdateViewSet):
    serializer_class = HostSpecDataSerializer
    permission_classes = (permissions.IsAuthenticated,)

    def get_queryset(self):
        allowed_spec_data = SpectraService.GetAuthorizedHostSpecData_ByUser(self.request.user)
        return allowed_spec_data

### `Telescope Resource` ViewSets ###
class ToOResourceViewSet(custom_viewsets.ListCreateRetrieveUpdateViewSet):
    serializer_class = ToOResourceSerializer
    permission_classes = (permissions.IsAuthenticated,)

    def get_queryset(self):
        allowed_resource = ObservingResourceService.GetAuthorizedToOResource_ByUser(self.request.user)
        return allowed_resource

class QueuedResourceViewSet(custom_viewsets.ListCreateRetrieveUpdateViewSet):
    serializer_class = QueuedResourceSerializer
    permission_classes = (permissions.IsAuthenticated,)

    def get_queryset(self):
        allowed_resource = ObservingResourceService.GetAuthorizedQueuedResource_ByUser(self.request.user)
        return allowed_resource

class AllocationViewSet(custom_viewsets.ListCreateRetrieveUpdateViewSet):
    """Allocations the user may see (#305): staff see all, others the active ones open to them; writes are staff-only."""

    serializer_class = AllocationSerializer
    permission_classes = (permissions.IsAuthenticated,)

    def get_queryset(self):
        from YSE_App.services.allocations import allocations_for_user

        user = self.request.user
        if user.is_staff or user.is_superuser:
            return Allocation.objects.select_related("telescope", "instrument", "principal_investigator", "credential")
        return allocations_for_user(user, facility_only=False)

    def _staff_only(self):
        user = self.request.user
        if not (user.is_staff or user.is_superuser):
            raise PermissionDenied({"message": "Only staff may create or edit allocations."})

    def perform_create(self, serializer):
        self._staff_only()
        super().perform_create(serializer)

    def perform_update(self, serializer):
        self._staff_only()
        super().perform_update(serializer)


class FacilityRequestViewSet(viewsets.ReadOnlyModelViewSet):
    """Facility requests (#299), read-only; ``?transient=<id>``, ``?allocation=<id>``, ``?state=`` filters."""

    serializer_class = FacilityRequestSerializer
    permission_classes = (permissions.IsAuthenticated,)

    def get_queryset(self):
        from YSE_App.services.allocations import allocations_for_user

        qs = FacilityRequest.objects.select_related("allocation", "transient", "submitted_by", "run")
        user = self.request.user
        if not (user.is_staff or user.is_superuser):
            qs = qs.filter(Q(submitted_by=user) | Q(allocation__in=allocations_for_user(user, facility_only=False)))
        for key in ("transient", "allocation", "state", "kind"):
            value = self.request.query_params.get(key)
            if value:
                qs = qs.filter(**{key if key in ("state", "kind") else key + "_id": value})
        return qs.distinct()


class FacilityViewSet(viewsets.ViewSet):
    """Registered facility adapters (#298): slug, capabilities, credential keys, request form schema."""

    permission_classes = (permissions.IsAuthenticated,)

    def _describe(self, slug):
        from YSE_App.facilities import get_facility

        adapter = get_facility(slug)
        if adapter is None:
            return None
        info = adapter.describe()
        info["fields"] = adapter.form_schema()
        return info

    def list(self, request):
        from YSE_App.facilities import registered_slugs

        return Response([self._describe(slug) for slug in registered_slugs()])

    def retrieve(self, request, pk=None):
        info = self._describe(pk)
        if info is None:
            raise Http404("no facility %r" % pk)
        return Response(info)


class AnalysisServiceViewSet(viewsets.ReadOnlyModelViewSet):
    """Analysis services (#313) the caller may run; ``param_fields`` is the parameter form, ``cap`` the daily-cap state."""

    serializer_class = AnalysisServiceSerializer
    permission_classes = (permissions.IsAuthenticated,)

    def get_queryset(self):
        from YSE_App.services.analysis_services import services_for_user

        return services_for_user(self.request.user, include_disabled=bool(self.request.user.is_staff))


class AnalysisRunViewSet(mixins.CreateModelMixin, mixins.RetrieveModelMixin, mixins.ListModelMixin,
                         mixins.DestroyModelMixin, viewsets.GenericViewSet):
    """Analysis runs (#313, #314): list (``?transient=<id>``, ``?service=<slug>``, ``?status=``), retrieve by uuid,
    create ``{"service": slug, "transient": id or name, "params": {...}}`` (429 when the daily cap is reached),
    delete own runs (staff: any)."""

    serializer_class = AnalysisRunSerializer
    permission_classes = (permissions.IsAuthenticated,)
    lookup_field = "uuid"
    lookup_value_regex = "[0-9a-fA-F-]{36}"

    def get_queryset(self):
        qs = (ExternalServiceRun.objects.filter(service__kind=ExternalService.KIND_ANALYSIS)
              .select_related("service", "service__analysis", "transient", "created_by").prefetch_related("files"))
        user = self.request.user
        if not (user.is_staff or user.is_superuser):
            visible = filter_transients_by_user_access(
                user, Transient.objects.filter(service_runs__service__kind=ExternalService.KIND_ANALYSIS).distinct())
            qs = qs.filter(Q(created_by=user) | Q(transient__in=visible))
        transient = self.request.query_params.get("transient")
        if transient:
            qs = qs.filter(Q(transient_id=transient) if str(transient).isdigit() else Q(transient__name=transient))
        service = self.request.query_params.get("service")
        if service:
            qs = qs.filter(service__slug=service)
        status_value = self.request.query_params.get("status")
        if status_value:
            qs = qs.filter(status=status_value)
        return qs.distinct()

    def create(self, request, *args, **kwargs):
        from YSE_App.analysis_views import user_may_see_transient
        from YSE_App.services import analysis_services as analysis_svc
        from YSE_App.services import external_services as runs

        data = AnalysisRunCreateSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        ref = str(data.validated_data["transient"])
        transient = Transient.objects.filter(Q(pk=int(ref)) if ref.isdigit() else Q(name=ref)).first()
        if transient is None or not user_may_see_transient(request.user, transient):
            return Response({"error": "unknown transient or not visible to you"}, status=status.HTTP_404_NOT_FOUND)
        profile = analysis_svc.services_for_user(request.user).filter(service__slug=data.validated_data["service"]).first()
        if profile is None:
            return Response({"error": "unknown analysis service or not available to you"}, status=status.HTTP_400_BAD_REQUEST)
        try:
            run = analysis_svc.start_analysis(profile, transient, request.user, data.validated_data.get("params") or {})
        except analysis_svc.InvalidParams as exc:
            return Response({"error": str(exc), "errors": exc.errors}, status=status.HTTP_400_BAD_REQUEST)
        except runs.RunLimitExceeded as exc:
            return Response({"error": str(exc), "limit": exc.limit}, status=status.HTTP_429_TOO_MANY_REQUESTS)
        except runs.ServiceDisabled as exc:
            return Response({"error": str(exc)}, status=status.HTTP_403_FORBIDDEN)
        run = self.get_queryset().get(pk=run.pk)
        return Response(self.get_serializer(run).data, status=status.HTTP_201_CREATED)

    def perform_destroy(self, instance):
        from YSE_App.services import analysis_services as analysis_svc

        if not analysis_svc.can_manage_run(instance, self.request.user):
            raise PermissionDenied("only the requester or staff may delete a run")
        analysis_svc.delete_run(instance)


class TransientAnnotationViewSet(viewsets.ModelViewSet):
    """Structured annotations (#317): one key/value document per transient and origin.

    List filters: ``?transient=<id|name>``, ``?origin=``, ``?key=`` (has that key), ``?verdict=``;
    with ``?transient=`` the read-only ``legacy`` entry (the annotation-like ``Transient`` columns)
    is appended unless ``?legacy=0``. Create is an upsert of ``{transient, origin, data, groups}``
    (``merge: true`` updates keys instead of replacing the document); update / delete work on one
    row. Staff write any origin; other users only ``user:<their username>``.
    """

    serializer_class = TransientAnnotationSerializer
    permission_classes = (permissions.IsAuthenticated,)

    def get_queryset(self):
        from YSE_App.services import annotations as annotations_svc

        user = self.request.user
        qs = (TransientAnnotation.objects.select_related("transient", "created_by", "modified_by", "service", "run")
              .prefetch_related("groups"))
        if not (user.is_staff or user.is_superuser):
            qs = qs.filter(Q(groups__isnull=True) | Q(groups__in=user.groups.all()))
            visible = filter_transients_by_user_access(user, Transient.objects.filter(annotations__isnull=False).distinct())
            qs = qs.filter(transient__in=visible)
        params = self.request.query_params
        transient = (params.get("transient") or "").strip()
        if transient:
            qs = qs.filter(transient_id=transient) if transient.isdigit() else qs.filter(transient__name=transient)
        origin = (params.get("origin") or "").strip()
        if origin:
            qs = qs.filter(origin=origin)
        key = (params.get("key") or "").strip()
        if key:
            qs = qs.filter(values__key=key)
        verdict = (params.get("verdict") or "").strip()
        if verdict:
            qs = qs.filter(values__key=annotations_svc.VERDICT_KEY, values__value_text=verdict)
        return qs.distinct().order_by("transient_id", "origin")

    def list(self, request, *args, **kwargs):
        from YSE_App.services import annotations as annotations_svc

        response = super().list(request, *args, **kwargs)
        transient = (request.query_params.get("transient") or "").strip()
        if not transient or request.query_params.get("legacy") in ("0", "false"):
            return response
        origin = (request.query_params.get("origin") or "").strip()
        if origin and origin != "legacy":
            return response
        obj = Transient.objects.filter(Q(pk=int(transient)) if transient.isdigit() else Q(name=transient)).first()
        if obj is None:
            return response
        legacy = annotations_svc.legacy_annotation(obj)
        if legacy is None:
            return response
        data = response.data
        if isinstance(data, dict) and isinstance(data.get("results"), list):
            data["results"].append(legacy)
            if isinstance(data.get("count"), int):
                data["count"] += 1
        elif isinstance(data, list):
            data.append(legacy)
        return response

    def _transient_for(self, request, ref):
        from YSE_App.analysis_views import user_may_see_transient

        ref = str(ref or "").strip()
        if not ref:
            return None
        transient = Transient.objects.filter(Q(pk=int(ref)) if ref.isdigit() else Q(name=ref)).first()
        if transient is None or not user_may_see_transient(request.user, transient):
            return None
        return transient

    def create(self, request, *args, **kwargs):
        from YSE_App.services import annotations as annotations_svc

        body = TransientAnnotationWriteSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        v = body.validated_data
        transient = self._transient_for(request, v.get("transient"))
        if transient is None:
            return Response({"error": "unknown transient or not visible to you"}, status=status.HTTP_404_NOT_FOUND)
        origin = (v.get("origin") or "").strip() or annotations_svc.user_origin(request.user)
        if not annotations_svc.can_write_origin(request.user, origin):
            raise PermissionDenied("you may only write annotations with origin %r" % annotations_svc.user_origin(request.user))
        try:
            annotation, created = annotations_svc.upsert(
                transient, origin, v.get("data") or {}, user=request.user,
                groups=v.get("groups") if "groups" in v else None, merge=bool(v.get("merge")),
            )
        except annotations_svc.AnnotationError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        annotation = self.get_queryset().get(pk=annotation.pk)
        return Response(self.get_serializer(annotation).data,
                        status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)

    def update(self, request, *args, **kwargs):
        from YSE_App.services import annotations as annotations_svc

        annotation = self.get_object()
        if not annotations_svc.can_manage(request.user, annotation):
            raise PermissionDenied("only staff or the owner may change this annotation")
        body = TransientAnnotationWriteSerializer(data=request.data, partial=True)
        body.is_valid(raise_exception=True)
        v = body.validated_data
        if v.get("origin") and v["origin"].strip() != annotation.origin:
            return Response({"error": "origin cannot be changed; create a new annotation"}, status=status.HTTP_400_BAD_REQUEST)
        merge = bool(v.get("merge")) or kwargs.get("partial", False)
        data = v.get("data") if "data" in v else (annotation.data or {})
        try:
            annotation, _ = annotations_svc.upsert(
                annotation.transient, annotation.origin, data, user=request.user,
                groups=v.get("groups") if "groups" in v else None, merge=merge,
            )
        except annotations_svc.AnnotationError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        annotation = self.get_queryset().get(pk=annotation.pk)
        return Response(self.get_serializer(annotation).data)

    def partial_update(self, request, *args, **kwargs):
        kwargs["partial"] = True
        return self.update(request, *args, **kwargs)

    def perform_destroy(self, instance):
        from YSE_App.services import annotations as annotations_svc

        if not annotations_svc.can_manage(self.request.user, instance):
            raise PermissionDenied("only staff or the owner may delete this annotation")
        annotations_svc.delete_annotation(instance)


### `ClassicalResource` Filter Set ###
class ClassicalResourceFilter(django_filters.FilterSet):
    telescope_name = django_filters.Filter(field_name="telescope__name")

    class Meta:
        model = ClassicalResource
        fields = ()

class ClassicalResourceViewSet(custom_viewsets.ListCreateRetrieveUpdateViewSet):
    serializer_class = ClassicalResourceSerializer
    permission_classes = (permissions.IsAuthenticated,)
    filter_backends = (DjangoFilterBackend,)
    filter_class = ClassicalResourceFilter

    def get_queryset(self):
        allowed_resource = ObservingResourceService.GetAuthorizedClassicalResource_ByUser(self.request.user)
        return allowed_resource

class ClassicalObservingDateViewSet(custom_viewsets.ListCreateRetrieveUpdateViewSet):
    serializer_class = ClassicalObservingDateSerializer
    permission_classes = (permissions.IsAuthenticated,)

    def get_queryset(self):
        allowed_resource = ObservingResourceService.GetAuthorizedClassicalObservingDate_ByUser(self.request.user)
        return allowed_resource

### `Telescope` ViewSets ###
class TelescopeViewSet(custom_viewsets.ListCreateRetrieveUpdateViewSet):
    queryset = Telescope.objects.all()
    serializer_class = TelescopeSerializer
    permission_classes = (permissions.IsAuthenticated,)

### `Transient` Filter Set ###
class TransientFilter(TransientSearchFilterSet):
    """``/api/transients/`` filters: the shared search FilterSet (#284).

    Every parameter of ``YSE_App.filters.transient_search`` works here,
    including the legacy names this class used to define itself
    (``created_date_gte``, ``modified_date_gte``, ``status_in``, ``ra_gte`` ...,
    ``tag_in``, ``name``, the ``peak_mag_lte`` family from #341 and
    ``ordering``). See docs/transient-search.md.
    """

    class Meta(TransientSearchFilterSet.Meta):
        pass

### `Transient` ViewSets ###
class TransientViewSet(custom_viewsets.ListCreateRetrieveUpdateViewSet):
    queryset = Transient.objects.all()
    serializer_class = TransientSerializer
    permission_classes = (permissions.IsAuthenticated,)
    filter_backends = (DjangoFilterBackend,)
    filterset_class = TransientFilter

    def get_queryset(self):
        qs = Transient.objects.all()
        return filter_transients_by_user_access(self.request.user, qs)

    @action(detail=False, methods=['post'], url_path='save_search')
    def save_search(self, request):
        """Save a search as an Explorer query (``POST /api/transients/save_search/``, #287).

        Body (JSON or form): ``title`` (required), ``add_to_dashboard`` (bool,
        default false) and the search filters either as ``params`` (an object
        of filter name -> value or list of values, the same names as the list
        endpoint) or as ``query_string`` (``status=New&has_spectrum=true``).
        Returns the query id, its Explorer URL, the compiled SQL and, when
        attached, the personal-dashboard section id.
        """
        from django.http import QueryDict
        from YSE_App.services.search_queries import SearchSaveError, save_search_query

        data = request.data
        params = data.get('params')
        if params is None:
            params = QueryDict(str(data.get('query_string', '')), mutable=True)
        add = data.get('add_to_dashboard', False)
        if isinstance(add, str):
            add = add.lower() in ('1', 'true', 'on', 'yes')
        try:
            result = save_search_query(
                request.user, params, data.get('title', ''), add_to_dashboard=bool(add), request=request,
            )
        except SearchSaveError as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        query = result['query']
        user_query = result['user_query']
        return Response({
            'query_id': query.id,
            'title': query.title,
            'explorer_url': request.build_absolute_uri(reverse('query_detail', args=[query.id])),
            'sql': result['sql'],
            'user_query_id': user_query.id if user_query is not None else None,
            'dashboard_url': request.build_absolute_uri(reverse('personaldashboard')),
        }, status=status.HTTP_201_CREATED)

    # --- AI summaries (#295, #296) -------------------------------------------------
    def _summary_transient(self, request, pk):
        from YSE_App.services import summaries as summaries_svc

        transient = self.get_object()
        if not summaries_svc.can_view(request.user, transient):
            raise Http404("no such transient")
        return transient

    def _summary_payload(self, transient):
        from YSE_App.services import summaries as summaries_svc

        version = summaries_svc.current_version(transient)
        run = summaries_svc.latest_run(transient)
        return {
            'transient': transient.pk,
            'transient_name': transient.name,
            'summary': transient.summary or '',
            'summary_modified': transient.summary_modified,
            'current': TransientSummaryVersionSerializer(version).data if version is not None else None,
            'history': TransientSummaryVersionSerializer(summaries_svc.history(transient), many=True).data,
            'run': {'uuid': str(run.uuid), 'status': run.status, 'error': run.error,
                    'finished_at': run.finished_at} if run is not None else None,
        }

    @action(detail=True, methods=['get', 'patch', 'put'], url_path='summary')
    def summary(self, request, pk=None):
        """``GET`` the summary with its history; ``PATCH {text}`` stores a human version (#295)."""
        from YSE_App.services import summaries as summaries_svc

        transient = self._summary_transient(request, pk)
        if request.method == 'GET':
            return Response(self._summary_payload(transient))
        if not summaries_svc.can_edit(request.user, transient):
            raise PermissionDenied("you may not edit this summary")
        body = TransientSummaryWriteSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        try:
            summaries_svc.set_summary(transient, body.validated_data['text'], request.user,
                                      source=summaries_svc.SOURCE_HUMAN)
        except summaries_svc.SummaryError as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(self._summary_payload(transient))

    @action(detail=True, methods=['post'], url_path='summary/generate')
    def summary_generate(self, request, pk=None):
        """Queue a summariser run (#296): 403 without the opt-in / group, 409 while one runs, 429 at the cap."""
        from YSE_App.services import external_services as runs_svc
        from YSE_App.services import summaries as summaries_svc

        transient = self._summary_transient(request, pk)
        service = summaries_svc.service_row()
        if service is None:
            return Response({'detail': 'the AI summary service is not registered or is disabled'},
                            status=status.HTTP_404_NOT_FOUND)
        if not summaries_svc.can_generate(request.user, transient, service):
            raise PermissionDenied("AI summaries are not enabled for you (opt in on a transient page) or your groups")
        if summaries_svc.active_run(transient) is not None:
            return Response({'detail': 'a summary is already being generated'}, status=status.HTTP_409_CONFLICT)
        try:
            run = summaries_svc.request_summary(transient, request.user, service=service)
        except runs_svc.RunLimitExceeded as exc:
            return Response({'detail': str(exc), 'limit': exc.limit}, status=status.HTTP_429_TOO_MANY_REQUESTS)
        except (runs_svc.ServiceDisabled, summaries_svc.SummaryError) as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_403_FORBIDDEN)
        run = ExternalServiceRun.objects.get(pk=run.pk)
        return Response({'run': str(run.uuid), 'status': run.status, 'error': run.error},
                        status=status.HTTP_202_ACCEPTED)


class SummarySearchAPIView(generics.GenericAPIView):
    """``GET /api/summary_search/?q=...&limit=N``: transients ranked by their summary (#297)."""

    permission_classes = (permissions.IsAuthenticated,)

    def get(self, request):
        from YSE_App.services import summaries as summaries_svc

        try:
            limit = int(request.GET.get('limit', 0) or 0)
        except (TypeError, ValueError):
            limit = 0
        result = summaries_svc.search_summaries(request.GET.get('q', ''), request.user, limit=limit or None)
        return Response({
            'query': result['query'],
            'mode': result['mode'],
            'model': result['model'],
            'results': [{
                'transient': r['transient'].pk,
                'name': r['transient'].name,
                'slug': r['transient'].slug,
                'status': str(r['transient'].status),
                'spec_class': str(r['transient'].best_spec_class) if r['transient'].best_spec_class_id else None,
                'redshift': r['transient'].redshift,
                'score': r['score'],
                'snippet': r['snippet'],
                'summary_modified': r['transient'].summary_modified,
            } for r in result['results']],
        })


### `TransientPhotStat` (per-transient photometry statistics, #268) ###
class TransientPhotStatFilter(django_filters.FilterSet):
    transient_name = django_filters.CharFilter(field_name="transient__name")
    peak_mag_lte = django_filters.NumberFilter(field_name="peak_mag", lookup_expr='lte')
    peak_mag_gte = django_filters.NumberFilter(field_name="peak_mag", lookup_expr='gte')
    last_det_mag_lte = django_filters.NumberFilter(field_name="last_detected_mag", lookup_expr='lte')
    last_det_mag_gte = django_filters.NumberFilter(field_name="last_detected_mag", lookup_expr='gte')
    last_det_mjd_gte = django_filters.NumberFilter(field_name="last_detected_mjd", lookup_expr='gte')
    last_det_mjd_lte = django_filters.NumberFilter(field_name="last_detected_mjd", lookup_expr='lte')
    first_det_mjd_gte = django_filters.NumberFilter(field_name="first_detected_mjd", lookup_expr='gte')
    first_det_mjd_lte = django_filters.NumberFilter(field_name="first_detected_mjd", lookup_expr='lte')
    num_det_gte = django_filters.NumberFilter(field_name="num_det_global", lookup_expr='gte')
    rise_rate_gte = django_filters.NumberFilter(field_name="rise_rate", lookup_expr='gte')
    decay_rate_gte = django_filters.NumberFilter(field_name="decay_rate", lookup_expr='gte')
    deepest_limit_gte = django_filters.NumberFilter(field_name="deepest_limit", lookup_expr='gte')
    deepest_limit_band = django_filters.CharFilter(field_name="deepest_limit_band__name")
    last_non_detection_band = django_filters.CharFilter(field_name="last_non_detection_band__name")
    num_limits_gte = django_filters.NumberFilter(field_name="num_limits_global", lookup_expr='gte')
    ordering = django_filters.OrderingFilter(
        fields=(
            'peak_mag', 'peak_mjd', 'last_detected_mag', 'last_detected_mjd',
            'first_detected_mjd', 'num_det_global', 'num_obs_global',
            'rise_rate', 'decay_rate', 'last_updated', 'deepest_limit',
        )
    )

    class Meta:
        model = TransientPhotStat
        fields = ('transient_name',)


class TransientPhotStatViewSet(viewsets.ReadOnlyModelViewSet):
    """Read-only ``/api/transientphotstats/`` rows for the transients the user may see."""
    serializer_class = TransientPhotStatSerializer
    permission_classes = (permissions.IsAuthenticated,)
    filter_backends = (DjangoFilterBackend,)
    filter_class = TransientPhotStatFilter

    def get_queryset(self):
        allowed = filter_transients_by_user_access(self.request.user, Transient.objects.all())
        return (
            TransientPhotStat.objects.filter(transient__in=allowed.values('pk'))
            .select_related('transient', 'first_detected_band', 'last_detected_band', 'peak_band',
                            'deepest_limit_band', 'last_non_detection_band')
            .order_by('-last_updated', '-pk')
        )


class AlternateTransientNamesViewSet(custom_viewsets.ListCreateRetrieveUpdateViewSet):
    queryset = AlternateTransientNames.objects.all()
    serializer_class = AlternateTransientNamesSerializer
    permission_classes = (permissions.IsAuthenticated,)

### `User` ViewSets ###
class UserViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = User.objects.all()
    serializer_class = UserSerializer
    permission_classes = (permissions.IsAuthenticated,)

    def get_queryset(self):
        qs = User.objects.all()
        user = self.request.user
        if user.is_staff or user.is_superuser:
            return qs
        return qs.filter(pk=user.pk)

### `Group` ViewSets ###
class GroupViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Group.objects.all()
    serializer_class = GroupSerializer
    permission_classes = (permissions.IsAuthenticated,)

    def get_queryset(self):
        qs = Group.objects.all()
        user = self.request.user
        if user.is_staff or user.is_superuser:
            return qs
        return qs.filter(pk__in=user.groups.values_list("pk", flat=True))

### `Tag` ViewSets ###
class TransientTagViewSet(custom_viewsets.ListCreateRetrieveUpdateViewSet):
    queryset = TransientTag.objects.all()
    serializer_class = TransientTagSerializer
    permission_classes = (permissions.IsAuthenticated,)

### `GW` ViewSets ###
class GWCandidateViewSet(custom_viewsets.ListCreateRetrieveUpdateViewSet):
    queryset = GWCandidate.objects.all()
    serializer_class = GWCandidateSerializer
    permission_classes = (permissions.IsAuthenticated,)

class GWCandidateImageViewSet(custom_viewsets.ListCreateRetrieveUpdateViewSet):
    queryset = GWCandidateImage.objects.all()
    serializer_class = GWCandidateImageSerializer
    permission_classes = (permissions.IsAuthenticated,)


class TransientCommentListCreate(generics.ListCreateAPIView):
    """List/create transient-level comments (Log rows, not follow-up comments)."""

    permission_classes = (permissions.IsAuthenticated,)
    serializer_class = None  # set in get_serializer_class

    def get_serializer_class(self):
        from YSE_App.serializers.transient_comment_serializers import (
            TransientCommentSerializer,
        )

        return TransientCommentSerializer

    def get_queryset(self):
        from YSE_App.services.comments import transient_comment_queryset
        from YSE_App.services.visibility import user_can_view_transient

        transient_id = int(self.kwargs["transient_id"])
        if not user_can_view_transient(self.request.user, transient_id):
            return Log.objects.none()
        return transient_comment_queryset(transient_id, user=self.request.user)

    def create(self, request, *args, **kwargs):
        from YSE_App.services.audience import resolve_comment_audience
        from YSE_App.services.comments import create_transient_comment

        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        transient = serializer.validated_data["transient"]
        is_public, audience_groups = resolve_comment_audience(
            request.user,
            transient.id,
            is_public=serializer.validated_data.get("is_public", False),
            audience_group_ids=serializer.validated_data.get("audience_group_ids"),
        )
        log = create_transient_comment(
            transient=transient,
            comment=serializer.validated_data["comment"],
            user=request.user,
            is_public=is_public,
            audience_groups=audience_groups,
        )
        return Response(
            self.get_serializer(log).data,
            status=status.HTTP_201_CREATED,
        )


### Broker providers, filters and candidates (#272 / #276) ###
class BrokerViewSet(viewsets.ViewSet):
    """``/api/brokers/``: enabled providers with their capabilities; ``/api/brokers/<slug>/``
    one provider; ``?ra=&dec=&radius=`` on ``/api/brokers/<slug>/cone_search/`` proxies a cone search."""
    permission_classes = (permissions.IsAuthenticated,)

    def list(self, request):
        from YSE_App.brokers import registry
        from YSE_App.brokers.filters import describe_criteria
        return Response({"brokers": BrokerSerializer(registry.describe_all(), many=True).data,
                         "criteria": describe_criteria()})

    def retrieve(self, request, pk=None):
        from YSE_App.brokers import registry
        provider = registry.get_provider(pk)
        if provider is None:
            return Response({"detail": "unknown or disabled broker"}, status=status.HTTP_404_NOT_FOUND)
        return Response(BrokerSerializer(provider.describe()).data)

    @action(detail=True, methods=["get"])
    def cone_search(self, request, pk=None):
        from YSE_App.brokers import registry
        from YSE_App.brokers.base import CONE_SEARCH, BrokerError, BrokerUnavailable
        provider = registry.get_provider(pk)
        if provider is None:
            return Response({"detail": "unknown or disabled broker"}, status=status.HTTP_404_NOT_FOUND)
        if not provider.has(CONE_SEARCH):
            return Response({"detail": "%s has no cone search" % pk}, status=status.HTTP_400_BAD_REQUEST)
        try:
            ra, dec = float(request.query_params["ra"]), float(request.query_params["dec"])
            radius = float(request.query_params.get("radius", 5.0))
            limit = min(int(request.query_params.get("limit", 20)), 200)
        except (KeyError, ValueError):
            return Response({"detail": "ra, dec (deg) and optional radius (arcsec) are required"},
                            status=status.HTTP_400_BAD_REQUEST)
        try:
            alerts = provider.cone_search(ra, dec, radius, limit=limit)
        except BrokerUnavailable as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        except BrokerError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        return Response({"broker": pk, "count": len(alerts), "results": [a.to_dict() for a in alerts]})


class BrokerFilterViewSet(viewsets.ModelViewSet):
    """``/api/brokerfilters/``: filters of the user's groups (staff: all); group-scoped writes."""
    serializer_class = BrokerFilterSerializer
    permission_classes = (permissions.IsAuthenticated,)

    def get_queryset(self):
        user = self.request.user
        qs = BrokerFilter.objects.select_related("group").order_by("broker", "name")
        if user.is_staff or user.is_superuser:
            return qs
        return qs.filter(Q(group__isnull=True) | Q(group__in=user.groups.all()))

    def _check_group(self, group):
        user = self.request.user
        if user.is_staff or user.is_superuser:
            return
        if group is None:
            raise PermissionDenied("only staff may create filters without a group")
        if not user.groups.filter(pk=group.pk).exists():
            raise PermissionDenied("you are not a member of that group")

    def perform_create(self, serializer):
        self._check_group(serializer.validated_data.get("group"))
        serializer.save(created_by=self.request.user)

    def perform_update(self, serializer):
        self._check_group(serializer.instance.group)
        if "group" in serializer.validated_data:
            self._check_group(serializer.validated_data.get("group"))
        serializer.save()

    def perform_destroy(self, instance):
        self._check_group(instance.group)
        instance.delete()


class CandidateViewSet(viewsets.ReadOnlyModelViewSet):
    """``/api/candidates/`` (read) with ``save`` / ``reject`` / ``reopen`` POST actions."""
    serializer_class = CandidateSerializer
    permission_classes = (permissions.IsAuthenticated,)
    filter_backends = (DjangoFilterBackend,)
    filterset_fields = ("broker", "status", "alert_id")

    def get_queryset(self):
        from YSE_App.candidate_views import visible_filters
        allowed = visible_filters(self.request.user)
        return (
            Candidate.objects.filter(Q(filters__in=allowed) | Q(filters__isnull=True)).distinct()
            .select_related("transient").prefetch_related("filters").order_by("-last_seen", "-id")
        )

    def _act(self, request, pk, fn):
        from YSE_App.brokers.base import BrokerError
        from YSE_App.brokers.ingest import IngestError
        candidate = self.get_object()
        try:
            fn(candidate)
        except (IngestError, BrokerError) as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        candidate.refresh_from_db()
        return Response(self.get_serializer(candidate).data)

    @action(detail=True, methods=["post"])
    def save(self, request, pk=None):
        from YSE_App.brokers import ingest
        data = request.data or {}
        return self._act(request, pk, lambda c: ingest.save_candidate(
            c, request.user, status=data.get("status") or "New", obs_group=data.get("obs_group") or None,
            import_photometry=data.get("import_photometry", True) not in (False, "0", "false", 0)))

    @action(detail=True, methods=["post"])
    def reject(self, request, pk=None):
        from YSE_App.brokers import ingest
        return self._act(request, pk, lambda c: ingest.reject_candidate(c, request.user, note=str((request.data or {}).get("note") or "")[:255]))

    @action(detail=True, methods=["post"])
    def reopen(self, request, pk=None):
        from YSE_App.brokers import ingest
        return self._act(request, pk, lambda c: ingest.reopen_candidate(c, request.user))


### Transient interests (#289) and data access requests (#292) ###
class TransientInterestViewSet(custom_viewsets.ListCreateRetrieveUpdateViewSet):
    """Interests (intentions to publish) on transients the user may see.

    Filters: ``?transient=<id|name>``, ``?user=<id|username>``, ``?mine=1``, ``?status=``;
    withdrawn rows are left out unless ``?include_withdrawn=1``. Creating posts the
    automatic comment; only the owner (or staff) may update, and a status change
    goes through the service so withdrawn / published post their comment too.
    """

    serializer_class = TransientInterestSerializer
    permission_classes = (permissions.IsAuthenticated,)

    def get_queryset(self):
        from YSE_App.services.visibility import filter_transients_by_user_access

        params = self.request.query_params
        qs = TransientInterest.objects.select_related("transient", "user", "group")
        if params.get("include_withdrawn") not in ("1", "true"):
            qs = qs.filter(status__in=TransientInterest.VISIBLE_STATUSES)
        transient = (params.get("transient") or "").strip()
        if transient:
            qs = qs.filter(transient_id=transient) if transient.isdigit() else qs.filter(transient__name=transient)
        user = (params.get("user") or "").strip()
        if user:
            qs = qs.filter(user_id=user) if user.isdigit() else qs.filter(user__username=user)
        if params.get("mine") in ("1", "true"):
            qs = qs.filter(user=self.request.user)
        status_value = (params.get("status") or "").strip()
        if status_value:
            qs = qs.filter(status=status_value)
        if not (self.request.user.is_staff or self.request.user.is_superuser):
            visible = filter_transients_by_user_access(
                self.request.user, Transient.objects.filter(id__in=qs.values_list("transient_id", flat=True)),
            )
            qs = qs.filter(transient_id__in=visible.values_list("id", flat=True))
        return qs

    def perform_create(self, serializer):
        from YSE_App.services.interests import register_interest

        data = serializer.validated_data
        serializer.instance = register_interest(
            data["transient"], self.request.user, data.get("title", ""), group=data.get("group"),
            role=data.get("role") or TransientInterest.ROLE_LEAD, description=data.get("description") or "",
            status=data.get("status") or TransientInterest.STATUS_PLANNED,
        )

    def _update(self, serializer):
        from YSE_App.services.interests import update_interest_status, user_can_edit_interest

        interest = serializer.instance
        if not user_can_edit_interest(self.request.user, interest):
            raise PermissionDenied({"message": "Only the person who registered the interest (or staff) may change it."})
        data = dict(serializer.validated_data)
        for key in ("transient", "user"):
            data.pop(key, None)
        new_status = data.pop("status", None)
        doi = data.pop("doi", None)
        for key, value in data.items():
            setattr(interest, key, value)
        interest.modified_by = self.request.user
        interest.save()
        if new_status is not None or doi is not None:
            update_interest_status(interest, self.request.user, new_status or interest.status, doi=doi)

    def perform_update(self, serializer):
        self._update(serializer)

    def perform_partial_update(self, serializer):
        self._update(serializer)


class FavoriteTransientViewSet(mixins.CreateModelMixin, mixins.RetrieveModelMixin, mixins.ListModelMixin,
                               mixins.DestroyModelMixin, viewsets.GenericViewSet):
    """The requesting user's favorite transients (#323): ``GET`` lists, ``POST {"transient": id}`` stars,
    ``DELETE /api/favorites/<id>/`` unstars; ``?transient=<id|name>`` narrows the list.
    ``POST /api/favorites/toggle/ {"transient": id}`` flips the star and answers ``{favorite, count}``."""

    serializer_class = FavoriteTransientSerializer
    permission_classes = (permissions.IsAuthenticated,)

    def get_queryset(self):
        qs = UserFavoriteTransient.objects.filter(user=self.request.user).select_related("transient")
        transient = (self.request.query_params.get("transient") or "").strip()
        if transient:
            qs = qs.filter(transient_id=transient) if transient.isdigit() else qs.filter(transient__name=transient)
        return qs

    def _check_visible(self, transient):
        from YSE_App.services.visibility import user_can_view_transient

        if not user_can_view_transient(self.request.user, transient.id):
            raise PermissionDenied({"message": "You do not have access to this transient."})

    def perform_create(self, serializer):
        from YSE_App.services import favorites as favorites_svc

        transient = serializer.validated_data["transient"]
        self._check_visible(transient)
        serializer.instance, _created = favorites_svc.add(self.request.user, transient)

    @action(detail=False, methods=["post"])
    def toggle(self, request):
        from YSE_App.services import favorites as favorites_svc

        transient_id = request.data.get("transient")
        try:
            transient = Transient.objects.get(pk=int(transient_id))
        except (TypeError, ValueError, Transient.DoesNotExist):
            return Response({"transient": ["Give the id of an existing transient."]}, status=status.HTTP_400_BAD_REQUEST)
        self._check_visible(transient)
        state = favorites_svc.toggle(request.user, transient)
        _mine, count = favorites_svc.favorite_state(request.user, transient.id)
        return Response({"transient": transient.id, "favorite": state, "count": count})


class NotificationViewSet(mixins.RetrieveModelMixin, mixins.ListModelMixin, viewsets.GenericViewSet):
    """The requesting user's in-app notifications (#321): ``GET /api/notifications/?unread=1&kind=``,
    ``GET .../unread_count/``, ``POST .../<id>/read/``, ``POST .../read_all/``."""

    serializer_class = NotificationSerializer
    permission_classes = (permissions.IsAuthenticated,)

    def get_queryset(self):
        params = self.request.query_params
        qs = Notification.objects.filter(recipient=self.request.user).select_related("transient")
        if params.get("unread") in ("1", "true"):
            qs = qs.filter(read_at__isnull=True)
        kind = (params.get("kind") or "").strip()[:32]
        if kind:
            qs = qs.filter(kind=kind)
        transient = (params.get("transient") or "").strip()
        if transient:
            qs = qs.filter(transient_id=transient) if transient.isdigit() else qs.filter(transient__name=transient)
        return qs

    @action(detail=False, methods=["get"])
    def unread_count(self, request):
        from YSE_App.services import notify as notify_svc

        return Response({"unread": notify_svc.unread_count(request.user)})

    @action(detail=True, methods=["post"])
    def read(self, request, pk=None):
        notification = self.get_object()
        notification.mark_read()
        return Response(self.get_serializer(notification).data)

    @action(detail=False, methods=["post"])
    def read_all(self, request):
        from YSE_App.services import notify as notify_svc

        return Response({"marked": notify_svc.mark_all_read(request.user), "unread": 0})


class DataAccessRequestViewSet(mixins.CreateModelMixin, mixins.RetrieveModelMixin, mixins.ListModelMixin,
                               viewsets.GenericViewSet):
    """Data access requests: the user's own and those addressed to their groups (staff: all).

    Filters ``?box=mine|inbox``, ``?status=``, ``?transient=``, ``?kind=``. ``POST`` creates
    (``transient``, ``dataset_kind``, ``owner_group``, optional ``target_group`` / ``message`` /
    ``dataset_id``); ``POST .../<id>/accept/`` and ``.../decline/`` (optional ``note``) decide.
    """

    serializer_class = DataAccessRequestSerializer
    permission_classes = (permissions.IsAuthenticated,)

    def get_queryset(self):
        from YSE_App.services import data_access as dar

        user = self.request.user
        params = self.request.query_params
        box = params.get("box") or ""
        if box == "mine":
            qs = dar.requests_by(user)
        elif box == "inbox":
            qs = dar.requests_to_decide(user, pending_only=False)
        else:
            qs = DataAccessRequest.objects.select_related(
                "requester", "transient", "owner_group", "target_group", "decided_by")
            if not (user.is_staff or user.is_superuser):
                qs = qs.filter(Q(requester=user) | Q(owner_group__in=user.groups.all()))
        return dar.filter_requests(qs, params).distinct()

    def perform_create(self, serializer):
        from YSE_App.services import data_access as dar

        data = serializer.validated_data
        serializer.instance = dar.request_access(
            self.request.user, data["transient"], data.get("dataset_kind", ""), data["owner_group"],
            target_group=data.get("target_group"), message=data.get("message") or "",
            dataset_id=data.get("dataset_id"),
        )

    def _decide(self, request, accept):
        from YSE_App.services import data_access as dar

        obj = self.get_object()
        dar.decide(obj, request.user, accept, note=request.data.get("note", "") if hasattr(request.data, "get") else "")
        return Response(self.get_serializer(obj).data)

    @action(detail=True, methods=["post"])
    def accept(self, request, pk=None):
        return self._decide(request, True)

    @action(detail=True, methods=["post"])
    def decline(self, request, pk=None):
        return self._decide(request, False)
class SharingServiceViewSet(viewsets.ReadOnlyModelViewSet):
    """Read-only ``/api/sharingservices/``: the enabled services the user may report through (no secrets)."""
    serializer_class = SharingServiceSerializer
    permission_classes = (permissions.IsAuthenticated,)

    def get_queryset(self):
        visible = [s.pk for s in SharingService.for_user(self.request.user)]
        return SharingService.objects.filter(pk__in=visible).prefetch_related(
            'allowed_instruments', 'allowed_obs_groups', 'groups')


class SharingSubmissionViewSet(viewsets.ReadOnlyModelViewSet):
    """Read-only ``/api/sharingsubmissions/`` for transients the user may see; filter by ``status``, ``kind``, ``service``."""
    serializer_class = SharingSubmissionSerializer
    permission_classes = (permissions.IsAuthenticated,)

    def get_queryset(self):
        allowed = filter_transients_by_user_access(self.request.user, Transient.objects.all())
        qs = SharingSubmission.objects.filter(transient__in=allowed.values('pk')).select_related(
            'service', 'transient', 'created_by')
        params = self.request.query_params
        if params.get('status'):
            qs = qs.filter(status=params['status'])
        if params.get('kind'):
            qs = qs.filter(kind=params['kind'])
        if params.get('service'):
            qs = qs.filter(service__slug=params['service'])
        if params.get('transient'):
            qs = qs.filter(transient__name=params['transient'])
        return qs


class InstrumentLogViewSet(mixins.CreateModelMixin, mixins.RetrieveModelMixin, mixins.ListModelMixin,
                           viewsets.GenericViewSet):
    """``/api/instrumentlogs/`` (#310): list / retrieve for every authenticated user; POST for staff or accounts
    holding ``YSE_App.add_instrumentlog`` (facility service accounts; ``Authorization: Token <key>`` accepted).
    Filters: ``instrument`` (id), ``telescope`` (id), ``start_after``, ``end_before`` (ISO), ``source``."""

    serializer_class = InstrumentLogSerializer
    permission_classes = (permissions.IsAuthenticated,)

    def get_authenticators(self):
        from rest_framework.authentication import TokenAuthentication

        return super().get_authenticators() + [TokenAuthentication()]

    def get_queryset(self):
        from YSE_App.services.instrument_logs import logs_between

        params = self.request.query_params
        qs = logs_between(start=params.get("start_after") or None, end=params.get("end_before") or None,
                          source=params.get("source") or "")
        if params.get("instrument"):
            qs = qs.filter(instrument_id=params["instrument"])
        if params.get("telescope"):
            qs = qs.filter(instrument__telescope_id=params["telescope"])
        return qs

    def create(self, request, *args, **kwargs):
        from YSE_App.services.instrument_logs import add_log, can_add_logs

        if not can_add_logs(request.user):
            raise PermissionDenied({"message": "Only staff or accounts with the add_instrumentlog permission may post logs."})
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        row, created = add_log(
            data["instrument"], request.user, start=data.get("start"), end=data.get("end"),
            message=data.get("message", ""), entries=data.get("log"),
            source=data.get("source") or InstrumentLog.SOURCE_MANUAL, source_name=data.get("source_name", ""),
        )
        out = self.get_serializer(row)
        return Response(out.data, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)


class FeedSourceViewSet(viewsets.ReadOnlyModelViewSet):
    """``/api/feedsources/`` (#280): the configured Hermes / Einstein Probe / Scout feeds and their poll status.
    Disabled sources are listed for staff only. ``POST /api/feedsources/<id>/poll/`` (staff) queues a poll."""

    serializer_class = FeedSourceSerializer
    permission_classes = (permissions.IsAuthenticated,)
    filter_backends = (DjangoFilterBackend,)
    filterset_fields = ("kind", "enabled", "slug")

    def get_queryset(self):
        qs = FeedSource.objects.select_related("credential").order_by("kind", "name")
        if not (self.request.user.is_staff or self.request.user.is_superuser):
            qs = qs.filter(enabled=True)
        return qs

    def get_serializer_context(self):
        from YSE_App.feed_views import candidate_counts_by_kind

        context = super().get_serializer_context()
        context["candidate_counts"] = candidate_counts_by_kind()
        return context

    @action(detail=True, methods=["post"])
    def poll(self, request, pk=None):
        from YSE_App.feeds.jobs import enqueue_poll, poll_active

        if not (request.user.is_staff or request.user.is_superuser):
            return Response({"detail": "staff only"}, status=status.HTTP_403_FORBIDDEN)
        source = self.get_object()
        if poll_active(source):
            return Response({"detail": "a poll of %s is already queued or running" % source.slug},
                            status=status.HTTP_409_CONFLICT)
        job = enqueue_poll(source, created_by=request.user, force=True,
                           dry_run=str((request.data or {}).get("dry_run", "")).lower() in ("1", "true", "yes"))
        return Response({"job_id": job.pk, "source": source.slug, "status": job.status}, status=status.HTTP_202_ACCEPTED)

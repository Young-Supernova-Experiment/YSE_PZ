from django.contrib import admin
from django import forms

# Register your models here.
from YSE_App.models import *

class QueryModelChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        return obj.__unicode__

class QueryModelForm(forms.ModelForm):
	query = QueryModelChoiceField(Query.objects.all())
	
	class Meta:
		model = UserQuery
		fields = ('created_by','modified_by','user')
		
@admin.register(UserQuery)
class UserQueryAdmin(admin.ModelAdmin):
	form = QueryModelForm
	
admin.site.register(TransientStatus)
admin.site.register(FollowupStatus)
admin.site.register(TaskStatus)
admin.site.register(AntaresClassification)
admin.site.register(InternalSurvey)
admin.site.register(ObservationGroup)
admin.site.register(SEDType)
admin.site.register(HostMorphology)
admin.site.register(Phase)
admin.site.register(TransientClass)
admin.site.register(Observatory)
admin.site.register(OnCallDate)
admin.site.register(YSEOnCallDate)


@admin.register(Telescope)
class TelescopeAdmin(admin.ModelAdmin):
	list_display = ("name", "observatory", "latitude", "longitude", "elevation", "has_weather_widget")
	search_fields = ("name", "observatory__name")
	readonly_fields = ("weather", "weather_fetched_at", "created_by", "created_date", "modified_by", "modified_date")
	fieldsets = (
		(None, {"fields": ("observatory", "name", "latitude", "longitude", "elevation")}),
		("Weather widget and SkyCam (#311)", {
			"fields": ("weather_url", "weather_link", "skycam_url", "weather", "weather_fetched_at"),
			"description": "weather_url: JSON endpoint (Open-Meteo or OpenWeatherMap shape; {lat}/{lon}/{elevation} "
			               "placeholders). weather_link: the site's own weather page. skycam_url: all-sky image.",
		}),
		("Audit", {"fields": ("created_by", "created_date", "modified_by", "modified_date"), "classes": ("collapse",)}),
	)

	def save_model(self, request, obj, form, change):
		if not change or not obj.created_by_id:
			obj.created_by = request.user
		obj.modified_by = request.user
		super().save_model(request, obj, form, change)


@admin.register(InstrumentLog)
class InstrumentLogAdmin(admin.ModelAdmin):
	list_display = ("id", "instrument", "start", "end", "source", "source_name", "short_message", "entry_count", "created_by")
	list_filter = ("source", "instrument__telescope")
	search_fields = ("message", "instrument__name", "instrument__telescope__name")
	raw_id_fields = ("instrument", "run")
	readonly_fields = ("fingerprint", "created_by", "created_date", "modified_by", "modified_date")
	date_hierarchy = "start"
	list_select_related = ("instrument", "instrument__telescope", "created_by")

	def short_message(self, obj):
		return (obj.summary or "")[:80]
	short_message.short_description = "message"

	def save_model(self, request, obj, form, change):
		from YSE_App.services.instrument_logs import fingerprint, normalize_entries

		if not change or not obj.created_by_id:
			obj.created_by = request.user
		obj.modified_by = request.user
		obj.fingerprint = fingerprint(obj.instrument_id, obj.start, obj.message, normalize_entries(obj.log))
		super().save_model(request, obj, form, change)

admin.site.register(Instrument)


class ObservingResourceAdmin(admin.ModelAdmin):
	list_display = ("__str__", "creator_only", "begin_date_valid", "end_date_valid", "allocation")
	list_filter = ("creator_only",)
	filter_horizontal = ("groups",)
	raw_id_fields = ("allocation",)


admin.site.register(ToOResource, ObservingResourceAdmin)
admin.site.register(ClassicalResource, ObservingResourceAdmin)
admin.site.register(QueuedResource, ObservingResourceAdmin)
admin.site.register(ClassicalObservingDate)
admin.site.register(InstrumentConfig)
admin.site.register(ConfigElement)
admin.site.register(PhotometricBand)


class DataSharingDelegateInline(admin.TabularInline):
	"""The PI's deputies (#447): who may share this PI's private data (all of it, or one group's)."""
	model = DataSharingDelegate
	fk_name = 'pi'
	extra = 0
	raw_id_fields = ('delegate',)
	fields = ('delegate', 'group', 'note', 'created_by', 'created_date')
	readonly_fields = ('created_by', 'created_date')


@admin.register(PrincipalInvestigator)
class PrincipalInvestigatorAdmin(admin.ModelAdmin):
	"""PIs (#446): ``user`` links a PI to their YSE-PZ account (notifications, data-access decisions)."""
	list_display = ('name', 'institution', 'user')
	search_fields = ('name', 'institution')
	raw_id_fields = ('user',)
	filter_horizontal = ('obs_group',)
	inlines = [DataSharingDelegateInline]

	def save_formset(self, request, form, formset, change):
		instances = formset.save(commit=False)
		for obj in instances:
			if isinstance(obj, DataSharingDelegate) and not obj.created_by_id:
				obj.created_by = request.user
			obj.save()
		for obj in formset.deleted_objects:
			obj.delete()
		formset.save_m2m()


@admin.register(DataSharingDelegate)
class DataSharingDelegateAdmin(admin.ModelAdmin):
	"""PI-designated deputies who may share the PI's private data (#447)."""
	list_display = ('pi', 'delegate', 'group', 'created_by', 'created_date')
	list_filter = ('group',)
	search_fields = ('pi__name', 'delegate__username')
	raw_id_fields = ('pi', 'delegate')
	readonly_fields = ('created_by', 'created_date')

	def save_model(self, request, obj, form, change):
		if not obj.created_by_id:
			obj.created_by = request.user
		super().save_model(request, obj, form, change)

admin.site.register(Profile)
admin.site.register(UserTelescopeToFollow)
#admin.site.register(UserQuery)
admin.site.register(Host)
admin.site.register(Transient)
#admin.site.register(SimpleTransientSpecRequest)


class TransientFollowupRequestInline(admin.TabularInline):
	model = TransientFollowupRequest
	extra = 0
	raw_id_fields = ('requestor',)


@admin.register(TransientFollowup)
class TransientFollowupAdmin(admin.ModelAdmin):
	inlines = [TransientFollowupRequestInline]


admin.site.register(TransientFollowupRequest)
admin.site.register(HostFollowup)
admin.site.register(TransientObservationTask)
admin.site.register(HostObservationTask)


@admin.register(TransientSpectrum)
class TransientSpectrumAdmin(admin.ModelAdmin):
	"""``pis`` (#446): the spectrum's principal investigator(s)."""
	raw_id_fields = ('transient', 'followup')
	filter_horizontal = ('groups', 'pis', 'data_quality')
	list_display = ('__str__', 'instrument', 'obs_group')
	list_select_related = ('transient', 'instrument', 'obs_group')


admin.site.register(HostSpectrum)


@admin.register(TransientPhotometry)
class TransientPhotometryAdmin(admin.ModelAdmin):
	"""``pis`` (#446): the photometry set's principal investigator(s)."""
	raw_id_fields = ('transient', 'host', 'followup')
	filter_horizontal = ('groups', 'pis')
	list_display = ('__str__', 'instrument', 'obs_group')
	list_select_related = ('transient', 'instrument', 'obs_group')


admin.site.register(HostPhotometry)
admin.site.register(InformationSource)
admin.site.register(TransientWebResource)
admin.site.register(HostWebResource)
admin.site.register(AlternateTransientNames)
admin.site.register(TransientSpecData)
admin.site.register(HostSpecData)
admin.site.register(TransientPhotData)
admin.site.register(HostPhotData)
admin.site.register(TransientImage)
admin.site.register(TransientDiffImage)
admin.site.register(ClassicalNightType)
admin.site.register(WebAppColor)
admin.site.register(Unit)
admin.site.register(DataQuality)
admin.site.register(HostImage)
admin.site.register(HostSED)
admin.site.register(Log)
admin.site.register(TransientTag)
admin.site.register(GWCandidate)
admin.site.register(GWCandidateImage)
admin.site.register(SurveyField)
admin.site.register(SurveyFieldMSB)
admin.site.register(SurveyObservation)
admin.site.register(CanvasFOV)


# --- Encrypted credentials (#264): the secret is written through a form field
# and never rendered back; list and change pages show only the payload's keys.
class EncryptedCredentialForm(forms.ModelForm):
	secret_json = forms.CharField(
		label="New secret (JSON object)", required=False,
		widget=forms.Textarea(attrs={"rows": 4, "cols": 80, "autocomplete": "off"}),
		help_text="Paste a JSON object such as {\"username\": \"...\", \"password\": \"...\"}. "
				  "Leave blank to keep the stored secret. It is encrypted on save and never shown again.",
	)

	class Meta:
		model = EncryptedCredential
		fields = ("name", "service", "kind", "description", "owner_group", "owner_user", "is_active")

	def clean_secret_json(self):
		raw = (self.cleaned_data.get("secret_json") or "").strip()
		if not raw:
			return None
		import json
		try:
			payload = json.loads(raw)
		except ValueError:
			raise forms.ValidationError("Not valid JSON.")
		if not isinstance(payload, dict):
			raise forms.ValidationError("The secret must be a JSON object.")
		return payload

	def save(self, commit=True):
		obj = super().save(commit=False)
		payload = self.cleaned_data.get("secret_json")
		if payload is not None:
			obj.set_secret(payload)
		if commit:
			obj.save()
			self.save_m2m()
		return obj


@admin.register(EncryptedCredential)
class EncryptedCredentialAdmin(admin.ModelAdmin):
	form = EncryptedCredentialForm
	list_display = ("name", "service", "kind", "owner_group", "owner_user", "is_active",
					"masked_display", "key_fingerprint", "last_used_at", "modified_date")
	list_filter = ("kind", "service", "is_active")
	search_fields = ("name", "service", "description")
	readonly_fields = ("masked_display", "key_fingerprint", "last_used_at",
					   "created_by", "created_date", "modified_by", "modified_date")
	fieldsets = (
		(None, {"fields": ("name", "service", "kind", "description", "owner_group", "owner_user", "is_active")}),
		("Secret", {"fields": ("masked_display", "secret_json", "key_fingerprint", "last_used_at")}),
		("Audit", {"fields": ("created_by", "created_date", "modified_by", "modified_date")}),
	)

	def save_model(self, request, obj, form, change):
		if not change or not obj.created_by_id:
			obj.created_by = request.user
		obj.modified_by = request.user
		super().save_model(request, obj, form, change)


class ExternalServiceForm(forms.ModelForm):
	# The TEXT-backed JSON column needs a JSON form field, or the admin would
	# store the textarea contents as a JSON string instead of an object.
	default_params = forms.JSONField(required=False, initial=dict,
									 widget=forms.Textarea(attrs={"rows": 4, "cols": 80}))

	class Meta:
		model = ExternalService
		exclude = ("created_by", "modified_by")

	def clean_default_params(self):
		value = self.cleaned_data.get("default_params")
		if value in (None, ""):
			return {}
		if not isinstance(value, dict):
			raise forms.ValidationError("Default params must be a JSON object.")
		return value


@admin.register(ExternalService)
class ExternalServiceAdmin(admin.ModelAdmin):
	form = ExternalServiceForm
	list_display = ("name", "slug", "kind", "enabled", "base_url", "credential", "max_runs_per_user_per_day")
	list_filter = ("kind", "enabled")
	search_fields = ("name", "slug", "description")
	prepopulated_fields = {"slug": ("name",)}
	filter_horizontal = ("groups",)
	readonly_fields = ("created_by", "created_date", "modified_by", "modified_date")
	exclude = ()

	def save_model(self, request, obj, form, change):
		if not change or not obj.created_by_id:
			obj.created_by = request.user
		obj.modified_by = request.user
		super().save_model(request, obj, form, change)


@admin.register(ExternalServiceRun)
class ExternalServiceRunAdmin(admin.ModelAdmin):
	list_display = ("uuid", "service", "status", "transient", "target_ref", "created_by", "created_date", "finished_at")
	list_filter = ("status", "service")
	search_fields = ("uuid", "target_ref", "external_id", "transient__name")
	readonly_fields = ("uuid", "callback_token_hash", "created_by", "created_date", "modified_by", "modified_date")
	raw_id_fields = ("transient",)
	exclude = ("target_content_type", "target_object_id")

	def save_model(self, request, obj, form, change):
		if not change or not obj.created_by_id:
			obj.created_by = request.user
		obj.modified_by = request.user
		super().save_model(request, obj, form, change)
# --- Analysis services (#312, #313) -------------------------------------------
from YSE_App.models.analysis_models import AnalysisResultFile, AnalysisService  # noqa: E402


class AnalysisServiceForm(forms.ModelForm):
	input_spec = forms.JSONField(required=False, initial=list, widget=forms.Textarea(attrs={"rows": 2, "cols": 80}))
	output_spec = forms.JSONField(required=False, initial=list, widget=forms.Textarea(attrs={"rows": 2, "cols": 80}))
	param_schema = forms.JSONField(required=False, initial=dict, widget=forms.Textarea(attrs={"rows": 8, "cols": 80}))
	summary_keys = forms.JSONField(required=False, initial=list, widget=forms.Textarea(attrs={"rows": 2, "cols": 80}))

	class Meta:
		model = AnalysisService
		exclude = ("created_by", "modified_by")

	def clean(self):
		cleaned = super().clean()
		for key, default in (("input_spec", []), ("output_spec", []), ("param_schema", {}), ("summary_keys", [])):
			if cleaned.get(key) in (None, ""):
				cleaned[key] = default
		if cleaned.get("runner_kind") == AnalysisService.RUNNER_INPROCESS and not cleaned.get("runner_path"):
			raise forms.ValidationError("An in-process service needs a runner_path.")
		return cleaned


@admin.register(AnalysisService)
class AnalysisServiceAdmin(admin.ModelAdmin):
	form = AnalysisServiceForm
	list_display = ("service", "runner_kind", "runner_path", "timeout_seconds", "display_order")
	list_filter = ("runner_kind",)
	search_fields = ("service__name", "service__slug", "runner_path")
	readonly_fields = ("created_by", "created_date", "modified_by", "modified_date")

	def save_model(self, request, obj, form, change):
		if not change or not obj.created_by_id:
			obj.created_by = request.user
		obj.modified_by = request.user
		super().save_model(request, obj, form, change)


@admin.register(AnalysisResultFile)
class AnalysisResultFileAdmin(admin.ModelAdmin):
	list_display = ("name", "kind", "run", "content_type", "size", "created_date")
	list_filter = ("kind",)
	search_fields = ("name", "run__uuid")
	readonly_fields = ("size", "created_by", "created_date", "modified_by", "modified_date")
	raw_id_fields = ("run",)


# --- Transient annotations (#317) ----------------------------------------------
from YSE_App.models.annotation_models import TransientAnnotation, TransientAnnotationValue  # noqa: E402


class TransientAnnotationForm(forms.ModelForm):
	data = forms.JSONField(required=False, initial=dict, widget=forms.Textarea(attrs={"rows": 8, "cols": 80}))

	class Meta:
		model = TransientAnnotation
		exclude = ("created_by", "modified_by")

	def clean_data(self):
		value = self.cleaned_data.get("data")
		if value in (None, ""):
			return {}
		if not isinstance(value, dict):
			raise forms.ValidationError("Annotation data must be a JSON object.")
		return value


class TransientAnnotationValueInline(admin.TabularInline):
	model = TransientAnnotationValue
	extra = 0
	can_delete = False
	fields = ("key", "value_text", "value_num")
	readonly_fields = fields

	def has_add_permission(self, request, obj=None):
		return False


@admin.register(TransientAnnotation)
class TransientAnnotationAdmin(admin.ModelAdmin):
	form = TransientAnnotationForm
	list_display = ("transient", "origin", "verdict", "service", "modified_by", "modified_date")
	list_filter = ("origin", "service")
	search_fields = ("transient__name", "origin")
	raw_id_fields = ("transient", "run")
	filter_horizontal = ("groups",)
	readonly_fields = ("created_by", "created_date", "modified_by", "modified_date")
	inlines = (TransientAnnotationValueInline,)

	def save_model(self, request, obj, form, change):
		if not change or not obj.created_by_id:
			obj.created_by = request.user
		obj.modified_by = request.user
		super().save_model(request, obj, form, change)
		obj.sync_values()


# --- Background job queue (#263) and notifications (#266) -------------------
from django.utils import timezone as _tz  # noqa: E402

from YSE_App.models.job_models import Job  # noqa: E402
from YSE_App.models.notification_models import Notification, NotificationPreference  # noqa: E402


@admin.register(Job)
class JobAdmin(admin.ModelAdmin):
	list_display = ("id", "kind", "status", "attempts", "max_attempts", "run_after", "locked_by",
					"finished_at", "short_error", "created_by")
	list_filter = ("status", "kind")
	search_fields = ("kind", "error", "locked_by", "created_by__username")
	readonly_fields = ("attempts", "locked_at", "locked_by", "started_at", "finished_at", "result",
					   "created_at", "updated_at")
	date_hierarchy = "created_at"
	actions = ("retry_jobs", "cancel_jobs")
	list_select_related = ("created_by",)

	def short_error(self, obj):
		if not obj.error:
			return ""
		return obj.error.strip().splitlines()[-1][:80]
	short_error.short_description = "error"

	def retry_jobs(self, request, queryset):
		n = 0
		for job in queryset.filter(status__in=(Job.FAILED, Job.CANCELLED)):
			job.requeue()
			n += 1
		self.message_user(request, "%d job(s) queued again." % n)
	retry_jobs.short_description = "Retry selected failed/cancelled jobs"

	def cancel_jobs(self, request, queryset):
		n = queryset.filter(status=Job.QUEUED).update(status=Job.CANCELLED, finished_at=_tz.now(),
													  updated_at=_tz.now())
		self.message_user(request, "%d queued job(s) cancelled." % n)
	cancel_jobs.short_description = "Cancel selected queued jobs"


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
	list_display = ("id", "recipient", "kind", "subject", "created", "read_at", "delivery_summary")
	list_filter = ("kind",)
	search_fields = ("recipient__username", "subject", "text")
	readonly_fields = ("created", "delivered")
	raw_id_fields = ("recipient", "transient")
	list_select_related = ("recipient",)

	def delivery_summary(self, obj):
		delivered = obj.delivered or {}
		parts = []
		for channel, state in sorted(delivered.items()):
			state = state or {}
			parts.append("%s: %s" % (channel, "sent" if state.get("sent_at") else (state.get("error") or "pending")[:40]))
		return "; ".join(parts)
	delivery_summary.short_description = "delivery"


@admin.register(NotificationPreference)
class NotificationPreferenceAdmin(admin.ModelAdmin):
	list_display = ("user", "in_app", "email", "has_slack_webhook", "slack_user_id", "updated_at")
	list_filter = ("in_app", "email")
	search_fields = ("user__username", "slack_user_id")
	raw_id_fields = ("user",)

	def has_slack_webhook(self, obj):
		return bool(obj.slack_webhook_url)
	has_slack_webhook.boolean = True
	has_slack_webhook.short_description = "slack"


# --- Broker filters and candidates (#276): criteria / query JSON edited as text.
class BrokerFilterVersionInline(admin.TabularInline):
	model = BrokerFilterVersion
	extra = 0
	can_delete = False
	readonly_fields = ("version", "query", "criteria", "topics", "saved_by", "saved_at", "note")
	ordering = ("-version",)

	def has_add_permission(self, request, obj=None):
		return False


@admin.register(BrokerFilter)
class BrokerFilterAdmin(admin.ModelAdmin):
	list_display = ("name", "broker", "group", "enabled", "auto_save", "notify_group", "version", "save_status",
					"last_run_at", "last_run_summary")
	list_filter = ("broker", "enabled", "auto_save", "notify_group", "group")
	search_fields = ("name", "description")
	readonly_fields = ("version", "last_run_at", "last_run_summary", "created_at", "updated_at")
	filter_horizontal = ("default_tags",)
	inlines = (BrokerFilterVersionInline,)
	fieldsets = (
		(None, {"fields": ("name", "broker", "group", "description", "enabled")}),
		("Rule", {"fields": ("query", "criteria", "topics", "max_alerts", "version"),
				  "description": "query: broker-side keys (see the provider's query_keys); criteria: "
								 "YSE_App.brokers.filters.CRITERIA keys, all must pass; topics: stream topics this "
								 "filter applies to (blank = all). Editing query / criteria records a new version."}),
		("Saving", {"fields": ("auto_save", "save_status", "save_obs_group", "import_photometry", "default_tags")}),
		("Notifications", {"fields": ("notify_group",)}),
		("Bookkeeping", {"fields": ("created_by", "last_run_at", "last_run_summary", "created_at", "updated_at")}),
	)

	def save_model(self, request, obj, form, change):
		if not change and not obj.created_by_id:
			obj.created_by = request.user
		super().save_model(request, obj, form, change)
		obj.record_version(request.user, "edited in admin" if change else "created")


@admin.register(BrokerConnection)
class BrokerConnectionAdmin(admin.ModelAdmin):
	list_display = ("slug", "name", "broker", "kind", "enabled", "bootstrap_servers", "group_id", "message_format",
					"last_message_at", "short_error")
	list_filter = ("broker", "kind", "enabled")
	search_fields = ("name", "slug", "bootstrap_servers", "description")
	raw_id_fields = ("credential",)
	prepopulated_fields = {"slug": ("name",)}
	readonly_fields = ("last_message_at", "last_error", "created_at", "updated_at")
	fieldsets = (
		(None, {"fields": ("name", "slug", "broker", "kind", "enabled", "description")}),
		("Stream", {"fields": ("bootstrap_servers", "topics", "group_id", "message_format", "credential", "config"),
					"description": "Off by default. Start a worker with manage.py broker_ingest --connection <slug> "
								   "(docs/broker-streams.md). Credential: username/password (Kafka SASL) or "
								   "api_key/api_secret (ANTARES)."}),
		("Bookkeeping", {"fields": ("created_by", "last_message_at", "last_error", "created_at", "updated_at")}),
	)

	def short_error(self, obj):
		return (obj.last_error or "")[:80]
	short_error.short_description = "last error"

	def save_model(self, request, obj, form, change):
		if not change and not obj.created_by_id:
			obj.created_by = request.user
		super().save_model(request, obj, form, change)


@admin.register(IngestHeartbeat)
class IngestHeartbeatAdmin(admin.ModelAdmin):
	list_display = ("connection", "topic", "worker", "status", "stale", "last_seen", "lag", "messages", "candidates",
					"saved", "errors", "hostname", "pid", "short_error")
	list_filter = ("status", "connection")
	readonly_fields = [f.name for f in IngestHeartbeat._meta.fields]
	ordering = ("connection", "topic", "worker")

	def stale(self, obj):
		from YSE_App.brokers.streams import stale_minutes

		return obj.is_stale(stale_minutes())
	stale.boolean = True

	def short_error(self, obj):
		return (obj.last_error or "")[:80]
	short_error.short_description = "last error"

	def has_add_permission(self, request):
		return False


@admin.register(Candidate)
class CandidateAdmin(admin.ModelAdmin):
	list_display = ("alert_id", "broker", "status", "last_mag", "last_band", "rb", "classification", "transient", "last_seen")
	list_filter = ("broker", "status")
	search_fields = ("alert_id", "classification", "transient__name")
	raw_id_fields = ("transient", "status_changed_by")
	readonly_fields = ("first_seen", "last_seen", "n_alerts", "topic", "created_at", "updated_at")
	filter_horizontal = ("filters", "rejected_by_groups")


# --- Allocations and facility requests (#303, #298) -----------------------------
from YSE_App.models.allocation_models import Allocation, FacilityRequest  # noqa: E402


@admin.register(Allocation)
class AllocationAdmin(admin.ModelAdmin):
	list_display = ("name", "telescope", "instrument", "principal_investigator", "facility", "credential",
	                "hours_used", "hours_allocated", "start_date", "end_date", "is_active")
	list_filter = ("facility", "is_active", "telescope")
	search_fields = ("name", "proposal_id", "telescope__name", "notes")
	filter_horizontal = ("groups",)
	raw_id_fields = ("credential",)
	readonly_fields = ("service", "created_by", "created_date", "modified_by", "modified_date")

	def save_model(self, request, obj, form, change):
		if not change or not obj.created_by_id:
			obj.created_by = request.user
		obj.modified_by = request.user
		super().save_model(request, obj, form, change)
		from YSE_App.services.allocations import ensure_service

		ensure_service(obj, request.user)


@admin.register(FacilityRequest)
class FacilityRequestAdmin(admin.ModelAdmin):
	list_display = ("id", "transient", "allocation", "kind", "state", "external_id", "submitted_by", "submitted_at",
	                "attempts", "hours_charged", "charged_at", "n_results")
	list_filter = ("state", "kind", "allocation__facility", "allocation")
	search_fields = ("transient__name", "external_id", "allocation__name")
	raw_id_fields = ("transient", "followup", "run")
	readonly_fields = ("charged_at", "attempts", "results_ingested_at", "n_results", "created_by", "created_date",
	                   "modified_by", "modified_date")

	def save_model(self, request, obj, form, change):
		if not change or not obj.created_by_id:
			obj.created_by = request.user
		obj.modified_by = request.user
		super().save_model(request, obj, form, change)


# --- Favorite transients (#323) ---------------------------------------------
from YSE_App.models.favorite_models import UserFavoriteTransient  # noqa: E402


@admin.register(UserFavoriteTransient)
class UserFavoriteTransientAdmin(admin.ModelAdmin):
	list_display = ("user", "transient", "created")
	search_fields = ("user__username", "transient__name")
	raw_id_fields = ("user", "transient")
	list_select_related = ("user", "transient")


# --- Transient interests (#288) and data access requests (#291) --------------
@admin.register(TransientInterest)
class TransientInterestAdmin(admin.ModelAdmin):
	list_display = ("transient", "user", "title", "group", "role", "status", "created_date", "modified_date")
	list_filter = ("status", "role", "group")
	search_fields = ("title", "description", "doi", "transient__name", "user__username")
	raw_id_fields = ("transient", "user")
# --- Sharing services (#324, #325) ----------------------------------------------
from YSE_App.models.sharing_models import AutoPublisher, SharingService, SharingSubmission  # noqa: E402


class SharingServiceAdminForm(forms.ModelForm):
	class Meta:
		model = SharingService
		fields = "__all__"

	def clean_config(self):
		value = self.cleaned_data.get("config")
		if value in (None, ""):
			return {}
		if not isinstance(value, dict):
			raise forms.ValidationError("config must be a JSON object")
		return value


@admin.register(SharingService)
class SharingServiceAdmin(admin.ModelAdmin):
	form = SharingServiceAdminForm
	list_display = ("name", "slug", "kind", "tns_group_id", "credential", "testing", "enabled")
	list_filter = ("kind", "testing", "enabled")
	search_fields = ("name", "slug", "tns_group_name", "description")
	filter_horizontal = ("allowed_instruments", "allowed_obs_groups", "groups")
	raw_id_fields = ("credential",)
	prepopulated_fields = {"slug": ("name",)}
	readonly_fields = ("created_by", "created_date", "modified_by", "modified_date")

	def save_model(self, request, obj, form, change):
		if not change or not obj.created_by_id:
			obj.created_by = request.user
		obj.modified_by = request.user
		super().save_model(request, obj, form, change)


@admin.register(DataAccessRequest)
class DataAccessRequestAdmin(admin.ModelAdmin):
	list_display = ("id", "transient", "requester", "dataset_kind", "dataset_id", "owner_group", "target_group",
	                "status", "decided_by", "decided_as", "decided_at", "created_date")
	list_filter = ("status", "decided_as", "dataset_kind", "owner_group")
	search_fields = ("transient__name", "requester__username", "message", "note")
	raw_id_fields = ("transient", "requester", "decided_by")
	readonly_fields = ("granted_dataset_ids", "decided_as", "created_by", "created_date", "modified_by", "modified_date")
@admin.register(SharingSubmission)
class SharingSubmissionAdmin(admin.ModelAdmin):
	list_display = ("id", "service", "transient", "kind", "status", "tns_name", "external_id", "attempts",
					"created_by", "created_date", "finished_at")
	list_filter = ("status", "kind", "service")
	search_fields = ("transient__name", "tns_name", "external_id", "error")
	raw_id_fields = ("transient", "job", "auto_publisher")
	readonly_fields = ("payload", "response", "error", "attempts", "external_id", "tns_name", "submitted_at",
					   "finished_at", "created_by", "created_date", "modified_by", "modified_date")
	date_hierarchy = "created_date"
	actions = ("retry_submissions",)
	list_select_related = ("service", "transient", "created_by")

	def retry_submissions(self, request, queryset):
		from YSE_App.sharing.tns import retry_submission

		n = 0
		for submission in queryset:
			if submission.can_retry:
				retry_submission(submission, request.user)
				n += 1
		self.message_user(request, "%d submission(s) queued again." % n)
	retry_submissions.short_description = "Retry failed / rejected submissions"


@admin.register(AutoPublisher)
class AutoPublisherAdmin(admin.ModelAdmin):
	list_display = ("name", "service", "group", "kind", "tns_enabled", "hermes_enabled", "enabled", "last_run_at")
	list_filter = ("service", "kind", "enabled", "tns_enabled", "hermes_enabled")
	search_fields = ("name", "group__name")
	readonly_fields = ("last_run_at", "created_by", "created_date", "modified_by", "modified_date")

	def save_model(self, request, obj, form, change):
		if not change or not obj.created_by_id:
			obj.created_by = request.user
		obj.modified_by = request.user
		super().save_model(request, obj, form, change)
		from YSE_App.sharing.autopublish import reset_cache

		reset_cache()


# --- Feed sources (#280): Hermes / SCiMMA, Einstein Probe, JPL Scout ------------
from YSE_App.models.feed_models import FeedSource  # noqa: E402


class FeedSourceForm(forms.ModelForm):
	config = forms.JSONField(required=False, initial=dict, widget=forms.Textarea(attrs={"rows": 6, "cols": 80}),
							 help_text=FeedSource._meta.get_field("config").help_text)

	class Meta:
		model = FeedSource
		exclude = ("created_by",)

	def clean_config(self):
		value = self.cleaned_data.get("config")
		if value in (None, ""):
			return {}
		if not isinstance(value, dict):
			raise forms.ValidationError("config must be a JSON object.")
		return value


@admin.register(FeedSource)
class FeedSourceAdmin(admin.ModelAdmin):
	form = FeedSourceForm
	list_display = ("name", "kind", "topic", "enabled", "credential", "last_polled", "last_summary")
	list_filter = ("kind", "enabled")
	search_fields = ("name", "slug", "topic", "description")
	prepopulated_fields = {"slug": ("name",)}
	raw_id_fields = ("credential",)
	readonly_fields = ("last_polled", "last_summary", "last_error", "created_by", "created_at", "updated_at")
	fieldsets = (
		(None, {"fields": ("name", "slug", "kind", "topic", "description", "enabled")}),
		("Access", {"fields": ("credential", "config"),
					"description": "Hermes: the credential holds hermes_token (REST) and optionally hop_username / "
								   "hop_password (Kafka). Einstein Probe: client_id / client_secret for GCN Kafka, or "
								   "config.url for a notice mirror. Scout: no credential. See docs/feeds-*.md."}),
		("Bookkeeping", {"fields": ("last_polled", "last_summary", "last_error", "created_by", "created_at", "updated_at")}),
	)

	def save_model(self, request, obj, form, change):
		if not change and not obj.created_by_id:
			obj.created_by = request.user
		super().save_model(request, obj, form, change)


# --- AI summaries (#295, #297) --------------------------------------------------
from YSE_App.models.summary_models import (  # noqa: E402
	TransientSummaryEmbedding, TransientSummaryHistory, TransientSummaryPreference,
)


@admin.register(TransientSummaryHistory)
class TransientSummaryHistoryAdmin(admin.ModelAdmin):
	list_display = ("transient", "source", "provider", "model_name", "prompt_version", "is_current", "created_by", "created_date")
	list_filter = ("source", "provider", "is_current")
	search_fields = ("transient__name", "text", "model_name")
	raw_id_fields = ("transient", "run")
	readonly_fields = ("created_by", "created_date", "modified_by", "modified_date", "inputs_hash")

	def save_model(self, request, obj, form, change):
		if not change or not obj.created_by_id:
			obj.created_by = request.user
		obj.modified_by = request.user
		super().save_model(request, obj, form, change)


@admin.register(TransientSummaryEmbedding)
class TransientSummaryEmbeddingAdmin(admin.ModelAdmin):
	list_display = ("transient", "model_name", "dim", "modified_date")
	list_filter = ("model_name",)
	search_fields = ("transient__name",)
	raw_id_fields = ("transient",)
	readonly_fields = ("vector", "dim", "model_name", "text_hash", "created_date", "modified_date")


@admin.register(TransientSummaryPreference)
class TransientSummaryPreferenceAdmin(admin.ModelAdmin):
	list_display = ("user", "ai_enabled", "modified_date")
	list_filter = ("ai_enabled",)
	search_fields = ("user__username",)
	raw_id_fields = ("user",)


from django.urls import reverse as _reverse  # noqa: E402
from django.utils.html import format_html as _format_html  # noqa: E402

from YSE_App.models.feedback_models import Feedback  # noqa: E402


@admin.register(Feedback)
class FeedbackAdmin(admin.ModelAdmin):
	"""Feedback button rows (#443). Posting to GitHub is only on the staff review page."""
	list_display = ("id", "created", "kind", "title", "status", "page_url", "review_link")
	list_filter = ("status", "kind")
	search_fields = ("title", "text", "page_url")
	readonly_fields = ("user", "kind", "title", "text", "page_url", "created", "github_issue_url",
					   "reviewed_by", "reviewed_at", "review_link")
	fields = ("status", "review_link", "user", "created", "kind", "title", "text", "page_url",
			  "github_issue_url", "reviewed_by", "reviewed_at")

	def has_add_permission(self, request):
		return False

	def review_link(self, obj):
		if not obj or not obj.pk:
			return ""
		return _format_html('<a href="{}">Review / create GitHub issue</a>', _reverse("feedback_review_detail", args=[obj.pk]))

	review_link.short_description = "review"


# Two-factor sign-in (#512): superuser action on Users to reset a user's devices.
from django.conf import settings as _settings  # noqa: E402

if getattr(_settings, 'STAFF_MFA_REQUIRED', False):
    from YSE_App.staff_mfa.admin import install_user_admin_action  # noqa: E402

    install_user_admin_action()


# ---------------------------------------------------------------------------
# Researcher profile, mentorship, metrics and the user agreement (#627-#630).
# Encrypted values are write-only: the admin shows only "(encrypted, set on <date>)".
from YSE_App.models.researcher_models import (  # noqa: E402
	AffiliationHistory, AgreementAcceptance, EncryptedProfileValue, Mentorship, ProfileFieldHistory,
	ResearcherMetrics, ResearcherProfile, UserAgreement,
)
from YSE_App.services import researcher_profile as _rp  # noqa: E402

# Staff see a warning on the admin index while no research public key is installed.
admin.site.index_template = 'YSE_App/admin_index.html'


class _ReadOnlyAdmin(admin.ModelAdmin):
	def has_add_permission(self, request):
		return False

	def has_change_permission(self, request, obj=None):
		return False


@admin.register(ResearcherProfile)
class ResearcherProfileAdmin(admin.ModelAdmin):
	list_display = ("user", "career_level", "institution", "profile_completed_at", "profile_confirmed_at")
	list_filter = ("career_level",)
	search_fields = ("user__username", "full_name", "institution")
	raw_id_fields = ("user",)
	filter_horizontal = ("observing_experience",)
	readonly_fields = ("encrypted_fields", "profile_completed_at", "created_at", "updated_at")

	def encrypted_fields(self, obj):
		if not obj or not obj.pk:
			return "-"
		status = _rp.encrypted_status(obj.user)
		return "; ".join("%s: %s" % (name, row.display()) for name, row in sorted(status.items())) or "(none stored)"
	encrypted_fields.short_description = "encrypted fields"

	def save_model(self, request, obj, form, change):
		before = {name: "" for name in _rp.PLAIN_FIELDS}
		if change and obj.pk:
			old = ResearcherProfile.objects.filter(pk=obj.pk).first()
			if old is not None:
				before = _rp.snapshot(old)
		obj._history_before = before
		super().save_model(request, obj, form, change)

	def save_related(self, request, form, formsets, change):
		super().save_related(request, form, formsets, change)
		obj = form.instance
		_rp.record_plain_changes(obj.user, getattr(obj, "_history_before", {}), _rp.snapshot(obj),
		                         changed_by=request.user)


@admin.register(EncryptedProfileValue)
class EncryptedProfileValueAdmin(_ReadOnlyAdmin):
	list_display = ("user", "field_name", "display", "key_fingerprint_short", "set_at")
	list_filter = ("field_name",)
	search_fields = ("user__username",)
	fields = ("user", "field_name", "display", "key_fingerprint_short", "set_at")
	readonly_fields = fields

	def display(self, obj):
		return obj.display()
	display.short_description = "value"

	def key_fingerprint_short(self, obj):
		return (obj.key_fingerprint or "")[:16]
	key_fingerprint_short.short_description = "key fingerprint"


@admin.register(ProfileFieldHistory)
class ProfileFieldHistoryAdmin(_ReadOnlyAdmin):
	list_display = ("user", "field_name", "shown_value", "cleared", "changed_at", "changed_by")
	list_filter = ("field_name", "cleared")
	search_fields = ("user__username",)
	fields = ("user", "field_name", "shown_value", "cleared", "changed_at", "changed_by")
	readonly_fields = fields

	def shown_value(self, obj):
		if obj.encrypted_value_id:
			return obj.encrypted_value.display()
		if obj.field_name in ("country", "phd_year", "gender"):
			return "(encrypted value deleted)" if obj.cleared else "(encrypted)"
		return obj.value_plain
	shown_value.short_description = "value"


@admin.register(Mentorship)
class MentorshipAdmin(admin.ModelAdmin):
	list_display = ("id", "kind", "mentor_label", "mentee_label", "start_year", "end_year")
	list_filter = ("kind",)
	search_fields = ("mentor__username", "mentee__username", "mentor_name", "mentee_name")
	raw_id_fields = ("mentor", "mentee", "created_by")


@admin.register(AffiliationHistory)
class AffiliationHistoryAdmin(admin.ModelAdmin):
	list_display = ("user", "institution", "department", "position", "start_year", "end_year")
	search_fields = ("user__username", "institution")
	raw_id_fields = ("user",)


@admin.register(ResearcherMetrics)
class ResearcherMetricsAdmin(_ReadOnlyAdmin):
	list_display = ("user", "source", "fetched_at", "h_index", "citation_count", "refereed_count", "first_author_count")
	list_filter = ("source",)
	search_fields = ("user__username",)


@admin.register(UserAgreement)
class UserAgreementAdmin(admin.ModelAdmin):
	list_display = ("version", "title", "effective_date", "is_current", "acceptance_count")
	list_filter = ("is_current",)
	fieldsets = (
		(None, {
			"fields": ("version", "title", "effective_date", "is_current", "body"),
			"description": "Saving a version as current makes every other version not current and asks every "
			               "user to accept it on their next page view. In the body, {acknowledgements_url} "
			               "becomes the link to this site's Acknowledgements page.",
		}),
	)

	def acceptance_count(self, obj):
		return obj.acceptances.count()
	acceptance_count.short_description = "acceptances"


@admin.register(AgreementAcceptance)
class AgreementAcceptanceAdmin(_ReadOnlyAdmin):
	list_display = ("user", "agreement", "accepted_at")
	list_filter = ("agreement",)
	search_fields = ("user__username",)


# ---------------------------------------------------------------------------
# Research teams (#445, #440). Staff assign the first owners here, as data: the
# migrations create no owner rows. Owners then manage their team on /teams/<id>/.
from django.contrib.auth.admin import GroupAdmin as _DjangoGroupAdmin  # noqa: E402
from django.contrib.auth.models import Group as _AuthGroup  # noqa: E402

from YSE_App.models.group_models import GroupInvitation, GroupInvitationUse, GroupProfile  # noqa: E402
from YSE_App.services import groups as _group_svc  # noqa: E402


class GroupProfileAdminForm(forms.ModelForm):
	class Meta:
		model = GroupProfile
		fields = ("description", "is_active", "is_system", "owners")

	def clean(self):
		cleaned = super().clean()
		group = getattr(self.instance, "group", None) if self.instance and self.instance.group_id else None
		group = cleaned.get("group") or group
		system = bool(cleaned.get("is_system")) or (group is not None and group.name in _group_svc.system_group_names())
		if system and cleaned.get("owners"):
			raise forms.ValidationError("System groups cannot have owners (they are not research teams).")
		return cleaned


class GroupProfileInline(admin.StackedInline):
	model = GroupProfile
	form = GroupProfileAdminForm
	can_delete = False
	extra = 0
	max_num = 1
	filter_horizontal = ("owners",)
	fields = ("description", "is_active", "is_system", "owners")


admin.site.unregister(_AuthGroup)


@admin.register(_AuthGroup)
class YSEGroupAdmin(_DjangoGroupAdmin):
	inlines = (GroupProfileInline,)
	list_display = ("name", "team_active", "team_system", "owner_names")

	def get_queryset(self, request):
		return super().get_queryset(request).select_related("group_profile").prefetch_related("group_profile__owners")

	def get_object(self, request, object_id, from_field=None):
		obj = super().get_object(request, object_id, from_field)
		if obj is not None and getattr(obj, "group_profile", None) is None:
			_group_svc.profile_for(obj)  # groups created by older code: show the profile inline
			obj = super().get_object(request, object_id, from_field)
		return obj

	def _profile(self, obj):
		return getattr(obj, "group_profile", None)

	def team_active(self, obj):
		profile = self._profile(obj)
		return True if profile is None else profile.is_active
	team_active.boolean = True
	team_active.short_description = "active"

	def team_system(self, obj):
		profile = self._profile(obj)
		return bool(profile and profile.is_system) or obj.name in _group_svc.system_group_names()
	team_system.boolean = True
	team_system.short_description = "system"

	def owner_names(self, obj):
		profile = self._profile(obj)
		return ", ".join(u.get_username() for u in profile.owners.all()) if profile else ""
	owner_names.short_description = "owners"


@admin.register(GroupProfile)
class GroupProfileAdmin(admin.ModelAdmin):
	form = GroupProfileAdminForm
	list_display = ("group", "is_active", "is_system", "owner_count", "updated_at")
	list_filter = ("is_active", "is_system")
	search_fields = ("group__name",)
	filter_horizontal = ("owners",)
	readonly_fields = ("group", "created_at", "updated_at")
	fields = ("group", "description", "is_active", "is_system", "owners", "created_at", "updated_at")

	def has_add_permission(self, request):
		return False  # one per group, created automatically; edit it from the group

	def owner_count(self, obj):
		return obj.owners.count()
	owner_count.short_description = "owners"


class GroupInvitationUseInline(admin.TabularInline):
	model = GroupInvitationUse
	extra = 0
	can_delete = False
	readonly_fields = ("used_by", "used_at")

	def has_add_permission(self, request, obj=None):
		return False


@admin.register(GroupInvitation)
class GroupInvitationAdmin(admin.ModelAdmin):
	"""Invitations are created on the team pages; here staff can only review and revoke them."""
	list_display = ("id", "group", "created_by", "created_at", "expires_at", "max_uses", "status")
	list_filter = ("group",)
	readonly_fields = ("group", "created_by", "created_at", "expires_at", "max_uses", "revoked_by", "status")
	fields = ("group", "created_by", "created_at", "expires_at", "max_uses", "revoked_at", "revoked_by", "status")
	inlines = (GroupInvitationUseInline,)

	def has_add_permission(self, request):
		return False


from YSE_App.models.group_models import GroupMembershipRequest  # noqa: E402


@admin.register(GroupMembershipRequest)
class GroupMembershipRequestAdmin(admin.ModelAdmin):
	"""Requests are made on /teams/ and decided by the team's owners there (#448); staff review them here."""
	list_display = ("id", "group", "user", "status", "created_at", "decided_by", "decided_at")
	list_filter = ("status", "group")
	readonly_fields = ("user", "group", "message", "status", "created_at", "decided_by", "decided_at")
	fields = readonly_fields

	def has_add_permission(self, request):
		return False

	def has_change_permission(self, request, obj=None):
		return False


from YSE_App.models.night_plan_models import NightPlan, PlannedBlock  # noqa: E402


class _PlannedBlockInline(admin.TabularInline):
	model = PlannedBlock
	fields = ("order", "kind", "target_name", "start_ut", "end_ut", "followup", "priority_at_plan")
	readonly_fields = fields
	extra = 0
	can_delete = False

	def has_add_permission(self, request, obj=None):
		return False


@admin.register(NightPlan)
class NightPlanAdmin(admin.ModelAdmin):
	"""Night plans are saved and approved from the planner (#662, #663); read-only here."""
	list_display = ("id", "night", "version", "state", "author", "approved_by", "approved_at", "created_date")
	list_filter = ("state",)
	readonly_fields = ("night", "version", "parent", "state", "author", "approved_by", "approved_at",
	                   "solver", "solver_inputs", "notes", "created_by", "created_date")
	fields = readonly_fields
	inlines = (_PlannedBlockInline,)

	def has_add_permission(self, request):
		return False

	def has_change_permission(self, request, obj=None):
		return False

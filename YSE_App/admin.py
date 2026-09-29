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
admin.site.register(Telescope)
admin.site.register(Instrument)


class ObservingResourceAdmin(admin.ModelAdmin):
	list_display = ("__str__", "creator_only", "begin_date_valid", "end_date_valid")
	list_filter = ("creator_only",)
	filter_horizontal = ("groups",)


admin.site.register(ToOResource, ObservingResourceAdmin)
admin.site.register(ClassicalResource, ObservingResourceAdmin)
admin.site.register(QueuedResource, ObservingResourceAdmin)
admin.site.register(ClassicalObservingDate)
admin.site.register(InstrumentConfig)
admin.site.register(ConfigElement)
admin.site.register(PhotometricBand)
admin.site.register(PrincipalInvestigator)
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
admin.site.register(TransientSpectrum)
admin.site.register(HostSpectrum)
admin.site.register(TransientPhotometry)
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
	date_hierarchy = "created"
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
	list_display = ("user", "in_app", "email", "has_slack_webhook", "updated_at")
	list_filter = ("in_app", "email")
	search_fields = ("user__username",)
	raw_id_fields = ("user",)

	def has_slack_webhook(self, obj):
		return bool(obj.slack_webhook_url)
	has_slack_webhook.boolean = True
	has_slack_webhook.short_description = "slack"


# --- Broker filters and candidates (#276): criteria / query JSON edited as text.
@admin.register(BrokerFilter)
class BrokerFilterAdmin(admin.ModelAdmin):
	list_display = ("name", "broker", "group", "enabled", "auto_save", "save_status", "last_run_at", "last_run_summary")
	list_filter = ("broker", "enabled", "auto_save", "group")
	search_fields = ("name", "description")
	readonly_fields = ("last_run_at", "last_run_summary", "created_at", "updated_at")
	fieldsets = (
		(None, {"fields": ("name", "broker", "group", "description", "enabled")}),
		("Poll", {"fields": ("query", "criteria", "max_alerts"),
				  "description": "query: broker-side keys (see the provider's query_keys); criteria: "
								 "YSE_App.brokers.filters.CRITERIA keys, all must pass."}),
		("Saving", {"fields": ("auto_save", "save_status", "save_obs_group", "import_photometry")}),
		("Bookkeeping", {"fields": ("created_by", "last_run_at", "last_run_summary", "created_at", "updated_at")}),
	)

	def save_model(self, request, obj, form, change):
		if not change and not obj.created_by_id:
			obj.created_by = request.user
		super().save_model(request, obj, form, change)


@admin.register(Candidate)
class CandidateAdmin(admin.ModelAdmin):
	list_display = ("alert_id", "broker", "status", "last_mag", "last_band", "rb", "classification", "transient", "last_seen")
	list_filter = ("broker", "status")
	search_fields = ("alert_id", "classification", "transient__name")
	raw_id_fields = ("transient", "status_changed_by")
	readonly_fields = ("first_seen", "last_seen", "n_alerts", "created_at", "updated_at")
	filter_horizontal = ("filters",)


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
	list_display = ("id", "transient", "allocation", "state", "external_id", "submitted_by", "submitted_at",
	                "hours_charged", "charged_at")
	list_filter = ("state", "allocation__facility", "allocation")
	search_fields = ("transient__name", "external_id", "allocation__name")
	raw_id_fields = ("transient", "followup", "run")
	readonly_fields = ("charged_at", "created_by", "created_date", "modified_by", "modified_date")

	def save_model(self, request, obj, form, change):
		if not change or not obj.created_by_id:
			obj.created_by = request.user
		obj.modified_by = request.user
		super().save_model(request, obj, form, change)


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
	                "status", "decided_by", "decided_at", "created_date")
	list_filter = ("status", "dataset_kind", "owner_group")
	search_fields = ("transient__name", "requester__username", "message", "note")
	raw_id_fields = ("transient", "requester", "decided_by")
	readonly_fields = ("granted_dataset_ids", "created_by", "created_date", "modified_by", "modified_date")
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

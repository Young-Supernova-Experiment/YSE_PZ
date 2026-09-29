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

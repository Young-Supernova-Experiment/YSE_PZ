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


@admin.register(ExternalService)
class ExternalServiceAdmin(admin.ModelAdmin):
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

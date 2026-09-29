"""Django forms for the Allocations page (#305)."""

from __future__ import annotations

import json

from django import forms
from django.contrib.auth.models import Group

from YSE_App.facilities import facility_choices, get_facility
from YSE_App.models import Allocation, EncryptedCredential, Instrument, PrincipalInvestigator, Telescope


class AllocationForm(forms.ModelForm):
    """Create / edit an allocation; ``new_secret`` writes a fresh encrypted credential without echoing it."""

    facility = forms.ChoiceField(required=False, choices=())
    default_request_params_text = forms.CharField(
        required=False, label="Default request parameters (JSON object)",
        widget=forms.Textarea(attrs={"rows": 5, "class": "form-control", "spellcheck": "false"}),
        help_text="Merged under every request's parameters; also GENERIC settings "
                  "(notification_type, recipients, payload_template).",
    )
    new_secret = forms.CharField(
        required=False, label="New credential secret (JSON object)",
        widget=forms.Textarea(attrs={"rows": 3, "class": "form-control", "autocomplete": "off", "spellcheck": "false"}),
        help_text="Creates a new encrypted credential for this allocation and binds it; the value is never shown "
                  "again. Leave blank to keep the selected credential.",
    )
    new_secret_name = forms.CharField(required=False, label="Name for the new credential", max_length=128)

    class Meta:
        model = Allocation
        fields = [
            "name", "telescope", "instrument", "principal_investigator", "groups", "proposal_id",
            "hours_allocated", "hours_used", "start_date", "end_date", "facility", "credential",
            "endpoint_url", "is_active", "notes",
        ]
        widgets = {
            "start_date": forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"),
            "end_date": forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"),
            "notes": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["facility"].choices = facility_choices()
        self.fields["telescope"].queryset = Telescope.objects.order_by("name")
        self.fields["instrument"].queryset = Instrument.objects.select_related("telescope").order_by(
            "telescope__name", "name")
        self.fields["principal_investigator"].queryset = PrincipalInvestigator.objects.order_by("name")
        self.fields["groups"].queryset = Group.objects.order_by("name")
        self.fields["groups"].required = False
        self.fields["credential"].queryset = EncryptedCredential.objects.filter(is_active=True).order_by(
            "service", "name")
        self.fields["credential"].required = False
        self.fields["credential"].label_from_instance = lambda c: "%s (%s)%s" % (
            c.name, c.service, "" if c.has_secret else " - no secret")
        for name in ("start_date", "end_date"):
            self.fields[name].input_formats = ["%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S",
                                               "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"]
        if self.instance and self.instance.pk and not self.is_bound:
            self.fields["default_request_params_text"].initial = json.dumps(
                self.instance.default_request_params or {}, indent=2, sort_keys=True)

    def clean_default_request_params_text(self):
        text = (self.cleaned_data.get("default_request_params_text") or "").strip()
        if not text:
            return {}
        try:
            value = json.loads(text)
        except ValueError as exc:
            raise forms.ValidationError("Not valid JSON: %s" % exc)
        if not isinstance(value, dict):
            raise forms.ValidationError("Must be a JSON object ({...}).")
        return value

    def clean_new_secret(self):
        text = (self.cleaned_data.get("new_secret") or "").strip()
        if not text:
            return None
        try:
            value = json.loads(text)
        except ValueError as exc:
            raise forms.ValidationError("Not valid JSON: %s" % exc)
        if not isinstance(value, dict) or not value:
            raise forms.ValidationError("Must be a non-empty JSON object, e.g. {\"api_token\": \"...\"}.")
        return value

    def clean(self):
        cleaned = super().clean()
        start, end = cleaned.get("start_date"), cleaned.get("end_date")
        if start and end and end <= start:
            self.add_error("end_date", "must be after the start date")
        instrument = cleaned.get("instrument")
        telescope = cleaned.get("telescope")
        if instrument and telescope and instrument.telescope_id != telescope.pk:
            self.add_error("instrument", "belongs to %s, not %s" % (instrument.telescope.name, telescope.name))
        facility = cleaned.get("facility") or ""
        if facility and get_facility(facility) is None:
            self.add_error("facility", "unknown facility %r" % facility)
        return cleaned

    def save(self, user, commit=True):
        allocation = super().save(commit=False)
        allocation.facility = self.cleaned_data.get("facility") or ""
        allocation.default_request_params = self.cleaned_data.get("default_request_params_text") or {}
        if not allocation.created_by_id:
            allocation.created_by = user
        allocation.modified_by = user
        secret = self.cleaned_data.get("new_secret")
        if secret:
            name = (self.cleaned_data.get("new_secret_name") or "").strip() or "%s credential" % allocation.name
            service = allocation.facility or "generic"
            base, n = name, 1
            while EncryptedCredential.objects.filter(service=service, name=name).exists():
                n += 1
                name = "%s (%d)" % (base, n)
            cred = EncryptedCredential(name=name[:128], service=service, kind=EncryptedCredential.KIND_FACILITY,
                                       created_by=user, modified_by=user)
            cred.set_secret(secret)
            cred.save()
            allocation.credential = cred
        if commit:
            allocation.save()
            self.save_m2m()
            from YSE_App.services.allocations import ensure_service

            ensure_service(allocation, user)
        return allocation

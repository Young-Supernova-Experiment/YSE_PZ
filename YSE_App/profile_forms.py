"""Forms for the researcher profile, "My students" and the user agreement (#627, #628, #630)."""

from __future__ import annotations

from django import forms
from django.contrib.auth.models import User
from django.utils import timezone

from YSE_App.models import Instrument
from YSE_App.models.researcher_models import (
    CAREER_OTHER,
    POSTDOC_AND_ABOVE,
    STUDENT_LEVELS,
    Mentorship,
    ResearcherProfile,
)
from YSE_App.services import researcher_profile as rp

try:  # pytz is pinned in requirements.txt; validation is skipped if it is ever missing.
    import pytz
    _TIME_ZONES = frozenset(pytz.all_timezones)
except ImportError:  # pragma: no cover
    _TIME_ZONES = None

GENDER_CHOICES = (
    ("", "(not given)"),
    ("woman", "Woman"),
    ("man", "Man"),
    ("non_binary", "Non-binary"),
    ("self_describe", "Prefer to self-describe"),
    ("prefer_not_to_say", "Prefer not to say"),
)

ENCRYPTED_HELP = "Encrypted; even administrators cannot read it."


def _year_bounds():
    return rp.PHD_YEAR_MIN, rp.phd_year_max()


def active_user_or_none(user_id):
    if not user_id:
        return None
    try:
        return User.objects.get(pk=int(user_id), is_active=True)
    except (User.DoesNotExist, TypeError, ValueError):
        return None


class AgreementForm(forms.Form):
    agree = forms.BooleanField(
        required=True, label="I have read and agree to the YSE-PZ User Agreement",
        error_messages={"required": "Tick the box to accept the agreement."},
    )
    next = forms.CharField(required=False, widget=forms.HiddenInput)


class ResearcherProfileForm(forms.ModelForm):
    """Plain fields are model fields; the encrypted ones are validated here, then encrypted by the view."""

    # Encrypted (never pre-filled: the server cannot read stored values). Blank keeps the stored value.
    country = forms.CharField(max_length=64, required=False, help_text=ENCRYPTED_HELP)
    phd_year = forms.IntegerField(required=False, help_text=ENCRYPTED_HELP)
    gender = forms.ChoiceField(choices=GENDER_CHOICES, required=False, help_text=ENCRYPTED_HELP)
    gender_self = forms.CharField(max_length=100, required=False, label="Gender (self-described)")
    clear_country = forms.BooleanField(required=False, label="Delete my stored country")
    clear_phd_year = forms.BooleanField(required=False, label="Delete my stored PhD year")
    clear_gender = forms.BooleanField(required=False, label="Delete my stored gender")
    # Current advisor (students): a YSE-PZ user picked from the list, or a free-form name.
    advisor_name = forms.CharField(max_length=200, required=False, label="Current advisor")
    advisor_user_id = forms.CharField(required=False, widget=forms.HiddenInput)

    class Meta:
        model = ResearcherProfile
        fields = (
            "full_name", "preferred_name", "pronouns", "author_name_format",
            "institution", "department", "career_level", "career_level_other",
            "orcid", "research_interests", "website", "github_username", "time_zone",
            "observing_experience", "acknowledgment_text",
        )
        widgets = {
            "research_interests": forms.Textarea(attrs={"rows": 3}),
            "acknowledgment_text": forms.Textarea(attrs={"rows": 3}),
            "observing_experience": forms.SelectMultiple(attrs={"size": 8}),
        }

    def __init__(self, *args, user=None, key_available=False, encrypted_status=None, advisor=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        self.key_available = key_available
        self.encrypted_status = encrypted_status or {}
        self.current_advisor = advisor
        self.fields["observing_experience"].queryset = (
            Instrument.objects.select_related("telescope").order_by("telescope__name", "name"))
        self.fields["observing_experience"].label_from_instance = (
            lambda i: "%s / %s" % (i.telescope.name, i.name))
        self.fields["career_level"].choices = [("", "(choose)")] + list(self.fields["career_level"].choices)[1:]
        lo, hi = _year_bounds()
        self.fields["phd_year"].widget.attrs.update({"min": lo, "max": hi})
        self.fields["orcid"].help_text = "0000-0000-0000-0000 (required for postdocs and above)."
        self.fields["orcid"].widget.attrs["placeholder"] = "0000-0000-0000-0000"
        self.fields["time_zone"].help_text = "IANA name, e.g. America/Los_Angeles."
        for name in rp.required_fields("", key_available):
            self.fields[name].required = True
        if not key_available:
            for name in ("country", "phd_year", "gender", "gender_self",
                         "clear_country", "clear_phd_year", "clear_gender"):
                self.fields[name].disabled = True
        if advisor is not None and not self.is_bound:
            self.initial.setdefault("advisor_name", advisor.mentor_label)
            if advisor.mentor_id:
                self.initial.setdefault("advisor_user_id", str(advisor.mentor_id))

    # -- field cleaning ----------------------------------------------------
    def clean_orcid(self):
        value = rp.normalize_orcid(self.cleaned_data.get("orcid", ""))
        if value and not rp.orcid_checksum_ok(value):
            raise forms.ValidationError("Not a valid ORCID iD (format 0000-0000-0000-0000 with a valid check digit).")
        return value

    def clean_time_zone(self):
        value = (self.cleaned_data.get("time_zone") or "").strip()
        if value and _TIME_ZONES is not None and value not in _TIME_ZONES:
            raise forms.ValidationError("Unknown time zone; use an IANA name such as Europe/Copenhagen.")
        return value

    def clean_github_username(self):
        value = (self.cleaned_data.get("github_username") or "").strip().lstrip("@")
        if value and not all(c.isalnum() or c == "-" for c in value):
            raise forms.ValidationError("GitHub usernames contain only letters, digits and hyphens.")
        return value

    def clean_phd_year(self):
        # Validated BEFORE encryption: the server cannot check it afterwards.
        value = self.cleaned_data.get("phd_year")
        if value is None:
            return None
        lo, hi = _year_bounds()
        if not lo <= value <= hi:
            raise forms.ValidationError("Enter a year between %d and %d." % (lo, hi))
        return value

    def clean_country(self):
        return (self.cleaned_data.get("country") or "").strip()

    def clean_gender_self(self):
        return (self.cleaned_data.get("gender_self") or "").strip()

    def clean(self):
        data = super().clean()
        level = data.get("career_level") or ""
        if level == CAREER_OTHER and not (data.get("career_level_other") or "").strip():
            self.add_error("career_level_other", "Describe your career level.")
        if level in POSTDOC_AND_ABOVE:
            if not data.get("orcid") and "orcid" not in self.errors:
                self.add_error("orcid", "An ORCID iD is required for postdocs and above.")
            if self.key_available:
                stored = "phd_year" in self.encrypted_status and not data.get("clear_phd_year")
                if data.get("phd_year") is None and not stored and "phd_year" not in self.errors:
                    self.add_error("phd_year", "The PhD year is required for postdocs and above.")
        if self.key_available and data.get("gender") == "self_describe" and not data.get("gender_self"):
            self.add_error("gender_self", "Describe your gender, or choose another option.")
        advisor_user = active_user_or_none(data.get("advisor_user_id"))
        if advisor_user is not None and self.user is not None and advisor_user.pk == self.user.pk:
            self.add_error("advisor_name", "You cannot be your own advisor.")
            advisor_user = None
        data["advisor_user"] = advisor_user
        if level in STUDENT_LEVELS:
            given = advisor_user is not None or (data.get("advisor_name") or "").strip()
            if not given and self.current_advisor is None:
                self.add_error("advisor_name", "Students must give their current advisor.")
        return data

    # -- values for the encrypted fields ------------------------------------
    def encrypted_changes(self):
        """``(to_set, to_clear)``: {field: plaintext} and [field] from the validated data."""
        if not self.key_available:
            return {}, []
        data = self.cleaned_data
        to_set, to_clear = {}, []
        for name in ("country", "phd_year", "gender"):
            if data.get("clear_" + name):
                to_clear.append(name)
        if data.get("country") and "country" not in to_clear:
            to_set["country"] = data["country"]
        if data.get("phd_year") is not None and "phd_year" not in to_clear:
            to_set["phd_year"] = str(data["phd_year"])
        gender = data.get("gender")
        if gender and "gender" not in to_clear:
            to_set["gender"] = ("self_describe:%s" % data["gender_self"]) if gender == "self_describe" else gender
        return to_set, to_clear


class StudentForm(forms.Form):
    """Add a student to "My students" (a YSE-PZ user or a free-form name)."""

    student_name = forms.CharField(max_length=200, required=False, label="Student")
    student_user_id = forms.CharField(required=False, widget=forms.HiddenInput)
    kind = forms.ChoiceField(choices=Mentorship.KIND_CHOICES, initial=Mentorship.KIND_CURRENT_ADVISOR)
    start_year = forms.IntegerField(required=False)
    end_year = forms.IntegerField(required=False)

    def __init__(self, *args, mentor=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.mentor = mentor

    def _check_year(self, name):
        value = self.cleaned_data.get(name)
        if value is not None and not 1950 <= value <= timezone.now().year + 10:
            raise forms.ValidationError("Enter a year between 1950 and %d." % (timezone.now().year + 10))
        return value

    def clean_start_year(self):
        return self._check_year("start_year")

    def clean_end_year(self):
        return self._check_year("end_year")

    def clean(self):
        data = super().clean()
        student = active_user_or_none(data.get("student_user_id"))
        if student is not None and self.mentor is not None and student.pk == self.mentor.pk:
            self.add_error("student_name", "You cannot add yourself as a student.")
            student = None
        data["student_user"] = student
        if student is None and not (data.get("student_name") or "").strip():
            self.add_error("student_name", "Choose a user or type the student's name.")
        start, end = data.get("start_year"), data.get("end_year")
        if start and end and end < start:
            self.add_error("end_year", "The end year is before the start year.")
        return data


class StudentUpdateForm(forms.Form):
    end_year = forms.IntegerField(required=False)
    remove = forms.BooleanField(required=False)

    def clean_end_year(self):
        value = self.cleaned_data.get("end_year")
        if value is not None and not 1950 <= value <= timezone.now().year + 10:
            raise forms.ValidationError("Enter a valid year.")
        return value

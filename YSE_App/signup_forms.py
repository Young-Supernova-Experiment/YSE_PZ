"""Forms for self-service signup and team membership requests (#448)."""

from __future__ import annotations

import time

from django import forms
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.models import Group, User
from django.core import signing

from YSE_App.services import membership
from YSE_App.services import signup as signup_svc

_TS_SALT = "yse-signup-form-ts"
HONEYPOT_FIELD = "website"


class SignupForm(UserCreationForm):
    """Given and family names, username, email and password (Django's password validators),
    plus the spam guard fields. The names go on the account (first_name / last_name)."""

    first_name = forms.CharField(
        max_length=150, label="Given name(s)", help_text="First and any middle names, as you write them.",
        widget=forms.TextInput(attrs={"autocomplete": "given-name", "class": "form-control"}),
    )
    last_name = forms.CharField(
        max_length=150, label="Family name", help_text="Surname(s).",
        widget=forms.TextInput(attrs={"autocomplete": "family-name", "class": "form-control"}),
    )

    email = forms.EmailField(
        max_length=254, label="Email address",
        help_text="Used for notifications and password resets. We send a link to confirm it.",
        widget=forms.EmailInput(attrs={"autocomplete": "email", "class": "form-control"}),
    )
    # Honeypot: hidden from people with CSS, but bots fill it in.
    website = forms.CharField(required=False, label="Leave this field empty",
                              widget=forms.TextInput(attrs={"autocomplete": "off", "tabindex": "-1"}))
    form_ts = forms.CharField(required=False, widget=forms.HiddenInput)
    # Optional: requests are opened at once but held until onboarding is done.
    teams = forms.ModelMultipleChoiceField(
        queryset=Group.objects.none(), required=False, widget=forms.CheckboxSelectMultiple,
        label="Teams you would like to join (optional)",
        help_text="The team owners decide. You can also ask later from My teams.",
    )
    team_message = forms.CharField(
        required=False, max_length=membership.MESSAGE_MAX_LENGTH, label="Note to the team owners (optional)",
        widget=forms.Textarea(attrs={"rows": 2, "class": "form-control"}),
    )

    class Meta(UserCreationForm.Meta):
        model = User
        fields = ("first_name", "last_name", "username", "email")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.is_bound:
            self.initial["form_ts"] = signing.dumps(int(time.time()), salt=_TS_SALT)
        for name in ("username", "password1", "password2"):
            self.fields[name].widget.attrs.setdefault("class", "form-control")
        self.fields["username"].widget.attrs["autocomplete"] = "username"
        offered = membership.signup_groups()
        if offered:
            self.fields["teams"].queryset = Group.objects.filter(pk__in=[g.pk for g in offered]).order_by("name")
        else:
            del self.fields["teams"]
            del self.fields["team_message"]

    def is_spam(self) -> bool:
        """True when the honeypot is filled or the form came back too quickly (or unsigned)."""
        if (self.data.get(HONEYPOT_FIELD) or "").strip():
            return True
        try:
            started = signing.loads(self.data.get("form_ts") or "", salt=_TS_SALT, max_age=86400)
        except signing.BadSignature:
            return True
        return (time.time() - float(started)) < signup_svc.min_fill_seconds()

    def clean_username(self):
        username = self.cleaned_data.get("username")
        if username and User.objects.filter(username__iexact=username).exists():
            raise forms.ValidationError("A user with that username already exists.")
        return username

    def clean_teams(self):
        teams = self.cleaned_data.get("teams")
        limit = membership.signup_max_requests()
        if teams is not None and limit > 0 and len(teams) > limit:
            raise forms.ValidationError("Choose at most %d teams; you can ask to join more later." % limit)
        return teams

    def clean_first_name(self):
        return " ".join((self.cleaned_data.get("first_name") or "").split())

    def clean_last_name(self):
        return " ".join((self.cleaned_data.get("last_name") or "").split())

    def clean_email(self):
        return (self.cleaned_data.get("email") or "").strip()


class EmailConfirmForm(forms.Form):
    email = forms.EmailField(max_length=254, label="Email address",
                             widget=forms.EmailInput(attrs={"autocomplete": "email", "class": "form-control"}))


class MembershipRequestForm(forms.Form):
    group = forms.IntegerField(widget=forms.HiddenInput)
    message = forms.CharField(
        required=False, max_length=membership.MESSAGE_MAX_LENGTH, label="Note to the owners (optional)",
        widget=forms.Textarea(attrs={"rows": 2, "class": "form-control"}),
    )

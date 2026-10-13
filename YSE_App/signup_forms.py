"""Forms for self-service signup and team membership requests (#448)."""

from __future__ import annotations

import time

from django import forms
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.models import User
from django.core import signing

from YSE_App.services import membership
from YSE_App.services import signup as signup_svc

_TS_SALT = "yse-signup-form-ts"
HONEYPOT_FIELD = "website"


class SignupForm(UserCreationForm):
    """Username, email and password (Django's password validators), plus the spam guard fields."""

    email = forms.EmailField(
        max_length=254, label="Email address",
        help_text="Used for notifications and password resets. We send a link to confirm it.",
        widget=forms.EmailInput(attrs={"autocomplete": "email", "class": "form-control"}),
    )
    # Honeypot: hidden from people with CSS, but bots fill it in.
    website = forms.CharField(required=False, label="Leave this field empty",
                              widget=forms.TextInput(attrs={"autocomplete": "off", "tabindex": "-1"}))
    form_ts = forms.CharField(required=False, widget=forms.HiddenInput)

    class Meta(UserCreationForm.Meta):
        model = User
        fields = ("username", "email")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.is_bound:
            self.initial["form_ts"] = signing.dumps(int(time.time()), salt=_TS_SALT)
        for name in ("username", "password1", "password2"):
            self.fields[name].widget.attrs.setdefault("class", "form-control")
        self.fields["username"].widget.attrs["autocomplete"] = "username"

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

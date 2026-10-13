"""Forms for the team pages and the user settings page (#445, #440)."""

from __future__ import annotations

from django import forms
from django.contrib.auth.models import Group

from YSE_App.models.group_models import GroupProfile, UserPreference
from YSE_App.services import groups as group_svc


class InvitationForm(forms.Form):
    days = forms.IntegerField(
        min_value=1, label="Link expires after (days)",
        widget=forms.NumberInput(attrs={"class": "form-control form-control-sm", "style": "width: 6em"}),
    )
    max_uses = forms.IntegerField(
        min_value=1, initial=1, label="Number of people who can use it",
        widget=forms.NumberInput(attrs={"class": "form-control form-control-sm", "style": "width: 6em"}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["days"].initial = group_svc.invite_default_days()
        self.fields["days"].max_value = group_svc.invite_max_days()
        self.fields["days"].widget.attrs["max"] = group_svc.invite_max_days()
        self.fields["max_uses"].max_value = group_svc.invite_max_uses_limit()
        self.fields["max_uses"].widget.attrs["max"] = group_svc.invite_max_uses_limit()
        # max_value set after construction needs its validator added explicitly.
        from django.core.validators import MaxValueValidator
        self.fields["days"].validators.append(MaxValueValidator(group_svc.invite_max_days()))
        self.fields["max_uses"].validators.append(MaxValueValidator(group_svc.invite_max_uses_limit()))


class GroupProfileForm(forms.ModelForm):
    class Meta:
        model = GroupProfile
        fields = ("description", "is_active")
        labels = {"is_active": "Active (shown in group pickers)"}
        widgets = {"description": forms.Textarea(attrs={"rows": 3, "class": "form-control"})}


class _GroupChoices(forms.ModelMultipleChoiceField):
    def label_from_instance(self, obj):
        profile = getattr(obj, "group_profile", None)
        if profile is not None and not profile.is_active:
            return "%s (inactive)" % obj.name
        return obj.name


class UserPreferenceForm(forms.ModelForm):
    hidden_groups = _GroupChoices(
        queryset=Group.objects.none(), required=False, widget=forms.CheckboxSelectMultiple,
        label="Hide these groups in pickers",
        help_text="You stay a member and keep seeing their data; they are just not offered when you "
                  "choose who can see a comment, follow-up request or interest. Public is always offered.",
    )
    comment_default_groups = _GroupChoices(
        queryset=Group.objects.none(), required=False, widget=forms.CheckboxSelectMultiple,
        label="Default groups for comments",
        help_text="Pre-checked when you write a comment. None checked: every group you share with the transient.",
    )
    followup_default_groups = _GroupChoices(
        queryset=Group.objects.none(), required=False, widget=forms.CheckboxSelectMultiple,
        label="Default groups for follow-up requests",
        help_text="Pre-checked when you request follow-up. None checked: every eligible group.",
    )
    comment_default_public = forms.BooleanField(
        required=False, label='Tick "Public" on new comments by default',
    )

    class Meta:
        model = UserPreference
        fields = ("hidden_groups", "comment_default_groups", "followup_default_groups", "comment_default_public")

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        excluded = group_svc.picker_excluded_system_ids(user)
        qs = user.groups.exclude(id__in=excluded).select_related("group_profile").order_by("name")
        for name in ("hidden_groups", "comment_default_groups", "followup_default_groups"):
            self.fields[name].queryset = qs
        # Public is always shown in pickers (#440): it cannot be hidden.
        self.fields["hidden_groups"].queryset = qs.exclude(id__in=group_svc.public_group_ids(user))
        if self.instance is not None and self.instance.comment_default_public is not None:
            self.initial["comment_default_public"] = bool(self.instance.comment_default_public)

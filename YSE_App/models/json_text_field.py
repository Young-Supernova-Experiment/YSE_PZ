"""A JSON-in-TEXT field that works on every MySQL/MariaDB version YSE-PZ runs on.

Django 3.2's ``models.JSONField`` needs MySQL 5.7.8+ / MariaDB 10.2.7+ and the
sqlite JSON1 extension; the Ziggy databases are not guaranteed to have either,
so the credential and external-service tables store JSON as text and (de)serialise
it in Python. Values are plain dicts/lists on the model instance.
"""

from __future__ import annotations

import json

from django.core.exceptions import ValidationError
from django.db import models


class JSONTextField(models.TextField):
    description = "JSON document stored as text"

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("default", dict)
        kwargs.setdefault("blank", True)
        super().__init__(*args, **kwargs)

    def deconstruct(self):
        name, path, args, kwargs = super().deconstruct()
        return name, path, args, kwargs

    def from_db_value(self, value, expression, connection):
        return self.to_python(value)

    def to_python(self, value):
        if value is None or value == "":
            return {} if self.default is dict else None
        if isinstance(value, (dict, list)):
            return value
        try:
            return json.loads(value)
        except (TypeError, ValueError) as exc:
            raise ValidationError("Value is not valid JSON.") from exc

    def get_prep_value(self, value):
        if value is None:
            return None
        if isinstance(value, str):
            # Accept pre-serialised text (admin textarea) but validate it.
            json.loads(value)
            return value
        return json.dumps(value, sort_keys=True, default=str)

    def value_to_string(self, obj):
        return self.get_prep_value(self.value_from_object(obj))

    def formfield(self, **kwargs):
        from django import forms

        defaults = {"widget": forms.Textarea(attrs={"rows": 6, "cols": 80})}
        defaults.update(kwargs)
        field = super().formfield(**defaults)
        return field

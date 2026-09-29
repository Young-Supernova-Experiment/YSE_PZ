"""Small custom model fields shared by the job and notification tables."""

import json

from django.core.exceptions import ValidationError
from django.db import models


class JSONTextField(models.TextField):
    """JSON document stored as text.

    The schema has no native ``JSONField`` yet and the MySQL version on the
    production host is not pinned, so the payload/result/delivery columns
    are plain ``TEXT`` holding ``json.dumps`` output. Python sees dicts and
    lists; ``None`` is stored as SQL NULL when the field allows it.
    """

    description = "JSON document stored as text"

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("blank", True)
        super().__init__(*args, **kwargs)

    def from_db_value(self, value, expression, connection):
        return self.to_python(value)

    def to_python(self, value):
        if value is None or isinstance(value, (dict, list, int, float, bool)):
            return value
        if value == "":
            return None
        try:
            return json.loads(value)
        except (TypeError, ValueError) as exc:
            raise ValidationError("Invalid JSON: %s" % exc)

    def get_prep_value(self, value):
        if value is None:
            return None
        return json.dumps(value, default=str, sort_keys=True)

    def value_to_string(self, obj):
        return self.get_prep_value(self.value_from_object(obj)) or ""

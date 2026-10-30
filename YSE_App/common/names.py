"""Split a researcher's full name into given and family names, only when it is unambiguous.

Accepted (everything else returns None, and the person fills the two fields themselves):

* two plain words: ``"Ada Lovelace"`` -> ``("Ada", "Lovelace")``;
* three plain words where one of the first two is an initial and the last is not:
  ``"Ada B. Lovelace"`` -> ``("Ada B.", "Lovelace")``, ``"A. Byron Lovelace"`` ->
  ``("A. Byron", "Lovelace")``.

A plain word is letters (any script) with internal hyphens or apostrophes; an initial is
one letter, optionally followed by a period. A given name made of an initial alone
(``"A. Lovelace"``), particles, suffixes, commas and longer names are left alone. Pure
code (no models), so migrations can import it.
"""

from __future__ import annotations

import re
from typing import Optional, Tuple

_WORD = re.compile(r"^[^\W\d_]+(?:['’-][^\W\d_]+)*$")
_INITIAL = re.compile(r"^[^\W\d_]\.?$")


def _is_initial(token: str) -> bool:
    return bool(_INITIAL.match(token))


def _is_word(token: str) -> bool:
    """A plain word of two or more letters (not an initial)."""
    return bool(_WORD.match(token)) and not _is_initial(token)


def split_full_name(full_name: str) -> Optional[Tuple[str, str]]:
    """``(given, family)`` for an unambiguous full name, else None."""
    tokens = (full_name or "").split()
    if len(tokens) == 2:
        given, family = tokens
        if _is_word(given) and _is_word(family):
            return given, family
        return None
    if len(tokens) == 3:
        first, middle, family = tokens
        if not _is_word(family):
            return None
        kinds = [_is_initial(first), _is_initial(middle)]
        words = [_is_word(first), _is_word(middle)]
        # Exactly one initial and one word among the given names.
        if sum(kinds) == 1 and sum(words) == 1:
            return "%s %s" % (first, middle), family
    return None

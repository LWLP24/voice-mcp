from __future__ import annotations

import re

_LANGUAGE_CODE = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")


def normalize_language_code(value: str) -> str:
    """Validate and normalize a compact BCP-47 language tag."""
    normalized = value.strip().replace("_", "-")
    if not _LANGUAGE_CODE.fullmatch(normalized):
        raise ValueError("language must be a BCP-47 code such as 'de', 'en', or 'de-DE'")

    parts = normalized.split("-")
    result = [parts[0].lower()]
    for part in parts[1:]:
        if len(part) == 2 and part.isalpha():
            result.append(part.upper())
        elif len(part) == 4 and part.isalpha():
            result.append(part.title())
        else:
            result.append(part.lower())
    return "-".join(result)

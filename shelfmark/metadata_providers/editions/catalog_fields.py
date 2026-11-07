"""Small strict field readers shared by the two opt-in catalog adapters."""

import math
import re
from datetime import date, datetime
from typing import TYPE_CHECKING, cast

from shelfmark.metadata_providers.editions.catalog_types import SourceError
from shelfmark.metadata_providers.editions.multisource import normalized_isbn
from shelfmark.metadata_providers.editions.search_ranking import ADAPTATION, PART

if TYPE_CHECKING:
    from collections.abc import Mapping


def obj(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(k, str) for k in value):
        raise SourceError("Expected a catalog object")
    return cast("dict[str, object]", value)


def rows(value: object) -> list[object]:
    if not isinstance(value, list) or len(value) > 1000:
        raise SourceError("Expected a bounded catalog list")
    return cast("list[object]", value)


def text(data: Mapping[str, object], key: str, *, required: bool = False) -> str | None:
    value = data.get(key)
    if value is None and not required:
        return None
    if not isinstance(value, str) or len(value) > 8192 or (required and not value.strip()):
        raise SourceError("Invalid catalog string: " + key)
    return value.strip() or None


def names(data: Mapping[str, object], key: str) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            name
            for row in optional_rows(data, key)
            if (name := text(obj(row), "name", required=True))
        )
    )


def optional_rows(data: Mapping[str, object], key: str) -> list[object]:
    return [] if data.get(key) is None else rows(data[key])


def number(value: object) -> float | None:
    if value is None:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        raise SourceError("Invalid catalog numeric field")
    return float(value)


def released(value: str | None) -> date | None:
    if not value:
        return None
    try:
        # Both reviewed APIs use ISO-8601 release dates, never publication years.
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:T[^\s]+)?", value):
            raise ValueError("Invalid date")  # noqa: TRY301 -- Keep validation beside the parsed field.
        return (
            date.fromisoformat(value) if len(value) == 10 else datetime.fromisoformat(value).date()
        )
    except ValueError:
        raise SourceError("Invalid recording release date") from None


def isbn_fields(value: str | None) -> tuple[str | None, str | None]:
    if not value:
        return None, None
    normalized = normalized_isbn(value)
    if normalized is None:
        raise SourceError("Invalid recording ISBN")
    compact = re.sub(r"[-\s]", "", value).upper()
    return (compact if len(compact) == 10 else None), normalized


def recording_labels(
    title: str, subtitle: str | None, publisher: str | None, disambiguation: str | None = None
) -> tuple[int | None, int | None, str | None]:
    label = " ".join(v for v in (title, subtitle, disambiguation) if v)
    parts = {(int(m[1] or m[3]), int(m[2]) if m[2] else None) for m in PART.finditer(label)}
    if len(parts) > 1:
        raise SourceError("Contradictory recording parts")
    part, total = next(iter(parts), (None, None))
    dramatized = bool(
        ADAPTATION.search(label)
        or re.fullmatch(r"graphic\s*audio(?:,?\s+llc)?", publisher or "", re.IGNORECASE)
    )
    conventional = bool(re.search(r"\b(?:conventional|single[- ]narrator)\b", label, re.IGNORECASE))
    if dramatized and conventional:
        raise SourceError("Contradictory recording production")
    return part, total, "dramatized" if dramatized else "conventional" if conventional else None

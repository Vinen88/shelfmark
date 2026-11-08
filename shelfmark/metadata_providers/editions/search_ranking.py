"""Query ranking only: never rewrite edition titles or use similarity as identity."""

import json
import re
import unicodedata
from difflib import SequenceMatcher
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from shelfmark.metadata_providers.editions.catalog_types import SourceRecord

PART = re.compile(r"\b(?:part\s+)?(\d+)\s+of\s+(\d+)\b|\bpart\s+(\d+)\b", re.IGNORECASE)
ADAPTATION = re.compile(r"\b(?:dramatized\s+adaptation|graphic\s*audio)\b", re.IGNORECASE)


def language(value: str | None) -> str | None:
    if not value or not value.strip():
        return None
    token = value.strip().lower().replace("_", "-").split("-")[0]
    return {
        "english": "en",
        "eng": "en",
        "french": "fr",
        "fra": "fr",
        "fre": "fr",
        "german": "de",
        "deu": "de",
        "spanish": "es",
        "spa": "es",
    }.get(token, token)


def words(value: str) -> str:
    return " ".join(re.findall(r"[^\W_]+", unicodedata.normalize("NFKC", value).casefold()))


def base_title(value: str) -> str:
    return words(PART.sub(" ", ADAPTATION.sub(" ", value)))


def fact(record: SourceRecord, key: str) -> object:
    raw = next((value for name, value in record.facts if name == key), "null")
    try:
        value: object = json.loads(raw)
        return value  # noqa: TRY300 -- Keep the existing nonblocking failure boundary.
    except ValueError:
        return None


def literary_title(value: str) -> str:
    """Remove explicit recording labels for display/ranking, never identity."""
    value = re.sub(
        r"[\[(](?:full[- ]cast editions?|narrated by [^\])]+)[\])]", "", value, flags=re.IGNORECASE
    )
    value = PART.sub("", ADAPTATION.sub("", value)).strip(" ,:;()[]")
    return re.sub(r",?\s+Book\s+\d+(?:\.\d+)?\s*$", "", value, flags=re.IGNORECASE).strip(
        " ,:;()[]"
    )


def relevance(
    record: SourceRecord, query: str, search_type: str
) -> tuple[int, int, int, float, int, float, int]:
    edition = record.edition
    needle = words(literary_title(query))
    haystack = base_title(
        " ".join(edition.authors) if search_type == "author" else literary_title(edition.title)
    )
    score = SequenceMatcher(None, needle, haystack, autojunk=False).ratio()
    series_match = bool(needle and needle == words(literary_title(edition.series_name or "")))
    author_match = search_type == "general" and needle in {words(a) for a in edition.authors}
    if needle == haystack:
        match = 0
    elif series_match or author_match:
        match = 1
    elif score >= 0.9:
        match = 2
    elif needle and (needle in haystack or set(needle.split()) <= set(haystack.split())):
        match = 3
    else:
        match = 4
    # Sparse podcast responses must not outrank a recording just because their
    # title repeats the query. Unknown formats are retained, never filtered out.
    format_value = fact(record, "bookFormat")
    recording = (
        format_value in {"abridged", "unabridged"} if isinstance(format_value, str) else False
    )
    complete = bool(
        edition.narrators and edition.publisher and (recording or edition.asin or edition.isbn_13)
    )
    episode = (
        format_value == "original_recording"
        and not complete
        and bool(
            re.search(
                r"\b(chapters?|episodes?|podcast|review|discussion)\b", edition.title, re.IGNORECASE
            )
        )
    )
    kind = 2 if episode else 0 if complete else 1
    wanted = PART.search(query)
    wanted_part = int(wanted[1] or wanted[3]) if wanted else None
    part_penalty = (
        0
        if wanted_part is None or edition.part_number == wanted_part
        else 1
        if edition.part_number is None
        else 2
    )
    adaptation_penalty = int(bool(ADAPTATION.search(query)) and record.production != "dramatized")
    position = (
        edition.series_position
        if series_match and edition.series_position is not None
        else float("inf")
    )
    return (
        int(episode),
        match,
        part_penalty + adaptation_penalty,
        position,
        kind,
        -score,
        abs(len(needle) - len(haystack)),
    )

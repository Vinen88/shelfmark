"""Exact recording ownership and a separate, display-only literary work projection."""

import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Protocol

from shelfmark.metadata_providers import BookMetadata
from shelfmark.metadata_providers.editions.catalog_types import AggregatedEdition
from shelfmark.metadata_providers.editions.identity import EditionIdentity
from shelfmark.metadata_providers.editions.multisource import encode_identity, normalized_isbn
from shelfmark.metadata_providers.editions.search_ranking import (
    ADAPTATION,
    PART,
    language,
    literary_title,
    words,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from shelfmark.metadata_providers.editions.models import AudiobookEdition

PROVIDER = "edition_metadata"


def asin(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    result = value.strip().upper()
    return result if re.fullmatch(r"[A-Z0-9]{10}", result) else None


def publisher_name(value: str) -> str:
    normalized = words(value)
    return (
        "graphicaudio"
        if normalized.replace(" ", "") in {"graphicaudio", "graphicaudiollc"}
        else normalized
    )


@dataclass(frozen=True, slots=True)
class EditionOwnership:
    identity: EditionIdentity
    external_ids: frozenset[tuple[str, str]]
    asins: frozenset[str]
    recording_isbns: frozenset[str]
    narrators: frozenset[str]
    publisher: str | None
    language: str | None
    production: str | None
    part: int | None
    total: int | None


@dataclass
class EditionBookMetadata(BookMetadata):
    # Display-only relationships; never used as recording or queue identity.
    edition_work_id: str | None = None
    edition_work_title: str | None = None
    edition_series_name: str | None = None

    # Deliberately not a dataclass field: native asdict/API/request JSON stays
    # unchanged. dataclasses.replace drops this context and thus fails closed.
    _edition_ownership: ClassVar[EditionOwnership | None] = None


def ownership_evidence(edition: AudiobookEdition) -> EditionOwnership:
    ids = {(PROVIDER, edition.provider_id)}
    asins: set[str] = set()
    isbns: set[str] = set()
    productions: set[str] = set()
    if isinstance(edition, AggregatedEdition):
        # Only our trusted producer's grouped references qualify. Never parse
        # ASINs from display fields or assume arbitrary catalog IDs are ASINs.
        for ref in edition.provenance.references:
            values: dict[str, object] = {
                v.field: json.loads(v.value_json)
                for v in edition.provenance.values
                if v.source == ref
            }
            if values.get("identity.asin_verified") is True and (value := asin(values.get("asin"))):
                asins.add(value)
            raw = values.get("identity.recording_isbns")
            if isinstance(raw, list):
                isbns.update(
                    value
                    for item in raw
                    if isinstance(item, str) and (value := normalized_isbn(item))
                )
            production = values.get("recording.production")
            if isinstance(production, str):
                productions.add(production)
            ids.add((PROVIDER, "aggregate:" + encode_identity(ref)))
    elif value := asin(edition.asin):
        # The base EditionBackend contract supplies a release-level ASIN.
        # It has no ISBN-scope attestation; raw model ISBNs do not become proof.
        asins.add(value)
    if ADAPTATION.search(edition.edition_label or "") or ADAPTATION.search(edition.title):
        productions.add("dramatized")
    production = next(iter(productions)) if len(productions) == 1 else None
    return EditionOwnership(
        EditionIdentity(PROVIDER, edition.provider_id),
        frozenset(ids),
        frozenset(asins),
        frozenset(isbns),
        frozenset(words(n) for n in edition.narrators),
        publisher_name(edition.publisher) if edition.publisher else None,
        language(edition.language),
        production,
        edition.part_number,
        edition.part_total,
    )


@dataclass(frozen=True, slots=True)
class LibraryRecording:
    raw_isbn: str | None
    narrators: frozenset[str]
    publisher: str | None
    language: str | None
    production: str | None
    part: int | None
    total: int | None
    valid: bool = True
    work_title: str = ""
    authors: frozenset[str] = frozenset()
    series: frozenset[tuple[str, float]] = frozenset()


def _series_position(value: object) -> float | None:
    text = str(value)
    return float(text) if re.fullmatch(r"\d+(?:\.\d+)?", text) else None


def library_recording(
    item: Mapping[str, object], *, work_title: str | None = None
) -> LibraryRecording:
    """Read actual ABS fields; malformed new fields never break native indexing."""
    media = item.get("media")
    raw = media.get("metadata") if isinstance(media, dict) else None
    meta: Mapping[str, object] = raw if isinstance(raw, dict) else {}
    valid = isinstance(raw, dict)

    def text(key: str) -> str | None:
        nonlocal valid
        value = meta.get(key)
        if value is None or value == "":
            return None
        if not isinstance(value, str):
            valid = False
            return None
        return value.strip() or None

    title = text("title") or ""
    raw_isbn, publisher, lang = text("isbn"), text("publisher"), text("language")
    narrators = meta.get("narrators")
    names: list[str] = []
    if narrators is not None:
        if isinstance(narrators, list) and all(isinstance(name, str) for name in narrators):
            names = [name for name in narrators if isinstance(name, str)]
        else:
            valid = False
    if not names and (display := text("narratorName")):
        names = display.split(", ")  # ABS's flattened joined-name field.
    part = PART.search(title)
    production = "dramatized" if ADAPTATION.search(title) else None
    # Only explicit labels establish the conventional style; "unabridged"
    # describes abridgement and is deliberately not a style classification.
    if re.search(r"\b(?:conventional|single[- ]narrator)\b", title, re.IGNORECASE):
        production = "conventional"
    authors = meta.get("authors")
    author_names = (
        [
            row["name"]
            for row in authors
            if isinstance(row, dict) and isinstance(row.get("name"), str)
        ]
        if isinstance(authors, list)
        else []
    )
    author_display = meta.get("authorName")
    if not author_names and isinstance(author_display, str):
        author_names = [author_display]
    series: set[tuple[str, float]] = set()
    raw_series = meta.get("series")
    if isinstance(raw_series, list):
        for row in raw_series:
            if isinstance(row, dict) and isinstance(row.get("name"), str):
                position = _series_position(row.get("sequence"))
                if position is not None:
                    series.add((words(row["name"]), position))
    series_display = meta.get("seriesName")
    if not series and isinstance(series_display, str):
        for name, sequence in re.findall(r"(?:^|, )([^#]+) #(\d+(?:\.\d+)?)", series_display):
            series.add((words(name), float(sequence)))
    work_title = words(literary_title(work_title if work_title is not None else title))
    # Stripping "Book N" is safe only with corroborating literary position.
    numbered = re.search(r"\bBook\s+(\d+(?:\.\d+)?)\b", title, re.IGNORECASE)
    if numbered and not any(position == float(numbered[1]) for _, position in series):
        work_title = ""
    return LibraryRecording(
        raw_isbn,
        frozenset(words(n) for n in names if words(n)),
        publisher_name(publisher) if publisher else None,
        language(lang),
        production,
        int(part[1] or part[3]) if part else None,
        int(part[2]) if part and part[2] else None,
        valid,
        work_title,
        frozenset(words(name) for name in author_names if words(name)),
        frozenset(series),
    )


class EntryLike(Protocol):
    @property
    def asins(self) -> frozenset[str]: ...
    @property
    def isbns(self) -> frozenset[str]: ...
    @property
    def external_ids(self) -> frozenset[tuple[str, str]]: ...


def _owned(context: EditionOwnership, entry: EntryLike) -> bool:
    if any(asin(raw) is None for raw in entry.asins):
        return False
    observed_asins = {value for raw in entry.asins if (value := asin(raw))}
    # A supplied conflicting ASIN must never be rescued by an ISBN/title match.
    if observed_asins and context.asins and not observed_asins <= context.asins:
        return False
    external = {pair for pair in entry.external_ids if pair[0] == PROVIDER}
    if external and not external <= context.external_ids:
        return False
    details = getattr(entry, "_edition_recording", None)
    recording = details if isinstance(details, LibraryRecording) else None
    library_isbn = normalized_isbn(recording.raw_isbn or "") if recording else None
    if recording:
        if not recording.valid:
            return False
        for left, right in (
            (context.part, recording.part),
            (context.total, recording.total),
            (context.production, recording.production),
            (context.language, recording.language),
            (context.publisher, recording.publisher),
        ):
            if left is not None and right is not None and left != right:
                return False
        # A subset can be an incomplete cast tag; an unexpected known name is
        # conflicting evidence. ISBN-only proof below requires the full cast.
        if (
            context.narrators
            and recording.narrators
            and not recording.narrators <= context.narrators
        ):
            return False
    if library_isbn and context.recording_isbns and library_isbn not in context.recording_isbns:
        return False
    if external & context.external_ids or observed_asins & context.asins:
        return True
    if not recording or not library_isbn or library_isbn not in context.recording_isbns:
        return False
    # A novel's ISBN on another recording cannot qualify on ISBN alone. Missing
    # corroborators are unknown, rather than a reason to try fuzzy matching.
    return (
        bool(context.narrators)
        and context.narrators == recording.narrators
        and context.publisher is not None
        and context.publisher == recording.publisher
        and context.language is not None
        and context.language == recording.language
        and (context.part is None or context.part == recording.part)
        and (context.total is None or context.total == recording.total)
        and (context.production is None or context.production == recording.production)
    )


def match_edition_entries(book: BookMetadata, entries: Sequence[EntryLike]) -> str | None:
    if not isinstance(book, EditionBookMetadata):
        return None
    context = book._edition_ownership
    if context is None or (context.identity.provider, context.identity.provider_id) != (
        book.provider,
        book.provider_id,
    ):
        return None
    return "owned" if any(_owned(context, entry) for entry in entries) else None


def work_in_entries(book: BookMetadata, entries: Sequence[EntryLike]) -> bool:
    """UI only: literary evidence may identify a work, never a selected recording.

    Reuse exact ownership first. Otherwise require a verified shared work ID or
    identical literary title and full authors, with explicit series positions
    agreeing. A title equal to its series requires the position as well.
    """
    if match_edition_entries(book, entries):
        return True
    if not isinstance(book, EditionBookMetadata) or book._edition_ownership is None:
        return False
    context = book._edition_ownership
    if (book.provider, book.provider_id) != (
        context.identity.provider,
        context.identity.provider_id,
    ):
        return False
    title = words(book.edition_work_title or literary_title(book.title))
    authors = frozenset(words(name) for name in book.authors if words(name))
    series = words(book.edition_series_name or book.series_name or "")
    position = _series_position(book.series_position)
    # Only real namespaced work IDs qualify; synthesized grouping keys do not.
    work_id = book.edition_work_id or ""
    shared_id = ("audiobookdb", work_id.removeprefix("audiobookdb:"))
    for entry in entries:
        ids = {pair for pair in entry.external_ids if pair[0] == "audiobookdb"}
        if work_id.startswith("audiobookdb:") and ids:
            if shared_id in ids:
                return True
            continue
        recording = getattr(entry, "_edition_recording", None)
        if not isinstance(recording, LibraryRecording) or not recording.valid:
            continue
        if (
            not title
            or title != recording.work_title
            or not authors
            or authors != recording.authors
        ):
            continue
        if series and position is not None:
            if (series, position) not in recording.series:
                continue
        elif recording.series or (series and title == series):
            # An explicit position on only one side cannot establish which book.
            continue
        return True
    return False

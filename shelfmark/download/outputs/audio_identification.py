"""Read local audio headers/tags; never retag, decode audio, or query a service."""

import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from mutagen import File, MutagenError

from shelfmark.core.edition_ownership import _owned, asin, library_recording, ownership_evidence
from shelfmark.core.library_providers import LibraryEntry
from shelfmark.metadata_providers.editions.catalog_types import AggregatedEdition
from shelfmark.metadata_providers.editions.multisource import normalized_isbn
from shelfmark.metadata_providers.editions.presentation import work_presentation
from shelfmark.metadata_providers.editions.search_ranking import PART, literary_title, words

if TYPE_CHECKING:
    from pathlib import Path

    from shelfmark.core.edition_handoff import FrozenEditionContext
    from shelfmark.metadata_providers.editions.snapshot import EditionSnapshot


def same_work(left: EditionSnapshot, right: EditionSnapshot) -> bool:
    a, b = left.edition, right.edition
    return (
        words(work_presentation(a)[1]) == words(work_presentation(b)[1])
        and bool(a.authors)
        and {words(v) for v in a.authors} == {words(v) for v in b.authors}
        and (not a.series_name or not b.series_name or words(a.series_name) == words(b.series_name))
        and (
            a.series_position is None
            or b.series_position is None
            or a.series_position == b.series_position
        )
    )


@dataclass(frozen=True)
class Identification:
    outcome: str
    snapshot: EditionSnapshot | None
    observed: dict[str, Any]
    reason: str = ""


_FIELDS = {
    "talb": "album",
    "album": "album",
    "©alb": "album",
    "©wrk": "album",
    "tit2": "title",
    "title": "title",
    "©nam": "title",
    "author": "authors",
    "composer": "authors",
    "tcom": "authors",
    "©wrt": "authors",
    "narrator": "narrators",
    "narrators": "narrators",
    "publisher": "publisher",
    "tpub": "publisher",
    "asin": "asin",
    "isbn13": "isbn",
    "isbn10": "isbn",
    "audibleasin": "asin",
    "isbn": "isbn",
    "language": "language",
    "tlan": "language",
    "date": "date",
    "tdrc": "date",
    "tyer": "date",
    "©day": "date",
    "tracknumber": "track",
    "trck": "track",
    "trkn": "track",
    "discnumber": "disc",
    "tpos": "disc",
    "disk": "disc",
    "series": "series",
    "seriespart": "series_position",
}


def inspect_audio(files: list[Path]) -> dict[str, Any]:
    from shelfmark.core.utils import AUDIOBOOK_FORMATS

    values: dict[str, set[str]] = {}
    durations: list[float] = []
    chapters = 0
    for path in files:
        if path.suffix.lstrip(".").lower() not in AUDIOBOOK_FORMATS:
            continue
        try:
            audio = File(path)
            if audio is None:
                continue
            durations.append(float(getattr(audio.info, "length", 0)))
            chapters += len(getattr(audio, "chapters", None) or ())
            for raw_key, raw in (audio.tags or {}).items():
                key = str(raw_key).casefold()
                if key.startswith(("txxx:", "----:")):
                    key = key.rsplit(":", 1)[-1]
                    key = re.sub(r"[\s_-]+", "", key)
                field = _FIELDS.get(key)
                if not field:
                    if key.startswith("chap:"):
                        chapters += 1
                    continue
                content = getattr(raw, "text", raw)
                for value in content if isinstance(content, (list, tuple)) else [content]:
                    decoded = (
                        value.decode("utf-8", errors="strict")
                        if isinstance(value, bytes)
                        else value
                    )
                    if isinstance(decoded, (str, int, tuple)):
                        text = str(decoded).strip()
                        if text and len(text) <= 4096:
                            values.setdefault(field, set()).add(text)
        except MutagenError, OSError, ValueError, TypeError, AttributeError:
            # Unsupported/malformed tags are unknown evidence, not the wrong book.
            continue
    result: dict[str, Any] = {key: sorted(value) for key, value in values.items()}
    result.update(duration_seconds=sum(durations), audio_files=len(durations), chapters=chapters)
    return result


def identify(files: list[Path], context: FrozenEditionContext) -> Identification:
    from shelfmark.metadata_providers.editions.snapshot import EditionSnapshot

    observed = inspect_audio(files)
    requested = context.snapshot
    candidates = [EditionSnapshot.from_json(raw) for raw in context.candidates] or [requested]
    title = work_presentation(requested.edition)[1]
    titles = observed.get("album") or observed.get("title", [])
    # Per-track titles commonly name chapters. Only a common title is book evidence.
    if not observed.get("album") and (
        len(titles) != 1
        or re.fullmatch(r"(?:chapter|track|disc|part)?\s*\d+", titles[0], re.IGNORECASE)
    ):
        titles = []

    def title_key(value: str) -> str:
        normalized = literary_title(value)
        numbered = re.search(r"\bBook\s+(\d+(?:\.\d+)?)\b", value, re.IGNORECASE)
        if numbered and (
            not requested.edition.series_name or requested.edition.series_position is None
        ):
            normalized += f", Book {numbered[1]}"
        return words(normalized)

    if any(title_key(value) != words(title) for value in titles):
        return Identification(
            "conflict",
            None,
            observed,
            "Embedded book title contradicts the requested literary work; files retained for review",
        )
    positions = {
        float(match[1])
        for value in titles
        for match in re.finditer(r"\bbook\s+(\d+(?:\.\d+)?)\b", value, re.IGNORECASE)
    }
    positions.update(
        float(value)
        for value in observed.get("series_position", [])
        if re.fullmatch(r"\d+(?:\.\d+)?", value)
    )
    if (
        requested.edition.series_position is not None
        and any(position != requested.edition.series_position for position in positions)
    ) or (
        requested.edition.series_name
        and any(
            words(value) != words(requested.edition.series_name)
            for value in observed.get("series", [])
        )
    ):
        return Identification(
            "conflict",
            None,
            observed,
            "Embedded literary series or book position contradicts the requested work; files retained for review",
        )
    authors = observed.get("authors", [])
    if authors and {words(v) for v in authors} != {words(v) for v in requested.edition.authors}:
        return Identification(
            "conflict",
            None,
            observed,
            "Embedded author contradicts the requested literary work; files retained for review",
        )
    parts = {
        (int(p[1] or p[3]), int(p[2]) if p[2] else None)
        for value in titles
        for p in PART.finditer(value)
    }
    if len(parts) > 1 or (
        not context.work
        and requested.edition.part_number is not None
        and any(
            p != requested.edition.part_number
            or (t is not None and t != requested.edition.part_total)
            for p, t in parts
        )
    ):
        return Identification(
            "conflict",
            None,
            observed,
            "Embedded recording part conflicts with the requested part; files retained for review",
        )
    asins = observed.get("asin", [])
    if parts:
        observed["recording_part"] = next(iter(parts))
    isbns = observed.get("isbn", [])
    normalized_isbns = {normalized_isbn(value) for value in isbns}
    valid_ids = all(asin(v) for v in asins) and all(normalized_isbn(v) for v in isbns)
    matches: list[EditionSnapshot] = []
    for candidate in candidates:
        if not same_work(requested, candidate):
            continue
        from shelfmark.core.edition_handoff import _validate_selected_recording

        try:
            _validate_selected_recording(candidate)
        except ValueError:
            continue
        meta = {"title": titles[0] if titles else "", "narrators": observed.get("narrators", [])}
        if len(normalized_isbns) == 1 and None not in normalized_isbns:
            meta["isbn"] = next(iter(normalized_isbns))
        for key in ("publisher", "language"):
            raw = observed.get(key, [])
            if len(raw) == 1:
                meta[key] = raw[0]
        if len(normalized_isbns) > 1 or any(
            len(observed.get(key, [])) > 1 for key in ("publisher", "language")
        ):
            continue
        entry = LibraryEntry(
            frozenset(),
            frozenset(),
            frozenset(v.upper() for v in asins),
            _edition_recording=library_recording({"media": {"metadata": meta}}),
        )
        evidence = ownership_evidence(candidate.edition)
        # Duration can veto a sample/wrong-length file, but never identifies a recording.
        if (
            isinstance(candidate.edition, AggregatedEdition)
            and observed["audio_files"] == len(files)
            and observed["duration_seconds"] > 0
        ):
            durations = [
                json.loads(value.value_json)
                for value in candidate.edition.provenance.values
                if value.field == "source.lengthMinutes"
                and value.source == candidate.edition.provenance.primary
            ]
            if (
                durations
                and isinstance(durations[0], (int, float))
                and durations[0] > 0
                and not 0.8 <= observed["duration_seconds"] / (durations[0] * 60) <= 1.2
            ):
                continue
        corroborated = bool(
            entry._edition_recording
            and (
                (evidence.narrators and evidence.narrators == entry._edition_recording.narrators)
                or (
                    evidence.publisher
                    and evidence.publisher == entry._edition_recording.publisher
                    and evidence.language
                    and evidence.language == entry._edition_recording.language
                )
            )
        )
        if valid_ids and (asins or isbns) and corroborated and _owned(evidence, entry):
            matches.append(candidate)
    if len(matches) == 1:
        return Identification("exact", matches[0], observed)
    selected = ownership_evidence(requested.edition)
    contradicting_cast = (
        selected.narrators
        and observed.get("narrators")
        and not {words(value) for value in observed["narrators"]} <= selected.narrators
    )
    if not context.work and (asins or isbns or contradicting_cast):
        return Identification(
            "conflict",
            None,
            observed,
            "Downloaded recording identifiers do not confirm the selected edition. Use Get book to import this release without a recording claim; files retained",
        )
    return Identification("work", None, observed)

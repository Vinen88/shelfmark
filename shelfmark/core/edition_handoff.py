"""Trusted edition selection for the native queue; no jobs, routes or output I/O."""

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, cast

from shelfmark.metadata_providers.editions.catalog_types import AggregatedEdition, SourceError
from shelfmark.metadata_providers.editions.identity import EditionIdentity
from shelfmark.metadata_providers.editions.multisource import (
    decode_identity,
    encode_identity,
    normalized_isbn,
)
from shelfmark.metadata_providers.editions.search_ranking import ADAPTATION, PART, language
from shelfmark.metadata_providers.editions.snapshot import EditionSnapshot

if TYPE_CHECKING:
    from shelfmark.metadata_providers import BookMetadata

PROVIDER = "edition_metadata"
MAX_CONTEXT_BYTES = 256 * 1024
MAX_IDENTIFIER_LENGTH = 4096
Resolver = Callable[[EditionIdentity], EditionSnapshot | None]


def work_metadata(book: BookMetadata) -> BookMetadata:
    """Native release-search inputs without a selected recording or ISBN."""
    from dataclasses import replace

    from shelfmark.metadata_providers.editions.search_ranking import literary_title, words

    title = getattr(book, "edition_work_title", None) or literary_title(book.title)
    search = title
    if words(title) == words(book.series_name or "") and book.series_position is not None:
        search += f" Book {book.series_position:g}"
    return replace(
        book,
        title=title,
        search_title=search,
        isbn_10=None,
        isbn_13=None,
        publisher=None,
        publish_year=None,
        subtitle=None,
        display_fields=[],
    )


INFORMATIONAL_WARNINGS = frozenset(
    {
        "Historical delisting report; current availability unverified",
        "Unsupported nonnumeric series position",
    }
)
# Only diagnostics emitted by bounded discovery/enrichment are nonblocking.
# Source-record warnings (including incomplete selected responses) still fail closed.
DISCOVERY_WARNING = re.compile(
    r"(?:[a-z0-9_]+(?: page [1-3])?: (?:timeout|[A-Za-z]+Error|RateLimited|"
    r"skipped \(deadline or request cap\)|repeated page identifiers; pagination incomplete|"
    r"discovery window reached; page [1-3] saturated, additional pages not read)|"
    r"Enrichment skipped: (?:request deadline reached|search request cap reached)|"
    r"Enrichment incomplete: using verified search records|"
    r"Language policy excluded \d+ source records \(including unknown if disabled\)|"
    r"Aggregate discovery window reached \(\d+ of \d+ observed groups\))"
)


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError("Edition context must be an object")
    return cast("dict[str, object]", value)


def _text(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or value != value.strip()
        or len(value) > MAX_IDENTIFIER_LENGTH
    ):
        raise ValueError(f"Invalid edition context {field}")
    return value


def _encode(value: object, limit: int = MAX_CONTEXT_BYTES) -> str:
    text = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    if len(text.encode("utf-8")) > limit:
        raise ValueError("Edition context exceeds 256 KiB; metadata was not truncated")
    return text


@dataclass(frozen=True, slots=True)
class EditionLocator:
    identity: EditionIdentity

    def __post_init__(self) -> None:
        _text(self.identity.provider, "provider")
        _text(self.identity.provider_id, "provider_id")

    def to_dict(self) -> dict[str, object]:
        return {
            "version": 1,
            "provider": self.identity.provider,
            "provider_id": self.identity.provider_id,
        }

    @classmethod
    def from_dict(cls, value: object) -> EditionLocator:
        data = _object(value)
        if (
            set(data) != {"version", "provider", "provider_id"}
            or type(data["version"]) is not int
            or data["version"] != 1
        ):
            raise ValueError("Invalid edition locator fields or version")
        return cls(
            EditionIdentity(
                _text(data["provider"], "provider"), _text(data["provider_id"], "provider_id")
            )
        )


@dataclass(frozen=True, slots=True)
class FrozenEditionContext:
    """Snapshot JSON is immutable; release identity stays in separate fields."""

    snapshot_json: str
    source: str
    source_id: str
    work: bool = False
    candidates: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _text(self.source, "source")
        _text(self.source_id, "source_id")
        if len(self.snapshot_json.encode("utf-8")) > MAX_CONTEXT_BYTES:
            raise ValueError("Edition snapshot exceeds 256 KiB")
        snapshot = EditionSnapshot.from_json(self.snapshot_json)
        if (
            snapshot.identity.provider != PROVIDER
            or snapshot.identity.provider_id != snapshot.edition.provider_id
        ):
            raise ValueError("Snapshot edition identity is inconsistent")
        # Canonicalize only through the lossless existing serializer.
        object.__setattr__(self, "snapshot_json", snapshot.to_json())
        for candidate in self.candidates:
            EditionSnapshot.from_json(candidate)
        self.to_json()

    @property
    def snapshot(self) -> EditionSnapshot:
        return EditionSnapshot.from_json(self.snapshot_json)

    def to_json(self) -> str:
        snapshot = self.snapshot
        data: dict[str, object] = {
            "version": 1,
            "provider": snapshot.identity.provider,
            "provider_id": snapshot.identity.provider_id,
            "release": {"source": self.source, "source_id": self.source_id},
            "snapshot": snapshot.to_dict(),
        }
        if self.work:
            data.update(
                version=2, intent="work", candidates=[json.loads(raw) for raw in self.candidates]
            )
        return _encode(data, 4 * 1024 * 1024 if self.work else MAX_CONTEXT_BYTES)

    def validate_release(self, source: str, source_id: str) -> None:
        if (source, source_id) != (self.source, self.source_id):
            raise ValueError("Edition context belongs to a different release source/ID")

    @classmethod
    def from_json(cls, text: str) -> FrozenEditionContext:
        if len(text.encode("utf-8")) > 4 * 1024 * 1024:
            raise ValueError("Edition context exceeds 256 KiB")

        def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
            data: dict[str, object] = {}
            for key, value in pairs:
                if key in data:
                    raise ValueError("Duplicate edition context field: " + key)
                data[key] = value
            return data

        data = _object(json.loads(text, object_pairs_hook=unique))
        work = data.get("version") == 2 and data.get("intent") == "work"
        fields = {"version", "provider", "provider_id", "release", "snapshot"}
        if work:
            fields |= {"intent", "candidates"}
        if (
            set(data) != fields
            or type(data["version"]) is not int
            or (not work and data["version"] != 1)
        ):
            raise ValueError("Invalid frozen edition context fields or version")
        release = _object(data["release"])
        if set(release) != {"source", "source_id"}:
            raise ValueError("Invalid release binding")
        snapshot = EditionSnapshot.from_dict(data["snapshot"])
        if snapshot.identity != EditionIdentity(
            _text(data["provider"], "provider"), _text(data["provider_id"], "provider_id")
        ):
            raise ValueError("Edition context and snapshot identities differ")
        candidates = data.get("candidates", [])
        if not isinstance(candidates, list) or len(candidates) > 100:
            raise ValueError("Invalid work candidates")
        return cls(
            snapshot.to_json(),
            _text(release["source"], "source"),
            _text(release["source_id"], "source_id"),
            work,
            tuple(EditionSnapshot.from_dict(raw).to_json() for raw in candidates),
        )


class ContextTask(Protocol):
    source: str
    task_id: str
    output_args: dict[str, object]


def context_from_task(task: ContextTask) -> FrozenEditionContext | None:
    """Strict read helper for restored native tasks; no retry interception."""
    if "edition_context" not in task.output_args:
        return None
    value = task.output_args["edition_context"]
    if not isinstance(value, str):
        raise ValueError("Stored edition context must be JSON text")  # noqa: TRY004 -- Serialized metadata uses ValueError for all invalid fields.
    result = FrozenEditionContext.from_json(value)
    result.validate_release(task.source, task.task_id)
    return result


def bind_request_locator(book_data: object, release_data: object) -> dict[str, object]:
    """Called by the two pinned native paths AFTER their access/policy checks."""
    book = _object(book_data)
    release = _object(release_data)
    if book.get("provider") != PROVIDER:
        if "work_context" in release:
            raise ValueError("Work download conflicts with the requested metadata provider")
        raw = release.get("edition_context")
        if isinstance(raw, dict) and raw.get("provider") == PROVIDER:
            raise ValueError("Release edition conflicts with the requested metadata provider")
        return release
    if book.get("download_intent") == "work":
        if "edition_context" in release:
            raise ValueError("An exact edition conflicts with a Get book request")
        if "work_context" in release and _object(release["work_context"]).get(
            "provider_id"
        ) != book.get("provider_id"):
            raise ValueError("Release work conflicts with the stored request")
        return {
            **release,
            "work_context": {
                "provider_id": book.get("provider_id"),
                "candidate_ids": book.get("edition_candidate_ids", []),
            },
        }
    if "work_context" in release:
        raise ValueError("A work download conflicts with an exact edition request")
    incoming = (
        EditionLocator.from_dict(release["edition_context"])
        if "edition_context" in release
        else None
    )
    expected = EditionLocator.from_dict(
        {"version": 1, "provider": PROVIDER, "provider_id": book.get("provider_id")}
    )
    if incoming is not None and incoming != expected:
        raise ValueError("Release edition conflicts with the stored request edition")
    return {**release, "edition_context": expected.to_dict()}


def trusted_snapshot(identity: EditionIdentity) -> EditionSnapshot | None:
    """Use the selected registered provider; never change its active settings."""
    from shelfmark.metadata_providers import get_provider, get_provider_kwargs
    from shelfmark.metadata_providers.editions.provider import EditionMetadataProvider

    provider = get_provider(identity.provider, **get_provider_kwargs(identity.provider))
    if not isinstance(provider, EditionMetadataProvider) or not provider.is_available():
        raise SourceError("Selected edition provider is unavailable")
    return provider.get_snapshot(identity.provider_id)


def _check_advisory_titles(release: dict[str, object], snapshot: EditionSnapshot) -> None:
    edition = snapshot.edition
    for field in ("title", "release_title"):
        text = release.get(field)
        if text is None:
            continue
        if not isinstance(text, str) or len(text) > MAX_IDENTIFIER_LENGTH:
            raise ValueError("Invalid advisory release title")
        for part in PART.finditer(text):
            number, total = int(part[1] or part[3]), int(part[2]) if part[2] else None
            if (edition.part_number is not None and number != edition.part_number) or (
                edition.part_total is not None and total is not None and total != edition.part_total
            ):
                raise ValueError(
                    "Release title explicitly conflicts with the selected edition part"
                )
        conventional = re.search(r"\b(?:conventional|single[- ]narrator)\b", text, re.IGNORECASE)
        selected = " ".join((edition.edition_label or "", snapshot.edition_title))
        if conventional and ADAPTATION.search(selected):
            raise ValueError("Release title conflicts with the selected dramatized recording")
        if ADAPTATION.search(text) and re.search(
            r"\b(?:conventional|single[- ]narrator)\b", selected, re.IGNORECASE
        ):
            raise ValueError("Release title conflicts with the selected conventional recording")
    selected_language = language(edition.language)
    release_language = release.get("language")
    if release_language is not None and not isinstance(release_language, str):
        raise ValueError("Invalid release language")
    known = language(release_language)
    unknown = {None, "unknown", "und", "unk", "n/a", "na", "none", "null", "unspecified"}
    if selected_language not in unknown and known not in unknown and selected_language != known:
        raise ValueError("Release language conflicts with the selected edition")


def _validate_selected_recording(snapshot: EditionSnapshot) -> None:
    edition = snapshot.edition
    if not isinstance(edition, AggregatedEdition):
        return  # Existing single-source backend contract is unchanged.
    provenance = edition.provenance
    if (
        edition.catalog != "aggregate"
        or decode_identity(edition.edition_id).key != provenance.primary.key
        or encode_identity(provenance.primary) != edition.edition_id
    ):
        raise ValueError(
            "Selected aggregate identity does not match its primary recording; select it again"
        )
    primary = {
        entry.field: json.loads(entry.value_json)
        for entry in provenance.values
        if entry.source == provenance.primary
    }
    if (
        not edition.title.strip()
        or not edition.authors
        or any(not name.strip() for name in edition.authors)
        or primary.get("title") != edition.title
        or primary.get("authors") != list(edition.authors)
    ):
        raise ValueError(
            "Selected recording needs trusted title and author evidence; refresh its detail"
        )
    if edition.asin is not None and (
        not re.fullmatch(r"[A-Z0-9]{10}", edition.asin)
        or primary.get("identity.asin_verified") is not True
        or primary.get("asin") != edition.asin
        or (
            provenance.primary.source.startswith("libex_")
            and provenance.primary.record_id != edition.asin
        )
    ):
        raise ValueError(
            "Selected recording ASIN is invalid, unverified or conflicts with its primary identity"
        )
    raw_isbns = tuple(value for value in (edition.isbn_10, edition.isbn_13) if value is not None)
    isbns = {normalized_isbn(value) for value in raw_isbns}
    if None in isbns or len(isbns) > 1:
        raise ValueError("Selected recording ISBN is invalid or its ISBN-10/ISBN-13 conflict")
    attested: set[str] = set()
    for entry in provenance.values:
        if entry.field == "identity.recording_isbns":
            evidence: object = json.loads(entry.value_json)
            if not isinstance(evidence, list) or any(not isinstance(v, str) for v in evidence):
                raise ValueError("Selected recording ISBN evidence is malformed")
            for value in evidence:
                if not isinstance(value, str) or (isbn := normalized_isbn(value)) is None:
                    raise ValueError("Selected recording ISBN evidence is invalid")
                attested.add(isbn)
    if (attested and not isbns <= attested) or len(attested) > 1:
        raise ValueError("Selected recording ISBN conflicts with verified recording-scope evidence")
    if not edition.asin and not (isbns & attested):
        raise ValueError(
            "Selected recording needs a verified ASIN or recording ISBN before queueing"
        )
    for field in ("part_number", "part_total"):
        expected = getattr(edition, field)
        if any(
            json.loads(entry.value_json) != expected
            for entry in provenance.values
            if entry.field == field
        ):
            raise ValueError("Selected recording part conflicts with trusted source evidence")
    selected = " ".join((edition.title, edition.edition_label or ""))
    for part in PART.finditer(selected):
        if edition.part_number != int(part[1] or part[3]) or (
            part[2] and edition.part_total != int(part[2])
        ):
            raise ValueError("Selected recording title requires matching part number and total")
    productions = {
        json.loads(entry.value_json)
        for entry in provenance.values
        if entry.field == "recording.production" and json.loads(entry.value_json) is not None
    }
    if len(productions) > 1 or (ADAPTATION.search(selected) and productions != {"dramatized"}):
        raise ValueError(
            "Selected recording production labels conflict or lack dramatized recording evidence"
        )
    if "dramatized" in productions and re.search(
        r"\b(?:conventional|single[- ]narrator)\b", selected, re.IGNORECASE
    ):
        raise ValueError("Selected recording production labels contradict its conventional title")
    unresolved = tuple(
        warning
        for warning in provenance.warnings
        if warning not in INFORMATIONAL_WARNINGS and not DISCOVERY_WARNING.fullmatch(warning)
    )
    if unresolved:
        raise ValueError(
            "Selected recording has unresolved source warnings; refresh/review detail: "
            + "; ".join(unresolved)
        )


def resolve_release(
    release_data: dict[str, object], resolver: Resolver = trusted_snapshot
) -> tuple[dict[str, object], FrozenEditionContext | None]:
    if "work_context" in release_data:
        if "edition_context" in release_data or release_data.get("content_type") != "audiobook":
            raise ValueError("Choose either Get book or Get this edition for an audiobook")
        raw_work = _object(release_data["work_context"])
        if set(raw_work) != {"provider_id", "candidate_ids"}:
            raise ValueError("Invalid work context")
        identity = EditionIdentity(PROVIDER, _text(raw_work["provider_id"], "provider_id"))
        snapshot = resolver(identity)
        if snapshot is None or snapshot.identity != identity:
            raise ValueError("Requested work metadata is unavailable")
        if isinstance(snapshot.edition, AggregatedEdition) and set(
            snapshot.edition.provenance.conflicts
        ) & {"authors", "series_name", "series_position"}:
            raise ValueError(
                "Catalog author/series evidence conflicts; review the work or select an unambiguous edition"
            )
        ids = raw_work["candidate_ids"]
        if (
            not isinstance(ids, list)
            or len(ids) > 100
            or any(not isinstance(value, str) for value in ids)
        ):
            raise ValueError("Invalid work candidates")
        from shelfmark.download.outputs.audio_identification import same_work
        from shelfmark.metadata_providers import get_provider, get_provider_kwargs
        from shelfmark.metadata_providers.editions.provider import EditionMetadataProvider

        provider = get_provider(PROVIDER, **get_provider_kwargs(PROVIDER))
        candidates = [snapshot]
        if isinstance(provider, EditionMetadataProvider):
            for book_id in dict.fromkeys(ids):
                candidate = provider.cached_snapshot(book_id)
                if candidate and candidate.identity != identity:
                    if not all(same_work(candidate, peer) for peer in candidates):
                        raise ValueError("Catalog candidates contain conflicting literary works")
                    candidates.append(candidate)
        context = FrozenEditionContext(
            snapshot.to_json(),
            _text(release_data.get("source"), "source"),
            _text(release_data.get("source_id"), "source_id"),
            True,
            tuple(c.to_json() for c in candidates),
        )
        from shelfmark.metadata_providers.editions.presentation import work_presentation

        _, title, _ = work_presentation(snapshot.edition)
        return {
            **release_data,
            "title": title,
            "author": ", ".join(snapshot.edition.authors),
            "series_name": snapshot.edition.series_name,
            "series_position": snapshot.edition.series_position,
            "year": None,
            "subtitle": None,
            "narrator": None,
        }, context
    if "edition_context" not in release_data:
        return release_data, None
    raw = release_data["edition_context"]
    other_provider = raw.get("provider") if isinstance(raw, dict) else None
    if (
        isinstance(other_provider, str)
        and other_provider.strip()
        and other_provider.strip() != PROVIDER
    ):
        # Another provider's optional data has no meaning to this adapter.
        return release_data, None
    locator = EditionLocator.from_dict(raw)
    if locator.identity.provider != PROVIDER:
        return release_data, None
    # Source binding must not change while the trusted catalog lookup yields.
    release_data = dict(release_data)
    if release_data.get("content_type") != "audiobook":
        raise ValueError("Edition handoff requires audiobook content_type")
    source = _text(release_data.get("source"), "source")
    source_id = _text(release_data.get("source_id"), "source_id")
    snapshot = resolver(locator.identity)
    if snapshot is None or snapshot.identity != locator.identity:
        raise ValueError("Trusted selected edition was not found or has a different identity")
    _validate_selected_recording(snapshot)
    context = FrozenEditionContext(snapshot.to_json(), source, source_id)
    _check_advisory_titles(release_data, context.snapshot)
    # Part-specific task title, without replacing source identity or release fields.
    return {**release_data, "title": context.snapshot.edition_title}, context

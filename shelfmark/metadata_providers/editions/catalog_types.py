"""Small source contract and lossless aggregation provenance; no application hooks."""

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, fields
from enum import StrEnum
from typing import cast

from shelfmark.metadata_providers.editions.models import AudiobookEdition


class Availability(StrEnum):
    LIVE = "returned_by_audible"
    HISTORICAL = "historical_database"
    DELISTED = "known_delisted"
    UNKNOWN = "unknown"


class SourceError(RuntimeError):
    pass


class RateLimited(SourceError):  # noqa: N818 -- Retain the existing catalog exception contract.
    def __init__(self, retry_after: float) -> None:
        super().__init__(f"Source rate limited; retry after {retry_after:g}s")
        self.retry_after = retry_after


@dataclass(frozen=True, slots=True)
class SourceRef:
    source: str
    record_id: str
    marketplace: str | None = None
    availability: Availability = Availability.UNKNOWN

    def __post_init__(self) -> None:
        if not self.source.strip() or not self.record_id.strip():
            raise ValueError("Source and record ID must be nonblank")

    @property
    def key(self) -> tuple[str, str, str]:
        return self.source, self.marketplace or "", self.record_id


@dataclass(frozen=True, slots=True)
class FieldValue:
    field: str
    source: SourceRef
    # Canonical JSON retains strings, numbers, dates and tuple/list values distinctly.
    value_json: str


@dataclass(frozen=True, slots=True)
class Provenance:
    primary: SourceRef
    references: tuple[SourceRef, ...]
    values: tuple[FieldValue, ...]
    conflicts: tuple[str, ...]
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        def reference(ref: SourceRef) -> dict[str, object]:
            return {
                "source": ref.source,
                "record_id": ref.record_id,
                "marketplace": ref.marketplace,
                "availability": ref.availability.value,
            }

        return {
            "primary": reference(self.primary),
            "references": [reference(ref) for ref in self.references],
            "values": [
                {"field": val.field, "source": reference(val.source), "value_json": val.value_json}
                for val in self.values
            ],
            "conflicts": list(self.conflicts),
            "warnings": list(self.warnings),
        }

    @classmethod
    def from_dict(cls, value: object) -> Provenance:
        def obj(raw: object, keys: set[str]) -> dict[str, object]:
            if not isinstance(raw, dict) or set(raw) != keys:
                raise ValueError("Invalid provenance object")
            return cast("dict[str, object]", raw)

        def text(raw: object) -> str:
            if not isinstance(raw, str):
                raise ValueError("Invalid provenance string")  # noqa: TRY004 -- Serialized metadata uses ValueError for all invalid fields.
            return raw

        def items(raw: object) -> list[object]:
            if not isinstance(raw, list) or len(raw) > 2048:
                raise ValueError("Invalid provenance list")
            return cast("list[object]", raw)

        def reference(raw: object) -> SourceRef:
            data = obj(raw, {"source", "record_id", "marketplace", "availability"})
            market = data["marketplace"]
            return SourceRef(
                text(data["source"]),
                text(data["record_id"]),
                None if market is None else text(market),
                Availability(text(data["availability"])),
            )

        data = obj(value, {"primary", "references", "values", "conflicts", "warnings"})
        values: list[FieldValue] = []
        for raw in items(data["values"]):
            entry = obj(raw, {"field", "source", "value_json"})
            encoded = text(entry["value_json"])
            decoded: object = json.loads(encoded)  # Validate the stored JSON, never evaluate it.
            if (
                json.dumps(
                    decoded,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                )
                != encoded
            ):
                raise ValueError("Noncanonical provenance JSON")
            values.append(FieldValue(text(entry["field"]), reference(entry["source"]), encoded))
        result = cls(
            reference(data["primary"]),
            tuple(reference(ref) for ref in items(data["references"])),
            tuple(values),
            tuple(text(v) for v in items(data["conflicts"])),
            tuple(text(v) for v in items(data["warnings"])),
        )
        if result.primary not in result.references or len(
            {ref.key for ref in result.references}
        ) != len(result.references):
            raise ValueError("Missing primary or duplicate source references")
        if any(val.source not in result.references for val in result.values):
            raise ValueError("Field provenance references an unknown source")
        return result


@dataclass(frozen=True, slots=True, kw_only=True)
class AggregatedEdition(AudiobookEdition):
    provenance: Provenance

    @classmethod
    def create(cls, edition: AudiobookEdition, provenance: Provenance) -> AggregatedEdition:
        # Explicit base fields keep future model changes visible to strict checking.
        return cls(
            catalog=edition.catalog,
            edition_id=edition.edition_id,
            title=edition.title,
            authors=edition.authors,
            subtitle=edition.subtitle,
            isbn_10=edition.isbn_10,
            isbn_13=edition.isbn_13,
            asin=edition.asin,
            cover_url=edition.cover_url,
            description=edition.description,
            publisher=edition.publisher,
            publish_year=edition.publish_year,
            language=edition.language,
            narrators=edition.narrators,
            series_name=edition.series_name,
            series_position=edition.series_position,
            source_url=edition.source_url,
            part_number=edition.part_number,
            part_total=edition.part_total,
            parent_work_id=edition.parent_work_id,
            search_title=edition.search_title,
            release_date=edition.release_date,
            canonical_title=edition.canonical_title,
            edition_label=edition.edition_label,
            provenance=provenance,
        )


@dataclass(frozen=True, slots=True)
class RegionalEvidence:
    rule: str
    group_id: str
    recording_isbn: str | None
    duration_minutes: int


@dataclass(frozen=True, slots=True)
class SourceRecord:
    reference: SourceRef
    edition: AudiobookEdition
    asin_verified: bool = False
    recording_isbns: tuple[str, ...] = ()  # Explicit attestation; raw ISBN alone is insufficient.
    links: tuple[SourceRef, ...] = ()
    production: str | None = (
        None  # e.g. dramatized/conventional; unabridged is not a production type.
    )
    warnings: tuple[str, ...] = ()
    facts: tuple[tuple[str, str], ...] = ()  # Source-specific supported facts, as canonical JSON.
    regional_evidence: RegionalEvidence | None = None


class CatalogSource(ABC):
    """Sources must honour timeout; lookup returns ONLY exact-identifier candidates.

    key uniquely identifies one family/marketplace adapter. No title fallback is
    permitted in lookup. Search is a bounded discovery window, not a global index.
    """

    key: str
    family: str

    @property
    def marketplace(self) -> str | None:
        """Optional catalog market; consumers only read this value."""
        return None

    searchable: bool = True
    unique_recording_ids: bool = (
        False  # True only if one recording cannot have aliases within this adapter.
    )
    supports_paging: bool = False
    page_limit: int = 100
    discovery_cost: int = 1  # Maximum HTTP calls inside one discovery operation.
    lookup_cost: int = 1

    @abstractmethod
    def search(
        self, query: str, search_type: str, limit: int, timeout: float
    ) -> tuple[SourceRecord, ...]: ...

    def search_page(
        self, query: str, search_type: str, limit: int, timeout: float, *, page: int = 1
    ) -> tuple[SourceRecord, ...]:
        """One-based extension pages; old sources remain first-page compatible."""
        if page != 1:
            raise SourceError("Source does not support additional discovery pages")
        return self.search(query, search_type, limit, timeout)

    @abstractmethod
    def get(self, record_id: str, timeout: float) -> SourceRecord | None: ...

    @abstractmethod
    def lookup(self, selected: SourceRecord, timeout: float) -> tuple[SourceRecord, ...]: ...


def metadata_values(edition: AudiobookEdition) -> dict[str, object]:
    return {
        field.name: getattr(edition, field.name)
        for field in fields(AudiobookEdition)
        if field.name not in {"catalog", "edition_id"}
    }

"""Lossless snapshots: legacy schema 1, aggregation provenance in schema 2."""

import json
import math
from dataclasses import dataclass
from datetime import date
from typing import Literal, NotRequired, TypedDict, cast

from shelfmark.metadata_providers.editions.catalog_types import AggregatedEdition, Provenance
from shelfmark.metadata_providers.editions.identity import EditionIdentity
from shelfmark.metadata_providers.editions.models import AudiobookEdition
from shelfmark.metadata_providers.editions.presentation import (
    canonical_title,
    display_title,
    search_query,
)


class EditionData(TypedDict):
    catalog: str
    edition_id: str
    title: str
    authors: list[str]
    subtitle: str | None
    isbn_10: str | None
    isbn_13: str | None
    asin: str | None
    cover_url: str | None
    description: str | None
    publisher: str | None
    publish_year: int | None
    language: str | None
    narrators: list[str]
    series_name: str | None
    series_position: float | None
    source_url: str | None
    part_number: int | None
    part_total: int | None
    parent_work_id: str | None
    search_title: str | None
    release_date: str | None
    canonical_title: str | None
    edition_label: str | None


class SnapshotData(TypedDict):
    schema_version: Literal[1, 2]
    provider: str
    provider_id: str
    canonical_title: str
    edition_title: str
    search_query: str
    edition: EditionData
    aggregation: NotRequired[dict[str, object]]


def _object(value: object, keys: frozenset[str]) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError("Snapshot object has missing or unknown fields")
    # Key equality above establishes the string-keyed shape; values are checked below.
    return cast("dict[str, object]", value)


def _text(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("Expected a string")  # noqa: TRY004 -- Serialized metadata uses ValueError for all invalid fields.
    return value


def _optional_text(value: object) -> str | None:
    return None if value is None else _text(value)


def _integer(value: object) -> int | None:
    if value is None:
        return None
    if type(value) is not int:
        raise ValueError("Expected an integer or null")
    return value


def _position(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("Expected a finite series position or null")
    return value


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError("Expected a list of strings")  # noqa: TRY004 -- Serialized metadata uses ValueError for all invalid fields.
    return tuple(_text(item) for item in value)


def _date(value: object) -> date | None:
    if value is None:
        return None
    text = _text(value)
    result = date.fromisoformat(text)
    if result.isoformat() != text:
        raise ValueError("Expected YYYY-MM-DD release date")
    return result


@dataclass(frozen=True, slots=True)
class EditionSnapshot:
    identity: EditionIdentity
    edition: AudiobookEdition

    @property
    def canonical_title(self) -> str:
        return canonical_title(self.edition)

    @property
    def edition_title(self) -> str:
        return display_title(self.edition)

    def to_dict(self) -> SnapshotData:
        e = self.edition
        data: EditionData = {
            "catalog": e.catalog,
            "edition_id": e.edition_id,
            "title": e.title,
            "authors": list(e.authors),
            "subtitle": e.subtitle,
            "isbn_10": e.isbn_10,
            "isbn_13": e.isbn_13,
            "asin": e.asin,
            "cover_url": e.cover_url,
            "description": e.description,
            "publisher": e.publisher,
            "publish_year": e.publish_year,
            "language": e.language,
            "narrators": list(e.narrators),
            "series_name": e.series_name,
            "series_position": e.series_position,
            "source_url": e.source_url,
            "part_number": e.part_number,
            "part_total": e.part_total,
            "parent_work_id": e.parent_work_id,
            "search_title": e.search_title,
            "release_date": e.release_date.isoformat() if e.release_date else None,
            "canonical_title": e.canonical_title,
            "edition_label": e.edition_label,
        }
        result: SnapshotData = {
            "schema_version": 1,
            "provider": self.identity.provider,
            "provider_id": self.identity.provider_id,
            "canonical_title": self.canonical_title,
            "edition_title": self.edition_title,
            "search_query": search_query(e),
            "edition": data,
        }
        if isinstance(e, AggregatedEdition):
            result["schema_version"] = 2
            result["aggregation"] = e.provenance.to_dict()
        return result

    def to_json(self) -> str:
        return json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )

    @classmethod
    def from_dict(cls, value: object) -> EditionSnapshot:
        if not isinstance(value, dict):
            raise ValueError("Expected snapshot object")  # noqa: TRY004 -- Serialized metadata uses ValueError for all invalid fields.
        version = value.get("schema_version")
        if type(version) is not int or version not in (1, 2):
            raise ValueError("Unsupported edition snapshot schema_version")
        keys = frozenset(SnapshotData.__annotations__)
        root = _object(value, keys if version == 2 else keys - {"aggregation"})
        data = _object(root["edition"], frozenset(EditionData.__annotations__))
        edition = AudiobookEdition(
            catalog=_text(data["catalog"]),
            edition_id=_text(data["edition_id"]),
            title=_text(data["title"]),
            authors=_strings(data["authors"]),
            subtitle=_optional_text(data["subtitle"]),
            isbn_10=_optional_text(data["isbn_10"]),
            isbn_13=_optional_text(data["isbn_13"]),
            asin=_optional_text(data["asin"]),
            cover_url=_optional_text(data["cover_url"]),
            description=_optional_text(data["description"]),
            publisher=_optional_text(data["publisher"]),
            publish_year=_integer(data["publish_year"]),
            language=_optional_text(data["language"]),
            narrators=_strings(data["narrators"]),
            series_name=_optional_text(data["series_name"]),
            series_position=_position(data["series_position"]),
            source_url=_optional_text(data["source_url"]),
            part_number=_integer(data["part_number"]),
            part_total=_integer(data["part_total"]),
            parent_work_id=_optional_text(data["parent_work_id"]),
            search_title=_optional_text(data["search_title"]),
            release_date=_date(data["release_date"]),
            canonical_title=_optional_text(data["canonical_title"]),
            edition_label=_optional_text(data["edition_label"]),
        )
        if version == 2:
            provenance = Provenance.from_dict(root["aggregation"])
            from shelfmark.metadata_providers.editions.multisource import decode_identity

            if (
                edition.catalog != "aggregate"
                or decode_identity(edition.edition_id).key != provenance.primary.key
                or root["provider_id"] != edition.provider_id
            ):
                raise ValueError("Aggregation identity does not match its primary source")
            edition = AggregatedEdition.create(edition, provenance)
        result = cls(EditionIdentity(_text(root["provider"]), _text(root["provider_id"])), edition)
        # Reject contradictory redundant fields rather than silently discard them.
        if (
            root["canonical_title"] != result.canonical_title
            or root["edition_title"] != result.edition_title
            or root["search_query"] != search_query(edition)
        ):
            raise ValueError("Snapshot titles/query do not match its edition")
        return result

    @classmethod
    def from_json(cls, payload: str) -> EditionSnapshot:
        def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
            result: dict[str, object] = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError(f"Duplicate snapshot field: {key}")
                result[key] = value
            return result

        value: object = json.loads(payload, object_pairs_hook=unique_object)
        return cls.from_dict(value)


def validate_unique_identities(snapshots: tuple[EditionSnapshot, ...]) -> None:
    """Never silently overwrite two records carrying the same primary identity."""
    seen: set[EditionIdentity] = set()
    for snapshot in snapshots:
        if snapshot.identity in seen:
            raise ValueError(f"Duplicate edition identity: {snapshot.identity.key}")
        seen.add(snapshot.identity)

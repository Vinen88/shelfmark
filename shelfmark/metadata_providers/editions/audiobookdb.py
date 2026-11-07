"""Opt-in AudiobookDB 2.0 recording adapter; a Book is never a recording ID."""

import json
import math
import re
from dataclasses import replace
from time import monotonic
from typing import TYPE_CHECKING
from urllib.parse import quote

from shelfmark.metadata_providers.editions.catalog_fields import (
    isbn_fields,
    names,
    number,
    obj,
    optional_rows,
    recording_labels,
    released,
    rows,
    text,
)
from shelfmark.metadata_providers.editions.catalog_types import (
    CatalogSource,
    SourceError,
    SourceRecord,
    SourceRef,
)
from shelfmark.metadata_providers.editions.models import AudiobookEdition
from shelfmark.metadata_providers.editions.multisource import normalized_isbn

if TYPE_CHECKING:
    from shelfmark.metadata_providers.editions.source_http import HttpResponse, JsonPostTransport

NANOID = re.compile(r"[346789ABCDEFGHJKLMNPQRTUVWXYabcdefghijkmnpqrtwxyz]{12}")
ASIN = re.compile(r"B[0-9][A-Z0-9]{8}|[0-9]{9}[0-9X]")


def identifier(value: object) -> str:
    if not isinstance(value, str) or not NANOID.fullmatch(value):
        raise SourceError("Invalid AudiobookDB release/work identifier")
    return value


def validate_contract(specification: object) -> None:
    spec = obj(specification)
    if obj(spec.get("info")).get("version") != "2.0.0":
        raise SourceError("AudiobookDB API version requires review")
    paths = obj(spec.get("paths"))
    for path, method, model in (
        ("/search", "post", None),
        ("/books/{id}", "get", "Book"),
        ("/releases/{id}", "get", "Release"),
        ("/audiobooks/external/{category}/{itemId}", "get", "ExternalAudiobook"),
    ):
        operation = obj(obj(paths.get(path)).get(method))
        schema = obj(
            obj(
                obj(obj(obj(operation.get("responses")).get("200")).get("content")).get(
                    "application/json"
                )
            ).get("schema")
        )
        if model:
            if schema.get("$ref") != "#/components/schemas/" + model:
                raise SourceError("AudiobookDB response contract changed")
        elif schema.get("type") != "array":
            raise SourceError("AudiobookDB search envelope changed")
    models = obj(obj(spec.get("components")).get("schemas"))
    for model, fields in (
        ("Book", {"id", "title", "people", "releases", "series"}),
        ("Release", {"id", "title", "book", "isbn", "external", "people", "language"}),
    ):
        if not fields <= obj(obj(models.get(model)).get("properties")).keys():
            raise SourceError("AudiobookDB model requires review")
    auth = obj(obj(obj(spec.get("components")).get("securitySchemes")).get("apiKeyAuth"))
    if auth.get("in") != "header" or auth.get("name") != "X-API-Key":
        raise SourceError("AudiobookDB authentication contract changed")


def _people(data: dict[str, object], role: str) -> tuple[str, ...]:
    selected = []
    for raw in optional_rows(data, "people"):
        entry = obj(raw)
        if (text(obj(entry.get("role")), "name") or "").casefold() == role and entry.get(
            "person"
        ) is not None:
            selected.append(obj(entry["person"]))
    return names({"people": selected}, "people")


class AudiobookDBSource(CatalogSource):
    key = family = "audiobookdb"
    unique_recording_ids = True
    page_limit = 10  # Upstream search has a hard ten-hit cap and no pagination.
    discovery_cost = lookup_cost = 4  # Search + at most three work-detail reads.

    def __init__(self, transport: JsonPostTransport) -> None:
        self.transport = transport

    def _request(
        self, path: str, deadline: float, payload: dict[str, object] | None = None
    ) -> HttpResponse:
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise SourceError("AudiobookDB deadline reached")
        response = (
            self.transport.get(path, {}, remaining)
            if payload is None
            else self.transport.post(path, payload, remaining)
        )
        if response.status not in {200, 404}:
            raise SourceError(f"AudiobookDB HTTP {response.status}")
        return response

    def _record(self, raw: object, book: dict[str, object] | None = None) -> SourceRecord:
        data = obj(raw)
        release_id = identifier(data.get("id"))
        title = text(data, "title", required=True)
        if title is None:
            raise SourceError("Recording title is required")
        work_id: str | None = None
        if book is not None:
            work_id = identifier(book.get("id"))
            if data.get("book") is not None and identifier(obj(data["book"]).get("id")) != work_id:
                raise SourceError("AudiobookDB release belongs to a different work")
        elif data.get("book") is not None:
            work_id = identifier(obj(data["book"]).get("id"))
        isbn10, isbn13 = isbn_fields(text(data, "isbn"))  # Only Release.isbn; never Book.external.
        asins: set[str] = set()
        external_ids: list[dict[str, str]] = []
        for raw_external in optional_rows(data, "external"):
            external = obj(raw_external)
            category = obj(external.get("category"))
            name, item = (
                text(category, "title", required=True),
                text(external, "itemId", required=True),
            )
            if name is None or item is None:
                raise SourceError("External recording identifier is required")
            if category.get("itemKind") == "id" and re.fullmatch(r"[A-Za-z0-9_-]{1,256}", item):
                external_ids.append({"category": name, "id": item})
            if name.casefold() == "audible":
                if category.get("itemKind") != "id" or not ASIN.fullmatch(item):
                    raise SourceError("AudiobookDB Audible identifier is not a recording ASIN")
                asins.add(item)
        if len(asins) > 1:
            raise SourceError("AudiobookDB release has conflicting ASINs")
        publisher = (
            text(obj(data["publisher"]), "name") if data.get("publisher") is not None else None
        )
        subtitle, disambiguation = text(data, "subtitle"), text(data, "disambiguation")
        part, total, production = recording_labels(title, subtitle, publisher, disambiguation)
        series_name: str | None = None
        position: float | None = None
        warnings: list[str] = []
        series = optional_rows(book, "series") if book is not None else []
        if series:
            membership = obj(series[0])
            series_name = text(obj(membership.get("series")), "title")
            placement = obj(membership.get("position"))
            if placement.get("status") == "complete" and placement.get("kind") == "point":
                raw_position = placement.get("sortKey")
                if raw_position is not None:
                    if (
                        isinstance(raw_position, bool)
                        or not isinstance(raw_position, (int, float))
                        or not math.isfinite(raw_position)
                    ):
                        raise SourceError("Invalid literary series position")
                    position = float(raw_position)
            elif placement.get("status") != "absent":
                warnings.append("Unsupported nonnumeric series position")
        language = text(obj(data["language"]), "name") if data.get("language") is not None else None
        release_date = released(text(data, "releaseDate"))
        facts: dict[str, object] = {"externalIds": external_ids}
        if work_id:
            facts["bookId"] = work_id
        seconds = number(data.get("runtimeLengthSec"))
        if seconds is not None:
            facts["lengthMinutes"] = seconds / 60
        if text(data, "duration"):
            facts["duration"] = text(data, "duration")
        edition = AudiobookEdition(
            self.key,
            release_id,
            title,
            authors=_people(book, "author") if book is not None else _people(data, "author"),
            narrators=_people(data, "narrator"),
            subtitle=subtitle,
            isbn_10=isbn10,
            isbn_13=isbn13,
            asin=next(iter(asins), None),
            publisher=publisher,
            language=language,
            release_date=release_date,
            series_name=series_name,
            series_position=position,
            parent_work_id="audiobookdb:" + work_id if work_id else None,
            part_number=part,
            part_total=total,
            canonical_title=text(book, "title") if book is not None else None,
            edition_label="Dramatized Adaptation" if production == "dramatized" else None,
            source_url="https://audiobookdb.org/api/releases/" + release_id,
        )
        return SourceRecord(
            SourceRef(self.key, release_id),
            edition,
            asin_verified=bool(asins),
            recording_isbns=(isbn13,) if isbn13 else (),
            production=production,
            warnings=tuple(warnings),
            facts=tuple(
                (key, json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
                for key, value in facts.items()
            ),
        )

    def _get_release(self, release_id: str, deadline: float) -> SourceRecord | None:
        identifier(release_id)
        response = self._request("/releases/" + release_id, deadline)
        if response.status == 404:
            return None
        data = obj(response.body)
        if data.get("id") != release_id:
            raise SourceError("AudiobookDB returned a different release")
        book: dict[str, object] | None = None
        if data.get("book") is not None:
            work_id = identifier(obj(data["book"]).get("id"))
            work_response = self._request("/books/" + work_id, deadline)
            if work_response.status == 404:
                raise SourceError("AudiobookDB release work is unavailable")
            book = obj(work_response.body)
            if book.get("id") != work_id:
                raise SourceError("AudiobookDB returned a different work")
        return self._record(data, book)

    def get(self, record_id: str, timeout: float) -> SourceRecord | None:
        deadline = monotonic() + timeout
        if NANOID.fullmatch(record_id):
            return self._get_release(record_id, deadline)
        if not ASIN.fullmatch(record_id):
            raise SourceError("Invalid AudiobookDB lookup identity")
        response = self._request(
            "/audiobooks/external/audible/" + quote(record_id, safe=""), deadline
        )
        if response.status == 404:
            return None
        # A work-only external match does not identify any recording.
        resolved = obj(response.body)
        work_id = identifier(resolved.get("id"))
        matched = resolved.get("matchedReleaseId")
        if matched is None:
            return None
        record = self._get_release(identifier(matched), deadline)
        if record is not None and (
            record.edition.asin != record_id
            or record.edition.parent_work_id != "audiobookdb:" + work_id
        ):
            raise SourceError(
                "AudiobookDB external lookup did not verify the selected recording/work"
            )
        return record

    def search(
        self, query: str, search_type: str, limit: int, timeout: float
    ) -> tuple[SourceRecord, ...]:
        if (
            limit <= 0
            or not query.strip()
            or search_type not in {"general", "title", "author", "isbn", "series"}
        ):
            return ()
        if ASIN.fullmatch(query.strip().upper()) and search_type != "isbn":
            found = self.get(query.strip().upper(), timeout)
            return (found,) if found else ()
        isbn = normalized_isbn(query) if search_type == "isbn" else None
        if search_type == "isbn" and isbn is None:
            return ()
        if len(query.strip()) < 3:
            return ()
        deadline = monotonic() + timeout
        payload: dict[str, object] = {"q": query.strip(), "type": "books"}
        if search_type == "author":
            payload = {"type": "books", "filters": {"person": query.strip(), "role": "Author"}}
        response = self._request("/search", deadline, payload)
        if response.status != 200:
            raise SourceError("Unexpected AudiobookDB search 404")
        hits = rows(response.body)
        if len(hits) > 10:
            raise SourceError("AudiobookDB search contract exceeded ten-hit limit")
        records: list[SourceRecord] = []
        bound = min(limit, self.page_limit)
        bounded = len(hits) > 3 or len(hits) == 10
        seen_works: set[str] = set()
        for hit in hits[:3]:
            work_id = identifier(obj(hit).get("id"))
            if work_id in seen_works:
                raise SourceError("Duplicate AudiobookDB search work")
            seen_works.add(work_id)
            work_response = self._request("/books/" + work_id, deadline)
            if work_response.status == 404:
                raise SourceError("AudiobookDB search work is no longer available")
            work = obj(work_response.body)
            if work.get("id") != work_id:
                raise SourceError("AudiobookDB returned a different search work")
            releases = optional_rows(work, "releases")
            bounded |= len(records) + len(releases) > bound
            for release in releases:
                record = self._record(release, work)
                if isbn is None or record.edition.isbn_13 == isbn:
                    records.append(record)
                    if len(records) == bound:
                        break
            if len(records) == bound:
                break
        if bounded:
            note = (
                "audiobookdb: discovery window reached; page 1 saturated, additional pages not read"
            )
            records = [replace(record, warnings=(*record.warnings, note)) for record in records]
        return tuple(records)

    def lookup(self, selected: SourceRecord, timeout: float) -> tuple[SourceRecord, ...]:
        if selected.reference.source == self.key:
            found = self.get(selected.reference.record_id, timeout)
            return (found,) if found else ()
        if selected.asin_verified and selected.edition.asin:
            found = self.get(selected.edition.asin, timeout)
            return (found,) if found else ()
        if selected.recording_isbns:
            return self.search(selected.recording_isbns[0], "isbn", self.page_limit, timeout)
        return ()

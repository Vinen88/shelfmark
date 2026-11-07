"""Reviewed Libex 2.4.x adapters. Live/historical retain independent provenance."""

import json
import math
import re
from dataclasses import replace
from datetime import date, datetime
from typing import TYPE_CHECKING, cast
from urllib.parse import quote

from shelfmark.metadata_providers.editions.catalog_types import (
    Availability,
    CatalogSource,
    SourceError,
    SourceRecord,
    SourceRef,
)
from shelfmark.metadata_providers.editions.models import AudiobookEdition
from shelfmark.metadata_providers.editions.regional import libex_evidence

if TYPE_CHECKING:
    from collections.abc import Mapping

    from shelfmark.metadata_providers.editions.source_http import HttpResponse, JsonTransport

SUPPORTED_MARKETS = frozenset({"us", "ca", "uk", "au", "de", "fr", "it", "es", "jp", "in", "br"})
PART = re.compile(r"\b(?:part\s+)?(\d+)\s+of\s+(\d+)\b", re.IGNORECASE)
API_VERSION = "2.4.0"
REVIEWED_API_VERSIONS = frozenset({API_VERSION, "2.4.2"})


def validate_contract(specification: object) -> None:
    """Explicit documentation check, never an automatic metadata/startup request."""
    spec = _object(specification)
    if _object(spec.get("info")).get("version") not in REVIEWED_API_VERSIONS:
        raise SourceError("Libex API version requires a contract review")
    paths = _object(spec.get("paths"))
    expected = {
        "/search": {"title", "author", "region", "limit", "page"},
        "/db/book": {"title", "author_name", "isbn", "series_name", "region", "limit", "page"},
        "/book/{asin}": {"asin", "cache", "region"},
        "/db/book/{asin}": {"asin", "region"},
        "/db/book/sku/{sku}": {"sku"},
    }
    for path, required in expected.items():
        operation = _object(_object(paths.get(path)).get("get"))
        parameters = operation.get("parameters")
        if not isinstance(parameters, list):
            raise SourceError("Missing Libex query parameters")
        names = {_text(_object(item), "name") for item in parameters}
        if not required <= names:
            raise SourceError("Libex query contract changed")
        by_name = {_text(_object(item), "name"): _object(item) for item in parameters}
        if path in {"/search", "/db/book"}:
            expected_page, expected_limit = (0, 50) if path == "/search" else (1, 100)
            if (
                _object(by_name["page"].get("schema")).get("default") != expected_page
                or _object(by_name["limit"].get("schema")).get("maximum") != expected_limit
            ):
                raise SourceError("Libex pagination contract changed")
        schema = _object(
            _object(_object(_object(operation.get("responses")).get("200")).get("content")).get(
                "application/json"
            )
        )
        response_schema = _object(schema.get("schema"))
        if path in {"/search", "/db/book", "/db/book/sku/{sku}"}:
            if response_schema.get("type") != "array":
                raise SourceError("Libex search envelope changed")
            response_schema = _object(response_schema.get("items"))
        if response_schema.get("$ref") != "#/components/schemas/BookResponse":
            raise SourceError("Libex response model changed")
    models = _object(_object(spec.get("components")).get("schemas"))
    model = _object(models.get("BookResponse"))
    required_fields = model.get("required")
    if (
        not isinstance(required_fields, list)
        or not all(isinstance(field, str) for field in required_fields)
        or set(required_fields) != {"asin", "region"}
    ):
        raise SourceError("Libex required fields changed")
    properties = _object(model.get("properties"))
    for key in ("asin", "region"):
        if _object(properties.get(key)).get("type") != "string":
            raise SourceError("Libex identity field type changed")
    for key in (
        "asin",
        "region",
        "title",
        "isbn",
        "publisher",
        "releaseDate",
        "imageUrl",
        "language",
        "authors",
        "narrators",
        "series",
        "manualEntry",
    ):
        if key not in properties:
            raise SourceError(f"Libex field missing: {key}")


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise SourceError("Expected a metadata object")
    return cast("dict[str, object]", value)


def _text(data: Mapping[str, object], key: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise SourceError(f"Invalid string field: {key}")
    return value.strip() or None


def _names(data: Mapping[str, object], key: str) -> tuple[str, ...]:
    value = data.get(key, [])
    if not isinstance(value, list):
        raise SourceError(f"Invalid list field: {key}")
    return tuple(dict.fromkeys(name for entry in value if (name := _text(_object(entry), "name"))))


class LibexSource(CatalogSource):
    unique_recording_ids = True  # One ASIN per marketplace/mode.
    supports_paging = True

    @property
    def marketplace(self) -> str:
        return self._marketplace

    def __init__(
        self, transport: JsonTransport, marketplace: str, *, historical: bool = False
    ) -> None:
        if marketplace not in SUPPORTED_MARKETS:
            raise ValueError("Unsupported Libex marketplace")
        self.transport = transport
        self._marketplace = marketplace
        self.historical = historical
        self.page_limit = 100 if historical else 50
        self.family = "libex_history" if historical else "libex_live"
        self.key = f"{self.family}_{marketplace}"

    def _parse(
        self, raw: object, response: HttpResponse, *, live_search: bool = False
    ) -> SourceRecord:
        data = _object(raw)
        asin = _text(data, "asin")
        region = _text(data, "region")
        title = _text(data, "title")
        if (
            not asin
            or not re.fullmatch(r"[A-Z0-9]{10}", asin)
            or region != self.marketplace
            or not title
        ):
            raise SourceError("Missing/mismatched Libex recording identity, region or title")
        # manualEntry records an earlier delisting report; it is NOT proof of current delisting.
        origin = response.headers.get("x-libex-source", "").lower()
        availability = (
            Availability.HISTORICAL
            if self.historical or origin == "db"
            else Availability.LIVE
            if live_search or origin == "audible"
            else Availability.UNKNOWN
        )
        warnings: list[str] = []
        if data.get("manualEntry") is True:
            warnings.append("Historical delisting report; current availability unverified")
        if response.headers.get("x-libex-complete", "true").lower() == "false":
            warnings.append(
                "Libex response incomplete: "
                + response.headers.get("x-libex-incomplete-reason", "unspecified")
            )
        datestr = _text(data, "releaseDate")
        try:
            released = (
                (
                    datetime.fromisoformat(datestr).date()
                    if "T" in datestr or " " in datestr
                    else date.fromisoformat(datestr)
                )
                if datestr
                else None
            )
        except ValueError as exc:
            raise SourceError("Invalid Libex releaseDate") from exc
        if released and released.year == 2200:
            raise SourceError("Libex/Audible placeholder record")
        isbn = _text(data, "isbn")
        # Do not turn malformed publisher SKUs into identifiers or manufacture ISBN variants.
        from shelfmark.metadata_providers.editions.multisource import normalized_isbn

        valid_isbn = isbn if isbn and normalized_isbn(isbn) else None
        production = (
            "dramatized" if re.search(r"dramatized\s+adaptation", title, re.IGNORECASE) else None
        )
        part = PART.search(title)
        series_raw = data.get("series", [])
        if not isinstance(series_raw, list):
            raise SourceError("Invalid Libex series")
        series_entries = tuple(_object(entry) for entry in series_raw)

        def positioned(entry: Mapping[str, object]) -> bool:
            value = _text(entry, "position")
            try:
                return value is not None and math.isfinite(float(value))
            except ValueError:
                return False

        # Unnumbered umbrella series can precede the numbered novel series in
        # search but follow it in detail. Preserve all entries in source facts.
        series = next(
            (entry for entry in series_entries if positioned(entry)),
            series_entries[0] if series_entries else {},
        )
        position_raw = _text(series, "position")
        try:
            position = float(position_raw) if position_raw else None
            if position is not None and not math.isfinite(position):
                raise ValueError("Nonfinite position")  # noqa: TRY301 -- Keep validation beside the parsed field.
        except ValueError:
            position = (
                None  # Preserve the nonnumeric value as provenance, never reinterpret it as a part.
            )
            warnings.append("Unsupported nonnumeric series position")
        edition = AudiobookEdition(
            catalog=self.key,
            edition_id=asin,
            title=title,
            authors=_names(data, "authors"),
            narrators=_names(data, "narrators"),
            subtitle=_text(data, "subtitle"),
            asin=asin,
            isbn_10=valid_isbn
            if valid_isbn and len(re.sub(r"[-\s]", "", valid_isbn)) == 10
            else None,
            isbn_13=valid_isbn
            if valid_isbn and len(re.sub(r"[-\s]", "", valid_isbn)) == 13
            else None,
            publisher=_text(data, "publisher"),
            release_date=released,
            cover_url=_text(data, "imageUrl"),
            language=_text(data, "language"),
            description=_text(data, "description") or _text(data, "summary"),
            source_url=_text(data, "link"),
            series_name=_text(series, "name"),
            series_position=position,
            part_number=int(part[1]) if part else None,
            part_total=int(part[2]) if part else None,
            edition_label="Dramatized Adaptation" if production else None,
        )
        fact_keys = (
            "region",
            "regions",
            "manualEntry",
            "isAvailable",
            "isBuyable",
            "isListenable",
            "productState",
            "updatedAt",
            "lengthMinutes",
            "bookFormat",
            "series",
            "sku",
            "skuGroup",
            "isbn",
            "releaseDate",
            "copyright",
        )
        facts = tuple(
            (
                key,
                json.dumps(
                    data[key],
                    ensure_ascii=False,
                    sort_keys=True,
                    allow_nan=False,
                    separators=(",", ":"),
                ),
            )
            for key in fact_keys
            if key in data
        )
        record = SourceRecord(
            SourceRef(self.key, asin, region, availability),
            edition,
            asin_verified=True,
            production=production,
            warnings=tuple(warnings),
            facts=facts,
        )
        evidence = libex_evidence(record)
        return replace(
            record,
            regional_evidence=evidence,
            recording_isbns=(evidence.recording_isbn,)
            if evidence and evidence.recording_isbn
            else (),
        )

    def _request(self, path: str, parameters: Mapping[str, str], timeout: float) -> HttpResponse:
        response = self.transport.get(path, parameters, timeout)
        if response.status not in (200, 404):
            raise SourceError(f"Libex HTTP {response.status}")
        return response

    def search(
        self, query: str, search_type: str, limit: int, timeout: float
    ) -> tuple[SourceRecord, ...]:
        return self.search_page(query, search_type, limit, timeout)

    def search_page(
        self, query: str, search_type: str, limit: int, timeout: float, *, page: int = 1
    ) -> tuple[SourceRecord, ...]:
        if page < 1:
            raise ValueError("Expected a one-based discovery page")
        parameters = {
            "region": self.marketplace,
            "limit": str(min(limit, 50 if not self.historical else 100)),
            "page": str(page if self.historical else page - 1),
        }
        if search_type == "isbn":
            if not self.historical:
                return ()  # Live search has no documented ISBN parameter.
            parameters["isbn"] = query
        elif search_type == "author":
            parameters["author_name" if self.historical else "author"] = query
        elif search_type == "series":
            parameters["series_name" if self.historical else "title"] = query
        elif search_type in {"general", "title"}:
            parameters["title"] = query
        else:
            return ()
        response = self._request("/db/book" if self.historical else "/search", parameters, timeout)
        complete = response.headers.get("x-libex-complete", "true").lower() != "false"
        if response.status == 404:
            if (
                complete
                and self.historical
                and _text(_object(response.body), "code") == "not_in_libex"
            ):
                return ()
            raise SourceError("Unexpected search route 404")
        if not isinstance(response.body, list):
            raise SourceError("Libex flat search must return an array, not an ABS matches envelope")
        if not response.body and not complete:
            raise SourceError("Libex search incomplete without usable records")
        if len(response.body) > int(parameters["limit"]):
            raise SourceError("Libex exceeded requested limit")
        return tuple(
            self._parse(row, response, live_search=not self.historical) for row in response.body
        )

    def get(self, record_id: str, timeout: float) -> SourceRecord | None:
        if not re.fullmatch(r"[A-Z0-9]{10}", record_id):
            raise SourceError("Invalid ASIN")
        parameters = {"region": self.marketplace}
        if not self.historical:
            parameters["cache"] = "true"
        response = self._request(
            ("/db/book/" if self.historical else "/book/") + quote(record_id, safe=""),
            parameters,
            timeout,
        )
        if response.status == 404:
            code = _text(_object(response.body), "code")
            if code not in {"not_in_libex", "not_on_audible"}:
                raise SourceError("Unexpected Libex detail 404")
            return None
        result = self._parse(response.body, response)
        if result.reference.record_id != record_id:
            raise SourceError("Libex returned a different ASIN")
        return result

    def lookup(self, selected: SourceRecord, timeout: float) -> tuple[SourceRecord, ...]:
        if self.historical and selected.regional_evidence is not None:
            # Exact SKU candidates restore regional aliases even without an ISBN.
            group = selected.regional_evidence.group_id
            response = self._request("/db/book/sku/" + quote(group, safe=""), {}, timeout)
            if (
                response.status != 200
                or not isinstance(response.body, list)
                or len(response.body) > 32
            ):
                raise SourceError("Invalid Libex SKU group response")
            return tuple(
                self._parse(row, response)
                for row in response.body
                if _text(_object(row), "region") == self.marketplace
                and _text(_object(row), "skuGroup") == group
            )
        if not selected.asin_verified or not selected.edition.asin:
            return ()
        found = self.get(selected.edition.asin, timeout)
        return (found,) if found else ()

"""Opt-in regional Audnexus ASIN/detail and reviewed 1.15 book-search route."""

import json
import math
import re
from typing import TYPE_CHECKING
from urllib.parse import quote

from shelfmark.metadata_providers.editions.catalog_fields import (
    names,
    number,
    obj,
    recording_labels,
    released,
    rows,
    text,
)
from shelfmark.metadata_providers.editions.catalog_types import (
    Availability,
    CatalogSource,
    SourceError,
    SourceRecord,
    SourceRef,
)
from shelfmark.metadata_providers.editions.models import AudiobookEdition

if TYPE_CHECKING:
    from shelfmark.metadata_providers.editions.source_http import JsonTransport

ASIN = re.compile(r"B[0-9][A-Z0-9]{8}|[0-9]{9}[0-9X]")
MARKETS = {
    "us": "com",
    "ca": "ca",
    "uk": "co.uk",
    "au": "com.au",
    "de": "de",
    "es": "es",
    "fr": "fr",
    "in": "in",
    "it": "it",
    "jp": "co.jp",
}


class AudnexusSource(CatalogSource):
    family = "audnexus"
    unique_recording_ids = True
    page_limit = 10

    @property
    def marketplace(self) -> str:
        return self._marketplace

    def __init__(self, transport: JsonTransport, marketplace: str) -> None:
        if marketplace not in MARKETS:
            raise ValueError("Unsupported Audnexus marketplace")
        self.transport, self._marketplace = transport, marketplace
        self.key = "audnexus_" + marketplace

    def _parse(self, raw: object) -> SourceRecord:
        data = obj(raw)
        asin, region = text(data, "asin", required=True), text(data, "region", required=True)
        if asin is None or not ASIN.fullmatch(asin) or region != self.marketplace:
            raise SourceError("Audnexus returned a mismatched ASIN/marketplace")
        title = text(data, "title", required=True)
        if title is None:
            raise SourceError("Recording title is required")
        publisher, subtitle = text(data, "publisherName"), text(data, "subtitle")
        part, total, production = recording_labels(title, subtitle, publisher)
        raw_isbn = text(data, "isbn")
        series = obj(data["seriesPrimary"]) if data.get("seriesPrimary") is not None else {}
        series_name = text(series, "name")
        position_raw = text(series, "position")
        warnings: tuple[str, ...] = ()
        try:
            position = float(position_raw) if position_raw else None
            if position is not None and not math.isfinite(position):
                raise ValueError("Invalid position")  # noqa: TRY301 -- Keep validation beside the parsed field.
        except ValueError:
            position = None
            warnings = ("Unsupported nonnumeric series position",)
        minutes = number(data.get("runtimeLengthMin"))
        edition = AudiobookEdition(
            self.key,
            asin,
            title,
            authors=names(data, "authors"),
            narrators=names(data, "narrators"),
            publisher=publisher,
            subtitle=subtitle,
            asin=asin,
            language=text(data, "language"),
            release_date=released(text(data, "releaseDate")),
            series_name=series_name,
            series_position=position,
            part_number=part,
            part_total=total,
            edition_label="Dramatized Adaptation" if production == "dramatized" else None,
            source_url="https://www.audible." + MARKETS[self.marketplace] + "/pd/" + asin,
        )
        facts: dict[str, object] = {"region": self.marketplace}
        if raw_isbn:
            # Audnexus copies Audible.product.isbn. As with Libex, that raw
            # field alone does not prove a recording ISBN; ASIN is sufficient
            # for exact selection. Keep it losslessly as unverified source data.
            facts["isbn"] = raw_isbn
        if minutes is not None:
            facts["lengthMinutes"] = minutes
        if series:
            facts["series"] = [
                {k: v for k, v in series.items() if k in {"asin", "name", "position"}}
            ]
        if text(data, "formatType") is not None:
            facts["bookFormat"] = text(data, "formatType")
        if data.get("copyright") is not None:
            facts["copyright"] = number(data["copyright"])
        return SourceRecord(
            SourceRef(self.key, asin, self.marketplace, Availability.UNKNOWN),
            edition,
            asin_verified=True,
            production=production,
            warnings=warnings,
            facts=tuple(
                (key, json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
                for key, value in facts.items()
            ),
        )

    def search(
        self, query: str, search_type: str, limit: int, timeout: float
    ) -> tuple[SourceRecord, ...]:
        if (
            limit <= 0
            or search_type not in {"general", "title", "author", "series"}
            or not query.strip()
        ):
            return ()  # ISBN search has no reviewed Audnexus endpoint.
        if ASIN.fullmatch(query.strip().upper()):
            found = self.get(query.strip().upper(), timeout)
            return (found,) if found else ()
        response = self.transport.get(
            "/books/search", {"q": query.strip(), "region": self.marketplace}, timeout
        )
        if response.status != 200:
            raise SourceError(
                f"Audnexus search HTTP {response.status}; instance must support /books/search"
            )
        entries = rows(response.body)
        if len(entries) > 10:
            raise SourceError("Audnexus search contract exceeded ten-hit limit")
        return tuple(self._parse(raw) for raw in entries[: min(limit, self.page_limit)])

    def get(self, record_id: str, timeout: float) -> SourceRecord | None:
        if not ASIN.fullmatch(record_id):
            raise SourceError("Invalid Audnexus recording ASIN")
        response = self.transport.get(
            "/books/" + quote(record_id, safe=""),
            {"region": self.marketplace, "update": "0", "seedAuthors": "0"},
            timeout,
        )
        if response.status == 404:
            return None
        if response.status != 200:
            raise SourceError(f"Audnexus HTTP {response.status}")
        record = self._parse(response.body)
        if record.reference.record_id != record_id:
            raise SourceError("Audnexus returned a different recording")
        return record

    def lookup(self, selected: SourceRecord, timeout: float) -> tuple[SourceRecord, ...]:
        if not selected.asin_verified or not selected.edition.asin:
            return ()
        found = self.get(selected.edition.asin, timeout)
        return (found,) if found else ()

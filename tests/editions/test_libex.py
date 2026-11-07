"""Libex OpenAPI 2.4.0 shapes, synthetic responses and a stubbed HTTP layer."""

import unittest
from collections.abc import Mapping
from dataclasses import replace
from unittest.mock import MagicMock, patch

from shelfmark.metadata_providers.editions.catalog_types import (
    AggregatedEdition,
    Availability,
    RateLimited,
    SourceError,
)
from shelfmark.metadata_providers.editions.libex import LibexSource
from shelfmark.metadata_providers.editions.multisource import MultiSourceBackend
from shelfmark.metadata_providers.editions.source_http import HttpClient, HttpResponse


def book(asin: str = "MOCKASIN01", region: str = "ca") -> dict[str, object]:
    return {
        "asin": asin,
        "region": region,
        "title": "Lantern (1 of 2) [Dramatized Adaptation]",
        "authors": [{"name": "Example Author"}],
        "narrators": [{"name": "Example Cast"}],
        "language": "English",
        "isbn": "9780000000019",
        "publisher": "Fixture publisher",
        "imageUrl": "https://example.invalid/cover.jpg",
        "releaseDate": "2025-01-15",
        "series": [{"name": "Lantern Cycle", "position": "1"}],
        "manualEntry": False,
        "isAvailable": True,
        "lengthMinutes": 300,
        "bookFormat": "unabridged",
    }


class FixtureHttp:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, str], float]] = []
        self.next = HttpResponse(200, book(), {"x-libex-source": "audible"})

    def get(self, path: str, parameters: Mapping[str, str], timeout: float) -> HttpResponse:
        self.calls.append((path, dict(parameters), timeout))
        return self.next


class LibexTests(unittest.TestCase):
    def test_live_and_database_search_parameter_and_envelope_contracts(self) -> None:
        http = FixtureHttp()
        http.next = HttpResponse(200, [book()], {})
        live, history = LibexSource(http, "ca"), LibexSource(http, "ca", historical=True)
        first = live.search("Lantern", "title", 40, 2)[0]
        self.assertEqual(
            http.calls[-1],
            ("/search", {"region": "ca", "limit": "40", "page": "0", "title": "Lantern"}, 2),
        )
        self.assertEqual(first.reference.availability, Availability.LIVE)
        archived = history.search("Example Author", "author", 40, 2)[0]
        self.assertEqual(http.calls[-1][0], "/db/book")
        self.assertEqual(http.calls[-1][1]["author_name"], "Example Author")
        self.assertEqual(http.calls[-1][1]["page"], "1")
        self.assertEqual(archived.reference.availability, Availability.HISTORICAL)
        http.next = HttpResponse(200, {"matches": [book()]}, {})
        with self.assertRaises(SourceError):
            live.search("Lantern", "title", 10, 2)

    def test_detail_identity_fields_and_explicit_part_mapping(self) -> None:
        http = FixtureHttp()
        live = LibexSource(http, "ca")
        found = live.get("MOCKASIN01", 2)
        assert found is not None
        self.assertEqual(found.edition.asin, "MOCKASIN01")
        self.assertEqual(found.edition.isbn_13, "9780000000019")
        self.assertEqual(found.edition.part_number, 1)
        self.assertEqual(found.edition.part_total, 2)
        self.assertEqual(found.edition.series_position, 1)
        self.assertEqual(found.production, "dramatized")
        self.assertEqual(found.edition.edition_label, "Dramatized Adaptation")
        self.assertEqual(found.recording_isbns, ())
        self.assertEqual(http.calls[-1][0], "/book/MOCKASIN01")
        self.assertEqual(http.calls[-1][1]["cache"], "true")

    def test_historical_detail_does_not_reach_live_route_or_infer_delisting(self) -> None:
        http = FixtureHttp()
        data = book()
        data.update(manualEntry=True, isAvailable=False, isBuyable=False)
        http.next = HttpResponse(200, data, {})
        history = LibexSource(http, "ca", historical=True)
        result = history.get("MOCKASIN01", 2)
        assert result is not None
        self.assertEqual(result.reference.availability, Availability.HISTORICAL)
        self.assertTrue(result.warnings)
        self.assertEqual(http.calls[-1][0], "/db/book/MOCKASIN01")
        self.assertNotIn("cache", http.calls[-1][1])
        self.assertIn(("manualEntry", "true"), result.facts)

    def test_cache_and_incomplete_headers_do_not_claim_live_availability(self) -> None:
        http = FixtureHttp()
        http.next = HttpResponse(
            200,
            book(),
            {
                "x-libex-source": "cache",
                "x-libex-complete": "false",
                "x-libex-incomplete-reason": "hydration-failed",
            },
        )
        found = LibexSource(http, "ca").get("MOCKASIN01", 2)
        assert found is not None
        self.assertEqual(found.reference.availability, Availability.UNKNOWN)
        self.assertTrue(found.warnings)

    def test_404_notfound_is_distinct_from_outage_and_unknown_error(self) -> None:
        http = FixtureHttp()
        source = LibexSource(http, "ca", historical=True)
        http.next = HttpResponse(404, {"code": "not_in_libex"}, {})
        self.assertIsNone(source.get("MOCKASIN01", 2))
        for response in (
            HttpResponse(503, {"code": "upstream_unavailable"}, {}),
            HttpResponse(404, {"detail": "route not found"}, {}),
        ):
            http.next = response
            with self.assertRaises(SourceError):
                source.get("MOCKASIN01", 2)

    def test_malformed_missing_and_conflicting_ids_are_refused(self) -> None:
        http = FixtureHttp()
        source = LibexSource(http, "ca")
        for changes in (
            {"asin": "OTHERASIN1"},
            {"asin": None},
            {"title": None},
            {"region": "us"},
            {"releaseDate": "2200-01-01"},
            {"releaseDate": "not-date"},
            {"authors": "Author"},
        ):
            data = book()
            data.update(changes)
            http.next = HttpResponse(200, data, {})
            with self.assertRaises((SourceError, ValueError)):
                source.get("MOCKASIN01", 2)
        with self.assertRaises(ValueError):
            LibexSource(http, "en")

    def test_observed_historical_empty_search_is_not_an_outage(self) -> None:
        http = FixtureHttp()
        http.next = HttpResponse(
            404,
            {"code": "not_in_libex", "error": "No books found matching the given parameters"},
            {},
        )
        self.assertEqual(
            LibexSource(http, "au", historical=True).search("Lantern", "title", 40, 2), ()
        )
        with self.assertRaises(SourceError):
            LibexSource(http, "au").search("Lantern", "title", 40, 2)
        for body in ({"code": "route_not_found"}, {"detail": "not found"}):
            http.next = HttpResponse(404, body, {})
            with self.assertRaises(SourceError):
                LibexSource(http, "au", historical=True).search("Lantern", "title", 40, 2)

    def test_incomplete_optional_metadata_is_explicit(self) -> None:
        http = FixtureHttp()
        http.next = HttpResponse(
            200,
            {
                "asin": "MOCKASIN01",
                "region": "ca",
                "title": "Lantern",
                "isbn": "9DLInvalid",
                "bookFormat": "unabridged",
            },
            {},
        )
        result = LibexSource(http, "ca").get("MOCKASIN01", 2)
        assert result is not None
        self.assertIsNone(result.edition.language)
        self.assertIsNone(result.edition.isbn_13)
        self.assertEqual(result.edition.narrators, ())
        self.assertIsNone(result.production)
        self.assertIsNone(result.edition.part_number)

    def test_real_catalog_timestamp_dates_keep_the_source_calendar_day(self) -> None:
        http = FixtureHttp()
        source = LibexSource(http, "ca")
        for raw, expected in (
            ("2026-12-23T00:00:00+00:00", "2026-12-23"),
            ("2022-04-01T00:00:00Z", "2022-04-01"),
            ("2022-04-01T23:30:00-05:00", "2022-04-01"),
            ("2022-04-01", "2022-04-01"),
        ):
            data = book()
            data["releaseDate"] = raw
            http.next = HttpResponse(200, data, {})
            result = source.get("MOCKASIN01", 2)
            assert result is not None and result.edition.release_date is not None
            self.assertEqual(result.edition.release_date.isoformat(), expected)
            self.assertIn(("releaseDate", '"' + raw + '"'), result.facts)

    def test_live_has_no_isbn_search_but_database_does(self) -> None:
        http = FixtureHttp()
        self.assertEqual(LibexSource(http, "ca").search("9780000000019", "isbn", 10, 2), ())
        self.assertEqual(http.calls, [])
        http.next = HttpResponse(200, [book()], {})
        LibexSource(http, "ca", historical=True).search("9780000000019", "isbn", 10, 2)
        self.assertEqual(http.calls[-1][1]["isbn"], "9780000000019")

    def test_lookup_is_asin_based_not_title_based(self) -> None:
        http = FixtureHttp()
        source = LibexSource(http, "ca")
        selected = source.get("MOCKASIN01", 2)
        assert selected is not None
        self.assertEqual(len(source.lookup(selected, 2)), 1)
        self.assertTrue(all(path == "/book/MOCKASIN01" for path, _, _ in http.calls))
        self.assertEqual(source.lookup(replace(selected, asin_verified=False), 2), ())

    def test_numbered_series_survives_search_detail_series_reordering(self) -> None:
        http = FixtureHttp()
        data = book()
        entries = [
            {"name": "Umbrella universe", "position": ""},
            {"name": "Lantern Cycle", "position": "1"},
        ]
        data["series"] = entries
        http.next = HttpResponse(200, [data], {})
        source = LibexSource(http, "ca")
        found = source.search("Lantern", "title", 40, 1)[0]
        http.next = HttpResponse(200, dict(data, series=list(reversed(entries))), {})
        detail = source.get("MOCKASIN01", 1)
        assert detail is not None
        self.assertEqual(
            (found.edition.series_name, found.edition.series_position), ("Lantern Cycle", 1)
        )
        self.assertEqual(
            (detail.edition.series_name, detail.edition.series_position), ("Lantern Cycle", 1)
        )
        self.assertTrue(
            any(key == "series" and "Umbrella universe" in value for key, value in found.facts)
        )

    def test_incomplete_empty_search_cannot_claim_a_complete_empty_catalog(self) -> None:
        http = FixtureHttp()
        for body, status in (([], 200), ({"code": "not_in_libex"}, 404)):
            http.next = HttpResponse(status, body, {"x-libex-complete": "false"})
            with self.assertRaises(SourceError):
                LibexSource(http, "ca", historical=True).search("Lantern", "title", 40, 1)
        http.next = HttpResponse(200, [book()], {"x-libex-complete": "false"})
        found = MultiSourceBackend((LibexSource(http, "ca"),)).search("Lantern")
        self.assertEqual(len(found), 1)
        assert isinstance(found[0], AggregatedEdition)
        self.assertTrue(
            any("Libex response incomplete" in warning for warning in found[0].provenance.warnings)
        )

    def test_retry_after_without_network_or_automatic_retry(self) -> None:
        response = MagicMock()
        response.status = 429
        response.headers.items.return_value = [("Retry-After", "60")]
        with patch(
            "shelfmark.metadata_providers.editions.source_http.urlopen", return_value=response
        ) as open_url:
            client = HttpClient("https://example.invalid")
            with self.assertRaises(RateLimited) as limited:
                client.get("/search", {}, 1)
            self.assertEqual(limited.exception.retry_after, 60)
            with self.assertRaises(RateLimited):
                client.get("/search", {}, 1)
            self.assertEqual(open_url.call_count, 1)

    def test_libex_fixture_backend_search_to_detail_and_history_only(self) -> None:
        http = FixtureHttp()
        history = LibexSource(http, "ca", historical=True)
        http.next = HttpResponse(200, [book()], {})
        backend = MultiSourceBackend((history,))
        found = backend.search("Lantern")
        self.assertEqual(len(found), 1)
        http.next = HttpResponse(200, book(), {})
        self.assertEqual(backend.get(found[0].catalog, found[0].edition_id), found[0])


if __name__ == "__main__":
    unittest.main()

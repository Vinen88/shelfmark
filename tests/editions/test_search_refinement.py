"""Recording relevance, bounded discovery and conservative regional grouping."""

import json
import unittest
from collections.abc import Mapping
from dataclasses import replace
from datetime import date
from time import monotonic, sleep
from unittest.mock import patch

from mock_sources import MockCatalogSource
from test_multisource import record

from shelfmark.metadata_providers.editions.catalog_types import (
    AggregatedEdition,
    SourceError,
    SourceRecord,
)
from shelfmark.metadata_providers.editions.identity import EditionIdentity
from shelfmark.metadata_providers.editions.libex import LibexSource
from shelfmark.metadata_providers.editions.multisource import (
    AggregationOptions,
    MultiSourceBackend,
    decode_identity,
    equivalent,
)
from shelfmark.metadata_providers.editions.multisource_config import MultiSourceSettings
from shelfmark.metadata_providers.editions.regional import regional_equivalent
from shelfmark.metadata_providers.editions.snapshot import EditionSnapshot
from shelfmark.metadata_providers.editions.source_http import HttpResponse


def regional_book(part: int, market: str, total: int = 3) -> dict[str, object]:
    return {
        "asin": f"B0{market.upper()}0000{part:02d}",
        "region": market,
        "title": f"The Lantern Archive (Part {part} of {total}) (Dramatized Adaptation)",
        "authors": [{"name": "Example Author"}],
        "narrators": [{"name": f"Example Narrator {part}"}, {"name": f"Example Cast {part}"}],
        "isbn": ("9780000000019", "9780000000026", "9780000000033")[part - 1],
        "publisher": "Graphic Audio LLC",
        "language": "english",
        "releaseDate": "2025-01-15",
        "imageUrl": f"https://example.invalid/part-{part}.jpg",
        "lengthMinutes": 300 + part,
        "bookFormat": "unabridged",
        "skuGroup": f"BK_GRPH_00000{part}",
        "series": [{"name": "The Lantern Cycle", "position": "1"}],
    }


def culling_books() -> tuple[dict[str, object], ...]:
    """Observed identity fields only; artwork/URLs are synthetic, no catalog prose."""
    records: list[dict[str, object]] = []
    markets = ("ca", "us", "uk", "au")
    for dramatized, asins in (
        (True, ("B0HKK93HCK", "B0HKKHTZNK", "B0HKKJBYKX", "B0HKKFQHXV")),
        (False, ("B0848L35HS", "B0848Q9P8W", "B0848RXY4R", "B0848MSMXJ")),
    ):
        group = "BK_GRPH_005352" if dramatized else "BK_ACX0_180403"
        cast = (
            "Full Cast",
            "Natalie Van Sistine",
            "Jonathan Lee Taylor",
            "Tia Shearer",
            "Mort Shelby",
            "Danny Gavigan",
            "James Lewis",
            "Megan Hastie",
            "Jonathan Sleep",
            "Ryan Dalusung",
            "Christopher Williams",
            "Damon Alums",
        )
        for market, asin in zip(markets, asins, strict=True):
            records.append(
                {
                    "asin": asin,
                    "region": market,
                    "title": (
                        "The Culling Trials, Book 1 [Dramatized Adaptation]"
                        if dramatized
                        else "Shadowspell Academy: The Culling Trials: Book 1"
                    ),
                    "authors": [{"name": name} for name in ("K.F. Breene", "Shannon Mayer")],
                    "narrators": [
                        {"name": name} for name in (cast if dramatized else ("Khristine Hvam",))
                    ],
                    "publisher": "GraphicAudio" if dramatized else "K.F. Breene and Shannon Mayer",
                    "isbn": "9798890557476" if dramatized else None,
                    "releaseDate": "2026-11-06" if dramatized else "2020-01-29",
                    "lengthMinutes": 321 if dramatized else 331,
                    "language": "english",
                    "bookFormat": "unabridged",
                    "skuGroup": group,
                    "sku": group + (market.upper() if market != "us" else ""),
                    "imageUrl": f"https://example.invalid/{group}.jpg",
                    "series": [{"name": "Shadowspell Academy", "position": "1"}],
                }
            )
    return tuple(records)


class RegionalHttp:
    def __init__(self, total: int = 3) -> None:
        self.books = tuple(
            regional_book(part, market, total)
            for market in ("ca", "us", "uk", "au")
            for part in range(1, total + 1)
        )
        self.calls: list[tuple[str, dict[str, str]]] = []

    def get(self, path: str, parameters: Mapping[str, str], timeout: float) -> HttpResponse:
        self.calls.append((path, dict(parameters)))
        if path.startswith("/db/book/sku/"):
            return HttpResponse(
                200, [b for b in self.books if b["skuGroup"] == path.rsplit("/", 1)[-1]], {}
            )
        entries = tuple(b for b in self.books if b["region"] == parameters["region"])
        if path in {"/search", "/db/book"}:
            if "isbn" in parameters:
                entries = tuple(b for b in entries if b["isbn"] == parameters["isbn"])
            page = int(parameters["page"]) - (1 if path == "/db/book" else 0)
            limit = int(parameters["limit"])
            return HttpResponse(200, list(entries[page * limit : (page + 1) * limit]), {})
        selected = next((b for b in entries if b["asin"] == path.rsplit("/", 1)[-1]), None)
        return (
            HttpResponse(200, selected, {})
            if selected
            else HttpResponse(404, {"code": "not_in_libex"}, {})
        )


def regional_sources(http: RegionalHttp) -> tuple[LibexSource, ...]:
    return tuple(
        LibexSource(http, market, historical=history)
        for history in (False, True)
        for market in ("ca", "us", "uk", "au")
    )


class Pages(MockCatalogSource):
    supports_paging = True
    unique_recording_ids = True

    def __init__(
        self,
        key: str,
        pages: tuple[tuple[SourceRecord, ...], ...],
        *,
        family: str = "libex_live",
        market: str | None = None,
        delay: float = 0,
        fail_page: int | None = None,
    ) -> None:
        super().__init__(key, family, tuple(r for page in pages for r in page), marketplace=market)
        self.pages, self.delay, self.fail_page = pages, delay, fail_page
        self.calls: list[int] = []

    def search_page(
        self, query: str, search_type: str, limit: int, timeout: float, *, page: int = 1
    ) -> tuple[SourceRecord, ...]:
        self.calls.append(page)
        if self.delay:
            sleep(self.delay)
        if self.fail_page == page:
            raise SourceError("Isolated page failure")
        return self.pages[page - 1][:limit] if page <= len(self.pages) else ()


def podcast(key: str, index: int) -> SourceRecord:
    original = record(key)
    return replace(
        original,
        reference=replace(original.reference, record_id=f"episode-{index}"),
        edition=replace(
            original.edition,
            asin=None,
            isbn_10=None,
            isbn_13=None,
            title=f"The Lantern Archive discussion episode {index}",
            publisher=None,
            narrators=(),
        ),
        facts=(("bookFormat", '"original_recording"'),),
        asin_verified=False,
    )


class RefinementTests(unittest.TestCase):
    def test_culling_trials_eight_regional_records_are_two_distinct_recordings(self) -> None:
        from shelfmark.core.edition_ownership import ownership_evidence

        http = RegionalHttp()
        http.books = culling_books()
        sources = regional_sources(http)
        backend = MultiSourceBackend(sources)
        found = backend.search("The Culling Trials: Book 1")
        self.assertEqual(len(found), 2)
        self.assertEqual({e.release_date.year for e in found if e.release_date}, {2020, 2026})
        for edition in found:
            assert isinstance(edition, AggregatedEdition)
            self.assertIsNone(edition.part_number)  # Book 1 is not recording Part 1.
            expected = {str(b["asin"]) for b in http.books if b["isbn"] == edition.isbn_13}
            self.assertEqual(ownership_evidence(edition).asins, expected)
            detail = backend.get(edition.catalog, edition.edition_id)
            assert isinstance(detail, AggregatedEdition)
            self.assertEqual(detail.provider_id, edition.provider_id)
            self.assertEqual(ownership_evidence(detail).asins, expected)
            self.assertEqual(detail.asin, edition.asin)
            self.assertEqual(detail.isbn_13, edition.isbn_13)
        # Previously displayed regional locators still resolve their exact primary.
        for source in sources[:4]:
            for raw in source.search("Culling", "title", 40, 1):
                from shelfmark.metadata_providers.editions.multisource import encode_identity

                detail = backend.get("aggregate", encode_identity(raw.reference))
                assert detail
                self.assertEqual(detail.asin, raw.edition.asin)

    def test_culling_trials_incomplete_or_conflicting_evidence_stays_separate(self) -> None:
        for change in (
            {"skuGroup": "BK_ACX0_999999"},
            {"sku": "BK_ACX0_999999US"},
            {"lengthMinutes": 332},
            {"bookFormat": "abridged"},
            {"narrators": [{"name": "Different Narrator"}]},
            {"publisher": "Other publisher"},
            {"isbn": "9780306406157"},
            {"imageUrl": None},
        ):
            http = RegionalHttp()
            http.books = tuple(
                dict(b, **change) if b["asin"] == "B0848Q9P8W" else b for b in culling_books()
            )
            with self.subTest(change=change):
                self.assertEqual(
                    len(MultiSourceBackend(regional_sources(http)).search("Culling")), 3
                )

    def test_four_regions_three_parts_preserve_all_asins_and_snapshot_evidence(self) -> None:
        http = RegionalHttp()
        sources = regional_sources(http)
        found = MultiSourceBackend(sources).search("The Lantern Archive", limit=10)
        self.assertEqual({e.part_number for e in found}, {1, 2, 3})
        self.assertEqual(len(found), 3)
        for edition in found:
            assert isinstance(edition, AggregatedEdition)
            self.assertEqual(edition.provenance.primary.marketplace, "ca")
            self.assertEqual(len(edition.provenance.references), 8)
            self.assertEqual(len({ref.record_id for ref in edition.provenance.references}), 4)
            snapshot = EditionSnapshot(
                EditionIdentity("edition_metadata", edition.provider_id), edition
            )
            self.assertEqual(EditionSnapshot.from_json(snapshot.to_json()), snapshot)
            self.assertTrue(
                any(v.field == "identity.regional_evidence" for v in edition.provenance.values)
            )
            detail = MultiSourceBackend(sources).get(edition.catalog, edition.edition_id)
            assert isinstance(detail, AggregatedEdition)
            self.assertEqual(
                (detail.edition_id, detail.asin, detail.title, detail.part_number),
                (edition.edition_id, edition.asin, edition.title, edition.part_number),
            )
            self.assertEqual(
                {ref.record_id for ref in detail.provenance.references},
                {ref.record_id for ref in edition.provenance.references},
            )

    def test_two_parts_and_every_original_regional_identity_resolve_independently(self) -> None:
        sources = regional_sources(RegionalHttp(2))
        grouped = MultiSourceBackend(sources).search("Lantern")
        ungrouped = [
            edition
            for market in ("ca", "us", "uk", "au")
            for edition in MultiSourceBackend(
                tuple(s for s in sources if s.marketplace == market)
            ).search("Lantern")
        ]
        self.assertEqual(len(grouped), 2)
        self.assertEqual(len(ungrouped), 8)
        for edition in ungrouped:
            detail = MultiSourceBackend(sources).get(edition.catalog, edition.edition_id)
            assert detail is not None
            self.assertEqual(detail.edition_id, edition.edition_id)
            self.assertEqual(detail.asin, edition.asin)
        changed = MultiSourceBackend(
            sources, AggregationOptions(marketplaces=("uk", "au", "ca", "us"))
        )
        self.assertEqual(decode_identity(changed.search("Lantern")[0].edition_id).marketplace, "uk")
        old = changed.get(grouped[0].catalog, grouped[0].edition_id)
        assert old is not None
        self.assertEqual(old.edition_id, grouped[0].edition_id)

    def test_ordinary_asin_veto_is_unchanged_and_isbn_alone_is_insufficient(self) -> None:
        http = RegionalHttp()
        ca = LibexSource(http, "ca").search("Lantern", "title", 40, 1)[0]
        us = LibexSource(http, "us").search("Lantern", "title", 40, 1)[0]
        self.assertFalse(equivalent(ca, us))
        self.assertTrue(regional_equivalent(ca, us))
        self.assertFalse(regional_equivalent(ca, replace(us, regional_evidence=None)))
        self.assertFalse(
            regional_equivalent(ca, replace(us, reference=replace(us.reference, marketplace="ca")))
        )

    def test_missing_or_conflicting_regional_corroborators_veto_grouping(self) -> None:
        http = RegionalHttp()
        ca = LibexSource(http, "ca").search("Lantern", "title", 40, 1)[0]
        us = LibexSource(http, "us").search("Lantern", "title", 40, 1)[0]
        variants = (
            replace(us, edition=replace(us.edition, isbn_13=None)),
            replace(us, edition=replace(us.edition, isbn_13="9780000000026")),
            replace(us, edition=replace(us.edition, narrators=("Different cast",))),
            replace(us, edition=replace(us.edition, authors=("Different author",))),
            replace(us, edition=replace(us.edition, publisher="Other publisher")),
            replace(us, edition=replace(us.edition, language=None)),
            replace(us, edition=replace(us.edition, language="French")),
            replace(us, edition=replace(us.edition, release_date=date(2025, 2, 1))),
            replace(us, edition=replace(us.edition, cover_url=None)),
            replace(us, edition=replace(us.edition, part_number=2)),
            replace(us, edition=replace(us.edition, part_total=2)),
            replace(us, edition=replace(us.edition, series_position=2)),
            replace(us, edition=replace(us.edition, title="Another Literary Work (1 of 3)")),
            replace(us, production=None),
            replace(us, asin_verified=False),
            replace(us, edition=replace(us.edition, asin=None)),
            replace(us, edition=replace(us.edition, asin="invalid")),
            replace(
                us,
                facts=tuple(
                    (k, json.dumps(999)) if k == "lengthMinutes" else (k, v) for k, v in us.facts
                ),
            ),
            replace(
                us,
                facts=tuple(
                    (k, '"BK_GRPH_999999"') if k == "skuGroup" else (k, v) for k, v in us.facts
                ),
            ),
        )
        for variant in variants:
            with self.subTest(variant=variant):
                self.assertFalse(regional_equivalent(ca, variant))

    def test_conventional_recording_has_no_regional_exception(self) -> None:
        http = RegionalHttp()
        http.books = tuple(dict(b, title="The Lantern Archive") for b in http.books)
        found = MultiSourceBackend(regional_sources(http)).search("Lantern")
        self.assertEqual(len(found), 12)  # Distinct regional ASINs are conservatively retained.

    def test_relevance_recovers_parts_from_second_page_before_final_cap(self) -> None:
        source = Pages(
            "live",
            (tuple(podcast("live", n) for n in range(40)), (record("live"), record("live", 2))),
        )
        found = MultiSourceBackend((source,)).search("The Lantern Archive", limit=2)
        self.assertEqual(source.calls, [1, 2])
        self.assertEqual({e.part_number for e in found}, {1, 2})
        self.assertTrue(all(e.asin for e in found))

    def test_exact_and_near_titles_rank_without_dropping_conventional_recordings(self) -> None:
        first = record("a")
        conventional = replace(
            record("a", 2),
            reference=replace(first.reference, record_id="CONVENT001"),
            edition=replace(
                first.edition,
                edition_id="CONVENT001",
                asin="CONVENT001",
                title="The Lantern Archive",
                part_number=None,
                part_total=None,
                narrators=("Solo Narrator",),
            ),
            production=None,
        )
        near = replace(
            record("a", 2), edition=replace(record("a", 2).edition, title="The Lantern Archives")
        )
        loose = replace(
            record("a"),
            reference=replace(first.reference, record_id="loose"),
            edition=replace(first.edition, asin="LOOSETITLE", title="A History of Lanterns"),
        )
        source = Pages("a", ((loose, podcast("a", 1), near, conventional, first),))
        found = MultiSourceBackend((source,)).search("The Lantern Archive")
        self.assertEqual(
            {e.asin for e in found[:3]},
            {first.edition.asin, conventional.edition.asin, near.edition.asin},
        )
        self.assertIn("CONVENT001", {e.asin for e in found})

    def test_requested_part_and_english_preference_do_not_depend_on_region(self) -> None:
        french = replace(
            record("ca", region="ca"),
            edition=replace(record("ca").edition, language="French", asin="FRENCH0001"),
        )
        sources = (
            Pages("ca", ((french,),), market="ca"),
            Pages("us", ((record("us", region="us"), record("us", 2, region="us")),), market="us"),
        )
        found = MultiSourceBackend(sources).search("The Lantern Archive Part 2")
        self.assertEqual(found[0].part_number, 2)
        self.assertEqual(found[0].language, "en")
        self.assertEqual(found[-1].language, "French")

    def test_sparse_exact_recording_is_not_ranked_below_unrelated_complete_books(self) -> None:
        exact = replace(
            record("a"),
            edition=replace(
                record("a").edition,
                asin=None,
                isbn_10=None,
                isbn_13=None,
                publisher=None,
                narrators=(),
            ),
            asin_verified=False,
        )
        unrelated = replace(
            record("a", 2), edition=replace(record("a", 2).edition, title="Another Complete Book")
        )
        source = Pages("a", ((unrelated, exact),))
        found = MultiSourceBackend((source,)).search("The Lantern Archive", limit=1)
        self.assertEqual(found[0].title, exact.edition.title)

    def test_slow_history_does_not_starve_live_pagination_or_fallback(self) -> None:
        episode = replace(
            podcast("ca", 0), reference=replace(podcast("ca", 0).reference, marketplace="ca")
        )
        ca = Pages("ca", ((episode,), (record("ca", region="ca"),)), market="ca")
        us = Pages("us", ((),), market="us")
        uk = Pages("uk", ((record("uk", 2, region="uk"),),), market="uk")
        history = Pages("history", ((),), family="libex_history", delay=0.15)
        started = monotonic()
        found = MultiSourceBackend(
            (history, uk, us, ca), AggregationOptions(workers=2, timeout=0.05, window=1)
        ).search("Lantern")
        self.assertLess(monotonic() - started, 0.12)
        self.assertEqual(ca.calls, [1, 2])
        self.assertEqual(
            uk.calls, [1, 2]
        )  # The saturated one-record page gets one bounded follow-up.
        self.assertTrue(found)
        self.assertEqual(found[0].part_number, 1)
        assert isinstance(found[0], AggregatedEdition)
        self.assertTrue(any("history" in warning for warning in found[0].provenance.warnings))

    def test_failed_second_page_preserves_first_page_and_reports_partial(self) -> None:
        source = Pages("a", ((record("a"),),), fail_page=2)
        found = MultiSourceBackend((source,), AggregationOptions(window=1)).search("Lantern")
        self.assertEqual(len(found), 1)
        assert isinstance(found[0], AggregatedEdition)
        self.assertTrue(any("page 2: SourceError" in w for w in found[0].provenance.warnings))
        self.assertEqual(source.calls, [1, 2])

    def test_request_cap_and_page_cap_are_visible_and_never_retry(self) -> None:
        a, b = Pages("a", ((record("a"),),)), Pages("b", ((record("b", 2),),))
        found = MultiSourceBackend((a, b), AggregationOptions(max_search_calls=1)).search("Lantern")
        self.assertEqual(a.calls, [1])
        self.assertEqual(b.calls, [])
        assert isinstance(found[0], AggregatedEdition)
        self.assertTrue(any("skipped" in w for w in found[0].provenance.warnings))
        found = MultiSourceBackend((a,), AggregationOptions(window=1, max_pages=1)).search(
            "Lantern"
        )
        assert isinstance(found[0], AggregatedEdition)
        self.assertTrue(any("additional pages not read" in w for w in found[0].provenance.warnings))

    def test_repeated_pages_never_duplicate_results_or_appear_complete(self) -> None:
        source = Pages("a", ((record("a"),), (record("a"),)))
        found = MultiSourceBackend((source,), AggregationOptions(window=1, max_pages=3)).search(
            "Lantern"
        )
        self.assertEqual(len(found), 1)
        self.assertEqual(source.calls, [1, 2])
        assert isinstance(found[0], AggregatedEdition)
        self.assertTrue(any("repeated page identifiers" in w for w in found[0].provenance.warnings))

    def test_source_page_limit_below_aggregate_window_still_paginates(self) -> None:
        source = Pages("a", ((podcast("a", 1),), (record("a"),)))
        source.page_limit = 1
        found = MultiSourceBackend((source,)).search("Lantern", limit=1)
        self.assertEqual(source.calls, [1, 2])
        self.assertEqual(found[0].asin, record("a").edition.asin)

    def test_targeted_asin_discovers_historical_record_without_title_search(self) -> None:
        historical = replace(
            record("history"),
            reference=replace(record("history").reference, record_id="B000000001"),
            edition=replace(record("history").edition, asin="B000000001"),
        )
        source = Pages("history", ((historical,),), family="libex_history")
        with patch.object(
            source, "search_page", side_effect=AssertionError("No title reconstruction")
        ):
            found = MultiSourceBackend((source,)).search("B000000001")
        self.assertEqual(found[0].asin, "B000000001")

    def test_regional_detail_enrichment_uses_recording_sku_without_title_inference(self) -> None:
        http = RegionalHttp(2)
        first = LibexSource(http, "ca").search("Lantern", "title", 40, 1)[0]
        http.calls.clear()
        results = LibexSource(http, "uk", historical=True).lookup(first, 1)
        self.assertEqual(results[0].edition.part_number, 1)
        self.assertEqual(http.calls[0], ("/db/book/sku/BK_GRPH_000001", {}))
        self.assertNotIn("title", http.calls[0][1])

    def test_exact_recording_isbn_resolves_one_grouped_part(self) -> None:
        backend = MultiSourceBackend(regional_sources(RegionalHttp(2)))
        edition = backend.by_isbn("9780000000026")
        assert isinstance(edition, AggregatedEdition)
        self.assertEqual(edition.part_number, 2)
        self.assertEqual(
            {ref.marketplace for ref in edition.provenance.references}, {"ca", "us", "uk", "au"}
        )

    def test_paging_mapping_and_safe_configuration_bounds(self) -> None:
        http = RegionalHttp(2)
        LibexSource(http, "ca").search_page("Lantern", "title", 1, 1, page=2)
        self.assertEqual(http.calls[-1][1]["page"], "1")
        LibexSource(http, "ca", historical=True).search_page("Lantern", "title", 1, 1, page=2)
        self.assertEqual(http.calls[-1][1]["page"], "2")
        for settings in (
            {"EDITION_MAX_PAGES": 4},
            {"EDITION_SEARCH_CALL_LIMIT": 49},
            {"EDITION_MAX_PAGES": 1.5},
        ):
            with self.assertRaises(ValueError):
                MultiSourceSettings.from_mapping(settings)
        self.assertFalse(MultiSourceSettings.from_mapping({}).backend().is_available())


if __name__ == "__main__":
    unittest.main()

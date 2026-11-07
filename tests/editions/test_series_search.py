"""Offline series/ranking regressions with factual relationships and synthetic IDs."""

import unittest
from dataclasses import asdict, replace
from unittest.mock import patch

from test_live_sources import AudHttp, DBHttp
from test_search_refinement import RegionalHttp, regional_sources

from shelfmark.metadata_providers import MetadataSearchOptions, SortOrder
from shelfmark.metadata_providers.editions.audiobookdb import AudiobookDBSource
from shelfmark.metadata_providers.editions.audnexus import AudnexusSource
from shelfmark.metadata_providers.editions.catalog_types import AggregatedEdition
from shelfmark.metadata_providers.editions.multisource import MultiSourceBackend
from shelfmark.metadata_providers.editions.presentation import work_presentation
from shelfmark.metadata_providers.editions.provider import EditionMetadataProvider, _to_shelfmark


def books() -> tuple[dict[str, object], ...]:
    result: list[dict[str, object]] = []
    for market in ("ca", "us", "uk", "au"):
        for number, title, position, narrator in (
            (1, "Harry Potter and the Goblet of Fire", 4, "Narrator A"),
            (2, "Harry Potter and the Goblet of Fire (Narrated by Narrator B)", 4, "Narrator B"),
            (3, "Harry Potter and the Half-Blood Prince", 6, "Narrator A"),
            (4, "Harry Potter - the Ultimate Quiz", None, "Quiz Narrator"),
            (5, "Harry Potter: A History of Magic", None, "Natalie Dormer"),
        ):
            group = f"BK_TEST_{number:06d}"
            result.append(
                {
                    "asin": f"B0{market.upper()}0000{number:02d}",
                    "region": market,
                    "title": title,
                    "authors": [{"name": "J.K. Rowling" if position else "Companion Author"}],
                    "narrators": [{"name": narrator}],
                    "isbn": "9780306406157",
                    "publisher": "Fixture Publisher",
                    "language": "english",
                    "releaseDate": "2020-01-01",
                    "imageUrl": f"https://example.invalid/{number}.jpg",
                    "lengthMinutes": 699,
                    "bookFormat": "unabridged",
                    "skuGroup": group,
                    "sku": group + (market.upper() if market != "us" else ""),
                    "series": [
                        {
                            "asin": "B0SERIES01",
                            "name": "Harry Potter"
                            if position
                            else (
                                "Wizarding World"
                                if market == "ca"
                                else "Wizarding World Collection"
                            ),
                            "position": str(position) if position else "",
                        }
                    ],
                }
            )
    return tuple(result)


class SeriesSearchTests(unittest.TestCase):
    def test_unidentified_catalog_listings_survive_batch_and_page_order(self) -> None:
        backend = MultiSourceBackend((*self.backend().sources, AudiobookDBSource(DBHttp([]))))
        template = backend.sources[0].search("Harry Potter", "title", 40, 1)[0]
        regional, unidentified = [], []
        for position in (1, 3, 10):
            asin = f"B0TEST{position:04d}"
            record = replace(
                template,
                reference=replace(template.reference, record_id=asin),
                edition=replace(
                    template.edition,
                    asin=asin,
                    isbn_10=None,
                    isbn_13=None,
                    title=f"Fixture Cycle {position}",
                    series_name="Fixture Cycle",
                    series_position=position,
                ),
                facts=(),
                links=(),
            )
            regional.append(record)
            unidentified.append(
                replace(
                    record,
                    reference=replace(
                        record.reference,
                        source="audiobookdb",
                        marketplace=None,
                        record_id=f"release-{position}",
                    ),
                    edition=replace(
                        record.edition, asin=None, parent_work_id=f"audiobookdb:work-{position}"
                    ),
                )
            )
        expected = None
        for reverse in (False, True):
            batches = {"libex_live_ca": tuple(regional), "audiobookdb": tuple(unidentified)}
            if reverse:
                batches = {
                    key: tuple(reversed(rows)) for key, rows in reversed(tuple(batches.items()))
                }
            with patch.object(backend, "_discover", return_value=(batches, (), (), 0)):
                result = backend.search("Fixture Cycle")
                self.assertEqual([row.series_position for row in result], [1, 1, 3, 3, 10, 10])
                self.assertEqual(sum(row.asin is None for row in result), 3)
                identities = [row.provider_id for row in result]
                self.assertEqual(len(set(identities)), 6)
                if expected is not None:
                    self.assertEqual(identities, expected)
                expected = identities
                # Request completed pages out of order; each retains its ranked slice.
                for page in (3, 1, 2):
                    self.assertEqual(
                        [
                            row.provider_id
                            for row in backend.search("Fixture Cycle", limit=2, page=page)
                        ],
                        identities[(page - 1) * 2 : page * 2],
                    )

    def backend(self) -> MultiSourceBackend:
        http = RegionalHttp()
        http.books = books()
        return MultiSourceBackend(regional_sources(http))

    def test_broad_series_then_specific_title_and_no_cover_penalty(self) -> None:
        backend = self.backend()
        broad = backend.search("Harry Potter")
        self.assertEqual([e.series_position for e in broad[:3]], [4, 4, 6])
        self.assertEqual(len(broad), 5)
        exact = backend.search("Harry Potter and the Half-Blood Prince")
        self.assertEqual(exact[0].series_position, 6)
        from shelfmark.metadata_providers.editions.search_ranking import relevance

        record = backend.sources[0].search("Harry Potter", "title", 40, 1)[0]
        self.assertEqual(
            relevance(record, "Harry Potter", "general"),
            relevance(
                replace(record, edition=replace(record.edition, cover_url=None)),
                "Harry Potter",
                "general",
            ),
        )

    def test_native_series_capability_filters_and_orders_existing_results(self) -> None:
        provider = EditionMetadataProvider(self.backend())
        self.assertEqual(provider.capabilities[0].key, "view_series")
        self.assertEqual(provider.search_fields[0].key, "series")
        found = provider.search(
            MetadataSearchOptions(
                query="", fields={"series": "Harry Potter"}, sort=SortOrder.SERIES_ORDER
            )
        )
        self.assertEqual([e.series_position for e in found], [4, 4, 6])
        self.assertTrue(all(e.provider == "edition_metadata" for e in found))
        # Other source adapters accept the same bounded series query; no new API.
        self.assertTrue(AudiobookDBSource(DBHttp()).search("Lantern", "series", 10, 2))
        self.assertTrue(AudnexusSource(AudHttp(), "ca").search("Lantern", "series", 10, 2))

    def test_history_magic_umbrella_order_does_not_split_verified_recording(self) -> None:
        found = [e for e in self.backend().search("Harry Potter") if "History of Magic" in e.title]
        self.assertEqual(len(found), 1)
        edition = found[0]
        assert isinstance(edition, AggregatedEdition)
        self.assertEqual(len({r.record_id for r in edition.provenance.references}), 4)
        self.assertIsNone(edition.isbn_13)  # Generic SKU grouping does not attest Audible ISBNs.

    def test_display_work_groups_require_explicit_evidence_keep_children(self) -> None:
        found = self.backend().search("Harry Potter")
        first, second = found[:2]
        self.assertNotEqual(first.provider_id, second.provider_id)
        self.assertEqual(work_presentation(first)[0], work_presentation(second)[0])
        self.assertNotEqual(work_presentation(first)[0], work_presentation(found[2])[0])
        assert isinstance(first, AggregatedEdition)
        without = replace(
            first,
            provenance=replace(
                first.provenance,
                values=tuple(v for v in first.provenance.values if v.field != "source.series"),
            ),
        )
        self.assertIsNone(work_presentation(without)[0])
        self.assertEqual(
            work_presentation(replace(without, parent_work_id="audiobookdb:work-1"))[0],
            "audiobookdb:work-1",
        )
        ui = _to_shelfmark(first)
        self.assertEqual(ui.provider_id, first.provider_id)
        self.assertIn("edition_work_id", asdict(ui))
        conflict = replace(
            first,
            parent_work_id="audiobookdb:work-1",
            provenance=replace(first.provenance, conflicts=("parent_work_id",)),
        )
        self.assertIsNone(work_presentation(conflict)[0])
        self.assertNotIn("_edition_ownership", asdict(ui))

    def test_series_navigation_uses_literary_names_for_parts_and_other_series(self) -> None:
        from test_search_refinement import culling_books

        for name in ("Red Rising", "Shadowspell Academy"):
            http = RegionalHttp()
            http.books = tuple(
                dict(row, series=[{"asin": "B0SERIES01", "name": name, "position": "1"}])
                for row in (http.books if name == "Red Rising" else culling_books())
            )
            backend = MultiSourceBackend(regional_sources(http))
            found = backend.search("", fields={"series": name})
            self.assertTrue(found)
            self.assertTrue(all(e.series_name == name for e in found))
            if name == "Red Rising":
                self.assertEqual({e.part_number for e in found}, {1, 2, 3})
                self.assertEqual(len({work_presentation(e)[0] for e in found}), 1)

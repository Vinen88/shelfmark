"""Display-only work ownership; fictional recordings, no external services."""

import unittest
from dataclasses import asdict, replace
from unittest.mock import patch

from mock_backend import MOCK_EDITIONS
from test_ownership import FixtureLibrary, abs_item
from test_search_refinement import RegionalHttp, regional_sources

from shelfmark.core import library_index
from shelfmark.core.edition_ownership import work_in_entries
from shelfmark.core.library_providers import LibraryEntry, audiobookshelf
from shelfmark.metadata_providers import MetadataSearchOptions
from shelfmark.metadata_providers.editions.multisource import MultiSourceBackend
from shelfmark.metadata_providers.editions.provider import EditionMetadataProvider, _to_shelfmark


def primal_book(**changes: object):
    # Reported literary discrepancy, synthetic recording IDs/cast/publisher.
    edition = replace(
        MOCK_EDITIONS[0],
        title="The Primal Hunter, Book 1",
        canonical_title="The Primal Hunter",
        authors=("Zogarth",),
        series_name="The Primal Hunter",
        series_position=1,
        asin="B0CATALOG1",
        narrators=("Fixture Narrator",),
        edition_label=None,
        part_number=None,
        part_total=None,
        parent_work_id="audiobookdb:fixture-work-1",
    )
    return _to_shelfmark(replace(edition, **changes))


def primal_entry(**changes: object) -> LibraryEntry:
    meta: dict[str, object] = {
        "title": "The Primal Hunter, Book 1",
        "authorName": "Zogarth",
        "series": [{"name": "The Primal Hunter", "sequence": "1"}],
        "asin": "B0ABSOWN01",
        "narrators": ["Other Fixture Narrator"],
    }
    meta.update(changes)
    return audiobookshelf._item_to_entry({"media": {"metadata": meta}})


class WorkOwnershipTests(unittest.TestCase):
    def test_primal_hunter_owned_abs_recording_absent_from_catalog(self) -> None:
        book, entry = primal_book(), primal_entry()
        before = asdict(book)
        self.assertIsNone(library_index.match_entries(book, [entry]))
        self.assertTrue(work_in_entries(book, [entry]))
        self.assertEqual(asdict(book), before)

    def test_regional_recording_narrator_and_production_are_not_work_identity(self) -> None:
        for changes in (
            {"asin": "B0REGION01", "narrators": ["Fixture Narrator"]},
            {"title": "The Primal Hunter, Book 1 [Dramatized Adaptation] (Part 1 of 3)"},
            {"narrators": ["Another Narrator"]},
            {"asin": ""},
        ):
            with self.subTest(changes=changes):
                entry = primal_entry(**changes)
                self.assertTrue(work_in_entries(primal_book(), [entry]))
                self.assertIsNone(library_index.match_entries(primal_book(), [entry]))

    def test_insufficient_or_conflicting_literary_evidence(self) -> None:
        for changes in (
            {"authorName": ""},
            {"authorName": "Another Zogarth"},
            {"series": []},
            {"series": [{"name": "The Primal Hunter", "sequence": "2"}]},
            {"series": [{"name": "Different Series", "sequence": "1"}]},
            {"title": "Another Book"},
            {"title": "The Primal Hunter, Book 2"},
        ):
            with self.subTest(changes=changes):
                self.assertFalse(work_in_entries(primal_book(), [primal_entry(**changes)]))
        self.assertFalse(work_in_entries(primal_book(), []))
        self.assertFalse(
            work_in_entries(primal_book(), [LibraryEntry(frozenset(), frozenset(), frozenset())])
        )

    def test_native_flattened_abs_fields_and_full_author_names(self) -> None:
        entry = primal_entry(series=None, seriesName="The Primal Hunter #1")
        self.assertTrue(work_in_entries(primal_book(), [entry]))
        entry = primal_entry(authorName="", authors=[{"name": "Zogarth"}])
        self.assertTrue(work_in_entries(primal_book(), [entry]))
        entry = primal_entry(
            title="The Primal Hunter 01: The Primal Hunter", seriesName="The Primal Hunter #1"
        )
        self.assertTrue(work_in_entries(primal_book(), [entry]))

    def test_standalone_title_and_author_without_series(self) -> None:
        book = primal_book(
            title="A Standalone Novel",
            canonical_title="A Standalone Novel",
            series_name=None,
            series_position=None,
        )
        self.assertTrue(
            work_in_entries(book, [primal_entry(title="A Standalone Novel", series=[])])
        )
        self.assertFalse(
            work_in_entries(
                book,
                [primal_entry(title="A Standalone Novel", series=[], authorName="Another Author")],
            )
        )

    def test_only_shared_real_work_ids_qualify(self) -> None:
        native = LibraryEntry(
            frozenset(), frozenset(), frozenset(), frozenset({("audiobookdb", "fixture-work-1")})
        )
        self.assertTrue(work_in_entries(primal_book(), [native]))
        conflicting = replace(native, external_ids=frozenset({("audiobookdb", "different-work")}))
        self.assertFalse(work_in_entries(primal_book(), [conflicting]))
        book = primal_book()
        book.edition_work_id = "series-position:fixture-work-1"
        self.assertFalse(work_in_entries(book, [native]))

    def test_graphicaudio_parts_stay_independent_when_work_is_owned(self) -> None:
        books = EditionMetadataProvider(
            MultiSourceBackend(regional_sources(RegionalHttp()))
        ).search(MetadataSearchOptions(query="Lantern"))
        entry = audiobookshelf._item_to_entry(abs_item(2, seriesName="The Lantern Cycle #1"))
        self.assertEqual([work_in_entries(book, [entry]) for book in books], [True] * 3)
        self.assertEqual(
            [library_index.match_entries(book, [entry]) for book in books], [None, "owned", None]
        )

    def test_projection_reuses_accessible_native_index_and_leaves_policy_strict(self) -> None:
        book = primal_book()
        library = FixtureLibrary([primal_entry()])
        with (
            patch.object(library_index, "_cache", {}),
            patch.object(library_index, "all_providers", return_value=[library]),
            patch.object(library, "fetch_entries", wraps=library.fetch_entries) as fetch,
        ):
            self.assertEqual(library_index.ownership(book), {"audiobook": None})
            for _ in range(5):
                self.assertTrue(library_index.work_owned(book))
                self.assertFalse(library_index.is_in_library(book, "audiobook", strict=True))
            self.assertEqual(fetch.call_count, 1)
        with patch.object(library_index, "all_providers", return_value=[]):
            self.assertIsNone(library_index.work_owned(book))

    def test_exact_native_ownership_is_also_work_ownership(self) -> None:
        entry = primal_entry(asin="B0CATALOG1", narrators=["Fixture Narrator"])
        self.assertEqual(library_index.match_entries(primal_book(), [entry]), "owned")
        self.assertTrue(work_in_entries(primal_book(), [entry]))

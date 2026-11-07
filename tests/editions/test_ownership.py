"""Ownership checks against the pinned native classes and fictional recordings."""

import unittest
from dataclasses import asdict, replace
from unittest.mock import MagicMock, patch

from mock_backend import MOCK_EDITIONS, MockBackend
from mock_sources import MockCatalogSource
from test_multisource import record
from test_search_refinement import RegionalHttp, regional_book, regional_sources

from shelfmark.core import auto_download, library_index
from shelfmark.core.library_providers import LibraryEntry, audiobookshelf
from shelfmark.core.user_db import UserDB
from shelfmark.metadata_providers import BookMetadata, MetadataSearchOptions
from shelfmark.metadata_providers.editions.multisource import MultiSourceBackend
from shelfmark.metadata_providers.editions.presentation import display_title
from shelfmark.metadata_providers.editions.provider import EditionMetadataProvider


def abs_item(part: int, market: str = "ca", /, **changes: object) -> dict[str, object]:
    raw = regional_book(part, market)
    meta: dict[str, object] = {
        "title": raw["title"],
        "isbn": raw["isbn"],
        "asin": raw["asin"],
        "authorName": "Example Author",
        "narrators": [f"Example Narrator {part}", f"Example Cast {part}"],
        "publisher": raw["publisher"],
        "language": raw["language"],
    }
    meta.update(changes)
    return {"media": {"metadata": meta}, "relPath": "fictional fixture only"}


class FixtureLibrary:
    name = "audiobookshelf"
    display_name = "ABS fixture"
    content_types = frozenset({"audiobook"})

    def __init__(self, entries: list[LibraryEntry]) -> None:
        self.entries = entries
        self.failure = False
        self.revision = 1

    def is_enabled(self) -> bool:
        return True

    def describe(self) -> str:
        return "isolated ABS fixture"

    def fingerprint(self) -> int:
        return self.revision

    def fetch_entries(self) -> list[LibraryEntry]:
        if self.failure:
            raise OSError("fixture library unavailable")
        return self.entries


def no_queue(*args: object, **kwargs: object) -> tuple[bool, str | None]:
    raise AssertionError("A test must never dispatch a download")


class OwnershipTests(unittest.TestCase):
    def setUp(self) -> None:
        empty_cache: dict[str, library_index._CacheSlot] = {}
        self.cache = patch.object(library_index, "_cache", empty_cache)
        self.cache.start()
        self.addCleanup(self.cache.stop)
        self.provider = EditionMetadataProvider(
            MultiSourceBackend(regional_sources(RegionalHttp()))
        )
        self.books = self.provider.search(MetadataSearchOptions(query="Lantern"))

    def holdings(self, entries: list[LibraryEntry]) -> list[bool]:
        library = FixtureLibrary(entries)
        with patch.object(library_index, "all_providers", return_value=[library]):
            answers = []
            for book in self.books:
                badge = library_index.ownership(book)
                self.assertIsNotNone(badge)
                assert badge is not None
                strict = library_index.is_in_library(book, "audiobook", strict=True)
                self.assertEqual(badge["audiobook"] == "owned", strict)
                answers.append(strict)
        return answers

    def test_part_one_owned_other_parts_missing(self) -> None:
        self.assertEqual(
            self.holdings([audiobookshelf._item_to_entry(abs_item(1))]), [True, False, False]
        )

    def test_part_two_owned_other_parts_missing(self) -> None:
        self.assertEqual(
            self.holdings([audiobookshelf._item_to_entry(abs_item(2))]), [False, True, False]
        )

    def test_all_parts_owned(self) -> None:
        self.assertEqual(
            self.holdings([audiobookshelf._item_to_entry(abs_item(p)) for p in (1, 2, 3)]),
            [True] * 3,
        )

    def test_no_parts_owned(self) -> None:
        self.assertEqual(self.holdings([]), [False] * 3)

    def test_every_verified_regional_asin_matches_only_its_part(self) -> None:
        for market in ("ca", "us", "uk", "au"):
            entries = [audiobookshelf._item_to_entry(abs_item(2, market))]
            for index, book in enumerate(self.books, 1):
                self.assertEqual(
                    library_index.match_entries(book, entries), "owned" if index == 2 else None
                )

    def test_recording_isbn_without_asin_requires_full_corroboration(self) -> None:
        entry = audiobookshelf._item_to_entry(abs_item(1, asin=""))
        self.assertEqual(library_index.match_entries(self.books[0], [entry]), "owned")
        other = audiobookshelf._item_to_entry(
            abs_item(2, asin="", isbn=regional_book(1, "ca")["isbn"])
        )
        self.assertIsNone(library_index.match_entries(self.books[0], [other]))

    def test_isbn10_and_legacy_cached_entries_are_handled_conservatively(self) -> None:
        entry = audiobookshelf._item_to_entry(abs_item(1, asin="", isbn="0000000019"))
        self.assertEqual(library_index.match_entries(self.books[0], [entry]), "owned")
        isbn_only = LibraryEntry(frozenset(), frozenset({"9780000000019"}), frozenset())
        self.assertIsNone(library_index.match_entries(self.books[0], [isbn_only]))
        asin_only = LibraryEntry(frozenset(), frozenset(), frozenset({"B0US000001"}))
        self.assertEqual(library_index.match_entries(self.books[0], [asin_only]), "owned")

    def test_original_two_part_mock_backend_still_matches_exact_recording_asins(self) -> None:
        provider = EditionMetadataProvider(MockBackend())
        first, second = provider.search(MetadataSearchOptions(query="Lantern"))
        edition = MOCK_EDITIONS[0]
        meta: dict[str, object] = {
            "title": display_title(edition),
            "asin": edition.asin,
            "isbn": edition.isbn_13,
            "narrators": list(edition.narrators),
            "publisher": edition.publisher,
            "language": edition.language,
        }
        raw = {"media": {"metadata": meta}}
        entry = audiobookshelf._item_to_entry(raw)
        self.assertEqual(library_index.match_entries(first, [entry]), "owned")
        self.assertIsNone(library_index.match_entries(second, [entry]))
        meta["asin"] = ""
        # A raw base-model ISBN has no recording-scope attestation, even when
        # all display metadata matches. It must not become ownership proof.
        self.assertIsNone(library_index.match_entries(first, [audiobookshelf._item_to_entry(raw)]))

    def test_unverified_asin_and_changed_book_identity_do_not_supply_proof(self) -> None:
        unverified = replace(record("unverified"), asin_verified=False)
        provider = EditionMetadataProvider(
            MultiSourceBackend((MockCatalogSource("unverified", "fixture", (unverified,)),))
        )
        book = provider.search(MetadataSearchOptions(query="Lantern"))[0]
        entry = LibraryEntry(frozenset(), frozenset(), frozenset({"MOCKASIN01"}))
        self.assertIsNone(library_index.match_entries(book, [entry]))
        first = self.books[0]
        first.provider_id = self.books[1].provider_id
        self.assertIsNone(
            library_index.match_entries(first, [audiobookshelf._item_to_entry(abs_item(1))])
        )
        first.provider_id = ""
        self.assertIsNone(
            library_index.match_entries(first, [audiobookshelf._item_to_entry(abs_item(1))])
        )

    def test_same_isbn_with_conflicting_narration_is_not_owned(self) -> None:
        for changes in (
            {"asin": "", "narrators": ["Different Solo Narrator"]},
            {"narrators": ["Different Solo Narrator"]},
            {"asin": "", "title": "The Lantern Archive (single narrator)"},
        ):
            entry = audiobookshelf._item_to_entry(abs_item(1, **changes))
            self.assertIsNone(library_index.match_entries(self.books[0], [entry]))

    def test_conflicting_publisher_language_part_and_strong_ids_veto_matching(self) -> None:
        variants: tuple[dict[str, object], ...] = (
            {"publisher": "Another recording publisher"},
            {"language": "French"},
            {"title": "The Lantern Archive (Part 2 of 3) (Dramatized Adaptation)"},
            {"isbn": regional_book(2, "ca")["isbn"]},
            {"asin": regional_book(2, "ca")["asin"]},
            {"asin": "invalid"},
        )
        for changes in variants:
            with self.subTest(changes=changes):
                entry = audiobookshelf._item_to_entry(abs_item(1, **changes))
                self.assertIsNone(library_index.match_entries(self.books[0], [entry]))

    def test_missing_identifiers_and_incomplete_isbn_corroboration_do_not_fall_back(self) -> None:
        variants: tuple[dict[str, object], ...] = (
            {"asin": "", "isbn": ""},
            {"asin": "", "narrators": []},
            {"asin": "", "publisher": None},
            {"asin": "", "language": None},
            {"asin": "", "title": "The Lantern Archive"},
            {"asin": "", "isbn": "9780000000010"},
        )
        for changes in variants:
            with self.subTest(changes=changes):
                self.assertIsNone(
                    library_index.match_entries(
                        self.books[0], [audiobookshelf._item_to_entry(abs_item(1, **changes))]
                    )
                )

    def test_exact_asin_works_with_missing_optional_library_metadata(self) -> None:
        entry = audiobookshelf._item_to_entry(
            abs_item(1, isbn=None, narrators=[], publisher=None, language=None, title="")
        )
        self.assertEqual(library_index.match_entries(self.books[0], [entry]), "owned")

    def test_known_publisher_spellings_do_not_create_false_recording_conflicts(self) -> None:
        for spelling in ("GraphicAudio", "GraphicAudio LLC", "Graphic Audio LLC"):
            entry = audiobookshelf._item_to_entry(abs_item(1, publisher=spelling))
            self.assertEqual(library_index.match_entries(self.books[0], [entry]), "owned")

    def test_partial_cast_is_compatible_with_asin_but_insufficient_for_isbn_only(self) -> None:
        for value, expected in ((regional_book(1, "ca")["asin"], "owned"), ("", None)):
            entry = audiobookshelf._item_to_entry(
                abs_item(1, asin=value, narrators=["Example Narrator 1"])
            )
            self.assertEqual(library_index.match_entries(self.books[0], [entry]), expected)

    def test_unverified_regional_asin_cannot_be_inferred_from_matching_isbn(self) -> None:
        entry = audiobookshelf._item_to_entry(abs_item(1, asin="B099999999"))
        self.assertIsNone(library_index.match_entries(self.books[0], [entry]))

    def test_comparable_provider_ids_work_only_when_library_actually_exposes_them(self) -> None:
        book = self.books[0]
        entry = LibraryEntry(
            frozenset(), frozenset(), frozenset(), frozenset({(book.provider, book.provider_id)})
        )
        self.assertEqual(library_index.match_entries(book, [entry]), "owned")
        other = replace(entry, external_ids=frozenset({(book.provider, self.books[1].provider_id)}))
        self.assertIsNone(library_index.match_entries(book, [other]))
        unrelated_namespace = replace(
            entry, external_ids=frozenset({("audible", book.provider_id)})
        )
        self.assertIsNone(library_index.match_entries(book, [unrelated_namespace]))
        self.assertFalse(audiobookshelf._item_to_entry(abs_item(1)).external_ids)

    def test_serialized_display_fields_and_copied_books_cannot_supply_trusted_proof(self) -> None:
        book = self.books[0]
        entry = audiobookshelf._item_to_entry(abs_item(1))
        raw = asdict(book)
        self.assertNotIn("_edition_ownership", raw)
        self.assertIsNotNone(entry._edition_recording)
        copied = replace(book)
        self.assertIsNone(library_index.match_entries(copied, [entry]))
        body_like = BookMetadata(
            provider=book.provider,
            provider_id=book.provider_id,
            title=book.title,
            isbn_13=book.isbn_13,
            display_fields=book.display_fields,
        )
        self.assertIsNone(library_index.match_entries(body_like, [entry]))

    def test_unavailable_library_does_not_become_a_cached_empty_library(self) -> None:
        library = FixtureLibrary([])
        library.failure = True
        with patch.object(library_index, "all_providers", return_value=[library]):
            self.assertEqual(library_index.ownership(self.books[0]), {"audiobook": None})
            with self.assertRaises(library_index.LibraryUnavailableError):
                library_index.is_in_library(self.books[0], "audiobook", strict=True)
            with self.assertRaises(library_index.LibraryUnavailableError):
                library_index.require_ready("audiobook")
            self.assertIsNone(library_index._cache[library.name].entries)
            self.assertIsNotNone(library_index._cache[library.name].failed_at)

    def test_stale_index_retains_native_readiness_policy_and_part_separation(self) -> None:
        library = FixtureLibrary([audiobookshelf._item_to_entry(abs_item(1))])
        with patch.object(library_index, "all_providers", return_value=[library]):
            self.assertTrue(library_index.is_in_library(self.books[0], "audiobook", strict=True))
            library.failure = True
            library.revision += 1
            self.assertFalse(library_index.is_in_library(self.books[1], "audiobook", strict=True))
            self.assertTrue(library_index.is_in_library(self.books[0], "audiobook", strict=True))
            library_index.require_ready(
                "audiobook"
            )  # Stock policy accepts an existing stale index.
            self.assertIsNotNone(library_index._cache[library.name].failed_at)

    def test_automatic_request_decisions_close_only_the_owned_part(self) -> None:
        library = FixtureLibrary([audiobookshelf._item_to_entry(abs_item(1))])
        with (
            patch.object(library_index, "all_providers", return_value=[library]),
            patch.object(
                auto_download,
                "search_source_releases",
                side_effect=AssertionError("No release searches"),
            ),
        ):
            for stale in (False, True):
                library.failure = stale
                library.revision += 1
                for book, expected in zip(self.books[:2], ("in_library", "no_match"), strict=True):
                    db = MagicMock(spec=UserDB)
                    row = {
                        "id": 1,
                        "user_id": 9,
                        "status": "pending",
                        "book_data": {"provider": book.provider, "provider_id": book.provider_id},
                    }
                    db.get_request.return_value = row
                    result = auto_download.auto_download_request(
                        db,
                        row,
                        sources=[],
                        content_type="audiobook",
                        min_seeders=0,
                        queue_release=no_queue,
                        admin_user_id=1,
                        providers={book.provider: self.provider},
                    )
                    self.assertEqual(result.status, expected)
                    self.assertEqual(db.update_request.call_count, int(expected == "in_library"))

    def test_automatic_request_is_held_when_library_unavailable(self) -> None:
        library = FixtureLibrary([])
        library.failure = True
        db = MagicMock(spec=UserDB)
        book = self.books[0]
        row = {
            "id": 1,
            "user_id": 9,
            "status": "pending",
            "book_data": {"provider": book.provider, "provider_id": book.provider_id},
        }
        db.get_request.return_value = row
        with patch.object(library_index, "all_providers", return_value=[library]):
            result = auto_download.auto_download_request(
                db,
                row,
                sources=[],
                content_type="audiobook",
                min_seeders=0,
                queue_release=no_queue,
                admin_user_id=1,
                providers={book.provider: self.provider},
            )
        self.assertEqual(result.status, "error")
        db.update_request.assert_not_called()


if __name__ == "__main__":
    unittest.main()

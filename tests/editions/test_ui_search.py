"""Presentation/search projections and the observed duplicate generic credit."""

import json
import unittest
from dataclasses import replace
from typing import cast
from unittest.mock import MagicMock, patch

from mock_backend import MOCK_EDITIONS
from test_search_refinement import RegionalHttp

from shelfmark.core.search_plan import build_release_search_plan
from shelfmark.metadata_providers import BookMetadata
from shelfmark.metadata_providers.editions.identity import EditionIdentity
from shelfmark.metadata_providers.editions.libex import LibexSource
from shelfmark.metadata_providers.editions.multisource import MultiSourceBackend
from shelfmark.metadata_providers.editions.presentation import release_search_query
from shelfmark.metadata_providers.editions.provider import _to_shelfmark
from shelfmark.metadata_providers.editions.snapshot import EditionSnapshot
from shelfmark.release_sources.prowlarr.source import ProwlarrSource


class UiSearchTests(unittest.TestCase):
    def test_catalog_spelling_projects_to_release_hint_without_identity_mutation(self) -> None:
        edition = replace(
            MOCK_EDITIONS[0],
            title="Dark Age (1 of 3) [Dramatized Adaptation]",
            part_number=1,
            part_total=3,
            search_title=None,
        )
        snapshot = EditionSnapshot(
            EditionIdentity("edition_metadata", edition.provider_id), edition
        )
        before = snapshot.to_json()
        book = _to_shelfmark(edition)
        self.assertEqual(book.search_title, "Dark Age Part 1")
        self.assertEqual(book.title, snapshot.edition_title)
        self.assertEqual(book.provider_id, edition.provider_id)
        self.assertEqual(book.isbn_13, edition.isbn_13)
        self.assertEqual(snapshot.to_json(), before)
        self.assertNotIn("Catalog", {f.label for f in book.display_fields})
        self.assertNotIn("Metadata incomplete", {f.label for f in book.display_fields})

    def test_manual_override_and_native_non_extension_plan(self) -> None:
        with patch("shelfmark.core.search_plan._normalize_languages", return_value=["en"]):
            book = _to_shelfmark(MOCK_EDITIONS[0])
            plan = build_release_search_plan(
                book, manual_query="  Dark Age GraphicAudio  ", indexers=["chosen"]
            )
            self.assertEqual(plan.title_variants[0].title, "Dark Age GraphicAudio")
            self.assertEqual(plan.indexers, ["chosen"])
            self.assertEqual(len(plan.title_variants), 1)
            native = BookMetadata(
                provider="openlibrary", provider_id="one", title="Native (1 of 3)"
            )
            self.assertEqual(
                build_release_search_plan(native).title_variants[0].title, native.title
            )

    def test_native_prowlarr_normal_expanded_and_manual_categories(self) -> None:
        client = MagicMock()
        client.indexer_timeout = 10
        client.get_enabled_indexers_detailed.return_value = [{"id": 7, "name": "chosen"}]
        client.get_enriched_indexer_ids.return_value = []
        client.get_disabled_indexers.return_value = {}
        client.torznab_search.return_value = []
        source = ProwlarrSource()
        book = _to_shelfmark(
            replace(
                MOCK_EDITIONS[0],
                title="Dark Age (1 of 3) [Dramatized Adaptation]",
                search_title=None,
            )
        )
        with (
            patch.object(source, "_get_client", return_value=client),
            patch.object(source, "_get_selected_indexer_ids", return_value=[7]),
            patch("shelfmark.release_sources.prowlarr.source.config") as config,
            patch("shelfmark.core.search_plan._normalize_languages", return_value=None),
        ):
            config.get.side_effect = lambda key, default=None: default
            source.search(book, build_release_search_plan(book), content_type="audiobook")
            source.search(
                book, build_release_search_plan(book), expand_search=True, content_type="audiobook"
            )
            source.search(
                book,
                build_release_search_plan(book, manual_query="Dark Age"),
                content_type="audiobook",
            )
        calls = client.torznab_search.call_args_list
        self.assertEqual(
            [c.kwargs["query"] for c in calls], ["Dark Age Part 1", "Dark Age Part 1", "Dark Age"]
        )
        self.assertEqual([c.kwargs["categories"] for c in calls], [[3030], None, [3030]])
        self.assertTrue(all(c.kwargs["indexer_id"] == 7 for c in calls))

    def test_generic_credit_deduplicates_with_verified_evidence_and_preserves_raw_provenance(
        self,
    ) -> None:
        http = RegionalHttp()
        for raw in http.books:
            names = list(cast("list[dict[str, str]]", raw["narrators"]))
            names.append({"name": "Full Cast" if raw["region"] == "ca" else "Full Cast Full Cast"})
            raw["narrators"] = names
        backend = MultiSourceBackend(tuple(LibexSource(http, m) for m in ("ca", "us")))
        results = backend.search("Lantern")
        self.assertEqual(len(results), 3)
        snapshot = EditionSnapshot(
            EditionIdentity("edition_metadata", results[2].provider_id), results[2]
        )
        self.assertIn("Full Cast Full Cast", snapshot.to_json())
        self.assertEqual(len(json.loads(snapshot.to_json())["aggregation"]["references"]), 2)
        for raw in http.books:
            if raw["region"] == "us":
                raw["narrators"] = [
                    *cast("list[dict[str, str]]", raw["narrators"]),
                    {"name": "Different Named Actor"},
                ]
        self.assertEqual(len(backend.search("Lantern")), 6)

    def test_release_query_retains_part_and_custom_title(self) -> None:
        for number in (1, 2):
            edition = replace(
                MOCK_EDITIONS[0], search_title="Lantern Release Hint", part_number=number
            )
            self.assertEqual(release_search_query(edition), f"Lantern Release Hint Part {number}")

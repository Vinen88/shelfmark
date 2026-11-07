"""Strictly typed regressions for the provider-neutral identity foundation."""

import unittest
from dataclasses import fields, replace
from datetime import date

from mock_backend import MOCK_EDITIONS

from shelfmark.metadata_providers.editions.identity import EditionIdentity
from shelfmark.metadata_providers.editions.models import AudiobookEdition
from shelfmark.metadata_providers.editions.presentation import (
    canonical_title,
    display_title,
    search_query,
)
from shelfmark.metadata_providers.editions.snapshot import (
    EditionSnapshot,
    validate_unique_identities,
)


def snapshot_for(edition: AudiobookEdition) -> EditionSnapshot:
    return EditionSnapshot(EditionIdentity("edition_metadata", edition.provider_id), edition)


class IdentityTests(unittest.TestCase):
    def test_primary_identity_uses_both_components(self) -> None:
        self.assertNotEqual(EditionIdentity("a", "x"), EditionIdentity("b", "x"))
        self.assertNotEqual(EditionIdentity("a:b", "c"), EditionIdentity("a", "b:c"))
        self.assertNotEqual(EditionIdentity("a:b", "c").key, EditionIdentity("a", "b:c").key)
        self.assertEqual(len({EditionIdentity("a", "x"), EditionIdentity("a", "x")}), 1)

    def test_empty_identity_is_invalid(self) -> None:
        for provider, provider_id in (("", "x"), ("a", " "), ("\t", "x")):
            with self.assertRaises(ValueError):
                EditionIdentity(provider, provider_id)

    def test_same_title_identifiers_do_not_define_identity(self) -> None:
        first, second = MOCK_EDITIONS
        self.assertEqual(canonical_title(first), canonical_title(second))
        self.assertNotEqual(first.isbn_10, second.isbn_10)
        self.assertNotEqual(first.isbn_13, second.isbn_13)
        self.assertNotEqual(first.asin, second.asin)
        snapshots = (snapshot_for(first), snapshot_for(second))
        validate_unique_identities(snapshots)
        self.assertNotEqual(snapshots[0].identity, snapshots[1].identity)
        renamed = replace(first, title="New title", asin=None, isbn_10=None, isbn_13=None)
        self.assertEqual(snapshot_for(renamed).identity, snapshots[0].identity)

    def test_duplicate_identity_is_not_silently_overwritten(self) -> None:
        first = snapshot_for(MOCK_EDITIONS[0])
        conflicting = EditionSnapshot(first.identity, MOCK_EDITIONS[1])
        for other in (first, conflicting):
            with self.assertRaisesRegex(ValueError, "Duplicate edition identity"):
                validate_unique_identities((first, other))


class SnapshotTests(unittest.TestCase):
    def test_roundtrip_every_model_field(self) -> None:
        # Include fields not supplied by the mock, Unicode, and a fractional series position.
        edition = replace(
            MOCK_EDITIONS[0],
            title="Édition spéciale",
            publish_year=2024,
            source_url="https://example.invalid/édition",
            language="fr",
            series_position=1.5,
            authors=("Author One", "Author Two"),
            release_date=date(2025, 2, 3),
        )
        snapshot = snapshot_for(edition)
        self.assertEqual(set(snapshot.to_dict()["edition"]), {f.name for f in fields(edition)})
        restored = EditionSnapshot.from_json(snapshot.to_json())
        self.assertEqual(restored, snapshot)
        self.assertEqual(restored.edition, edition)
        self.assertEqual(restored.to_json(), snapshot.to_json())
        self.assertEqual(EditionSnapshot.from_dict(snapshot.to_dict()), snapshot)

    def test_missing_optional_identifiers_roundtrip(self) -> None:
        edition = AudiobookEdition("test", "without-identifiers", "Canonical")
        snapshot = snapshot_for(edition)
        restored = EditionSnapshot.from_json(snapshot.to_json())
        self.assertEqual(restored.edition, edition)
        self.assertIsNone(restored.edition.asin)
        self.assertIsNone(restored.edition.release_date)
        self.assertEqual(restored.canonical_title, "Canonical")
        self.assertEqual(restored.edition_title, "Canonical")

    def test_unknown_versions_and_fields_are_rejected(self) -> None:
        snapshot = snapshot_for(MOCK_EDITIONS[0])
        for version in (True, "1", 2, None):
            root: dict[str, object] = dict(snapshot.to_dict())
            root["schema_version"] = version
            with self.assertRaises(ValueError):
                EditionSnapshot.from_dict(root)
        for key in ("extra", "edition"):
            root = dict(snapshot.to_dict())
            if key == "extra":
                root[key] = "ignored data would be lossy"
            else:
                del root[key]
            with self.assertRaises(ValueError):
                EditionSnapshot.from_dict(root)

    def test_wrong_types_dates_and_parts_are_rejected(self) -> None:
        snapshot = snapshot_for(MOCK_EDITIONS[0])
        for key, value in (
            ("authors", [1]),
            ("narrators", "cast"),
            ("part_number", True),
            ("part_number", 0),
            ("part_number", 3),
            ("release_date", "2025-02-30"),
            ("release_date", "20250203"),
            ("series_position", float("nan")),
            ("isbn_10", 123),
        ):
            root: dict[str, object] = dict(snapshot.to_dict())
            data: dict[str, object] = dict(snapshot.to_dict()["edition"])
            data[key] = value
            root["edition"] = data
            with self.assertRaises(ValueError):
                EditionSnapshot.from_dict(root)

    def test_contradictory_titles_and_duplicate_json_keys_are_rejected(self) -> None:
        snapshot = snapshot_for(MOCK_EDITIONS[0])
        for key in ("canonical_title", "edition_title", "search_query"):
            root: dict[str, object] = dict(snapshot.to_dict())
            root[key] = "Contradictory"
            with self.assertRaises(ValueError):
                EditionSnapshot.from_dict(root)
        with self.assertRaisesRegex(ValueError, "Duplicate snapshot field"):
            EditionSnapshot.from_json('{"schema_version":1,"schema_version":1}')
        for invalid in ("null", "[]", "{bad json"):
            with self.assertRaises(ValueError):
                EditionSnapshot.from_json(invalid)

    def test_dict_result_does_not_mutate_snapshot(self) -> None:
        snapshot = snapshot_for(MOCK_EDITIONS[0])
        data = snapshot.to_dict()
        data["edition"]["authors"].append("Other")
        self.assertEqual(snapshot.edition.authors, ("Example Author",))


class PresentationTests(unittest.TestCase):
    def test_part_specific_titles_queries_and_canonical_title(self) -> None:
        first, second = MOCK_EDITIONS
        self.assertEqual(first.title, second.title)
        self.assertEqual(canonical_title(first), canonical_title(second))
        self.assertEqual(display_title(first), "The Lantern Archive - GraphicAudio Part 1 of 2")
        self.assertEqual(search_query(second), "The Lantern Archive - GraphicAudio Part 2 of 2")
        self.assertNotEqual(display_title(first), display_title(second))
        self.assertNotEqual(search_query(first), search_query(second))
        self.assertEqual(first.title, "The Lantern Archive")
        self.assertEqual(first.search_title, "The Lantern Archive")

    def test_title_query_and_canonical_title_are_independent(self) -> None:
        edition = replace(
            MOCK_EDITIONS[0],
            title="Recording title",
            canonical_title="Novel title",
            search_title="Indexer words",
        )
        restored = EditionSnapshot.from_json(snapshot_for(edition).to_json())
        self.assertEqual(restored.canonical_title, "Novel title")
        self.assertEqual(restored.edition.title, "Recording title")
        self.assertEqual(search_query(restored.edition), "Indexer words - GraphicAudio Part 1 of 2")

    def test_legacy_titles_and_non_graphicaudio_parts(self) -> None:
        edition = AudiobookEdition(
            "legacy", "id", "Novel (Part 2 of 2)", part_number=2, part_total=2
        )
        self.assertEqual(display_title(edition), edition.title)
        generic = replace(edition, title="Novel")
        self.assertEqual(display_title(generic), "Novel - Part 2 of 2")
        self.assertNotIn("GraphicAudio", search_query(generic))
        self.assertEqual(display_title(replace(generic, part_total=None)), "Novel - Part 2")


if __name__ == "__main__":
    unittest.main()

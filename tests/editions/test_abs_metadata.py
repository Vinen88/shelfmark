"""Pure output projection; no production settings, filesystem or network."""

import json
import unittest
from dataclasses import replace
from datetime import date
from typing import ClassVar, cast

from mock_backend import MOCK_EDITIONS
from test_search_refinement import RegionalHttp, regional_sources

from shelfmark.download.outputs.abs_metadata import ExportPolicy, convert_snapshot
from shelfmark.metadata_providers.editions.catalog_types import AggregatedEdition, FieldValue
from shelfmark.metadata_providers.editions.identity import EditionIdentity
from shelfmark.metadata_providers.editions.multisource import MultiSourceBackend
from shelfmark.metadata_providers.editions.snapshot import EditionSnapshot

POLICY = ExportPolicy(("https://example.invalid/",))


def snapshots() -> tuple[EditionSnapshot, ...]:
    editions = MultiSourceBackend(regional_sources(RegionalHttp(2))).search("Lantern")
    return tuple(
        EditionSnapshot(EditionIdentity("edition_metadata", edition.provider_id), edition)
        for edition in sorted(editions, key=lambda edition: edition.part_number or 0)
    )


def obj(value: str) -> dict[str, object]:
    result: object = json.loads(value)
    assert isinstance(result, dict)
    return cast("dict[str, object]", result)


class AbsMetadataTests(unittest.TestCase):
    snapshots: ClassVar[tuple[EditionSnapshot, ...]]

    @classmethod
    def setUpClass(cls) -> None:
        cls.snapshots = snapshots()

    def test_two_parts_independent_exact_metadata(self) -> None:
        results = [
            obj(convert_snapshot(snapshot, POLICY).metadata_json) for snapshot in self.snapshots
        ]
        for part, (snapshot, result) in enumerate(zip(self.snapshots, results, strict=True), 1):
            self.assertEqual(result["title"], snapshot.edition_title)
            self.assertIn(f"Part {part} of 2", str(result["title"]))
            self.assertEqual(result["isbn"], snapshot.edition.isbn_13)
            self.assertEqual(result["asin"], snapshot.edition.asin)
            self.assertEqual(result["narrators"], list(snapshot.edition.narrators))
            self.assertEqual(result["authors"], ["Example Author"])
            self.assertEqual(result["publisher"], "Graphic Audio LLC")
            self.assertEqual(result["language"], "english")
            self.assertEqual(result["publishedDate"], "2025-01-15")
            self.assertEqual(result["publishedYear"], "2025")
            self.assertEqual(result["series"], ["The Lantern Cycle #1"])
        for field in ("title", "isbn", "asin", "narrators"):
            self.assertNotEqual(results[0][field], results[1][field])

    def test_regional_aliases_and_every_snapshot_field_lossless(self) -> None:
        for snapshot in self.snapshots:
            before = snapshot.to_json()
            export = convert_snapshot(snapshot, POLICY)
            data = obj(export.provenance_json)
            self.assertEqual(data["schema_version"], 1)
            restored = EditionSnapshot.from_dict(data["snapshot"])
            self.assertEqual(restored.to_json(), before)
            self.assertEqual(snapshot.to_json(), before)
            self.assertIsInstance(restored.edition, AggregatedEdition)
            assert isinstance(restored.edition, AggregatedEdition)
            self.assertEqual(
                {ref.marketplace for ref in restored.edition.provenance.references},
                {"ca", "us", "uk", "au"},
            )
            self.assertEqual(export.warnings, ())

    def test_base_mock_retains_raw_isbns_but_does_not_attest_scope(self) -> None:
        for edition in MOCK_EDITIONS:
            snapshot = EditionSnapshot(
                EditionIdentity("edition_metadata", edition.provider_id), edition
            )
            export = convert_snapshot(snapshot, POLICY)
            self.assertNotIn("isbn", obj(export.metadata_json))
            self.assertEqual(obj(export.metadata_json)["asin"], edition.asin)
            self.assertIn("recording scope", export.warnings[0])
            self.assertEqual(
                EditionSnapshot.from_dict(obj(export.provenance_json)["snapshot"]), snapshot
            )

    def test_missing_identifiers_and_optional_fields_remain_absent(self) -> None:
        edition = replace(
            MOCK_EDITIONS[0],
            isbn_10=None,
            isbn_13=None,
            asin=None,
            authors=(),
            narrators=(),
            publisher=None,
            language=None,
            release_date=None,
            cover_url=None,
            series_name=None,
            series_position=None,
            subtitle=None,
            description=None,
        )
        export = convert_snapshot(
            EditionSnapshot(EditionIdentity("edition_metadata", edition.provider_id), edition)
        )
        self.assertEqual(
            obj(export.metadata_json),
            {
                "title": EditionSnapshot(
                    EditionIdentity("edition_metadata", edition.provider_id), edition
                ).edition_title
            },
        )
        self.assertEqual(export.warnings, ())

    def test_literary_fractional_position_is_independent_of_release_part(self) -> None:
        snapshot = self.snapshots[1]
        export = convert_snapshot(
            replace(snapshot, edition=replace(snapshot.edition, series_position=4.5)), POLICY
        )
        self.assertEqual(obj(export.metadata_json)["series"], ["The Lantern Cycle #4.5"])
        self.assertEqual(
            EditionSnapshot.from_dict(obj(export.provenance_json)["snapshot"]).edition.part_number,
            2,
        )

    def test_missing_series_position_never_uses_release_part(self) -> None:
        snapshot = self.snapshots[1]
        export = convert_snapshot(
            replace(snapshot, edition=replace(snapshot.edition, series_position=None)), POLICY
        )
        self.assertEqual(obj(export.metadata_json)["series"], ["The Lantern Cycle"])

    def test_precise_fractional_series_position_is_not_rounded(self) -> None:
        snapshot = self.snapshots[0]
        precise = 1.2345678901234567
        export = convert_snapshot(
            replace(snapshot, edition=replace(snapshot.edition, series_position=precise)), POLICY
        )
        self.assertEqual(
            obj(export.metadata_json)["series"], ["The Lantern Cycle #" + str(precise)]
        )

    def test_ambiguous_series_name_without_position_is_refused(self) -> None:
        snapshot = self.snapshots[0]
        for name in ("A series #1", " "):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "series"):
                convert_snapshot(
                    replace(
                        snapshot,
                        edition=replace(snapshot.edition, series_name=name, series_position=None),
                    ),
                    POLICY,
                )

    def test_date_over_year_and_year_only(self) -> None:
        snapshot = self.snapshots[0]
        export = convert_snapshot(
            replace(snapshot, edition=replace(snapshot.edition, publish_year=1999)), POLICY
        )
        self.assertEqual(obj(export.metadata_json)["publishedYear"], "2025")
        export = convert_snapshot(
            replace(
                snapshot, edition=replace(snapshot.edition, release_date=None, publish_year=1999)
            ),
            POLICY,
        )
        self.assertEqual(obj(export.metadata_json)["publishedYear"], "1999")
        self.assertNotIn("publishedDate", obj(export.metadata_json))

    def test_unicode_and_repeatable_serialization(self) -> None:
        snapshot = self.snapshots[0]
        changed = replace(
            snapshot,
            edition=replace(
                snapshot.edition,
                title="Lumière — 日本語",
                authors=("Élodie",),
                release_date=date(2024, 2, 29),
            ),
        )
        first = convert_snapshot(changed, POLICY)
        self.assertEqual(convert_snapshot(changed, POLICY), first)
        self.assertIn("Élodie", first.metadata_json)
        self.assertEqual(obj(first.metadata_json)["publishedDate"], "2024-02-29")

    def test_abs_schema_does_not_embed_aliases_identity_cover_or_part_keys(self) -> None:
        export = convert_snapshot(self.snapshots[0], POLICY)
        forbidden = {
            "snapshot",
            "provider",
            "provider_id",
            "part_number",
            "part_total",
            "cover_url",
            "isbn_10",
            "isbn_13",
            "aggregation",
        }
        self.assertFalse(forbidden & obj(export.metadata_json).keys())
        self.assertEqual(set(obj(export.provenance_json)), {"schema_version", "snapshot"})

    def test_invalid_or_unverified_isbn_and_asin_not_exported_as_verified(self) -> None:
        snapshot = self.snapshots[0]
        for isbn in ("9780000000999", "9780306406157"):
            export = convert_snapshot(
                replace(snapshot, edition=replace(snapshot.edition, isbn_13=isbn, isbn_10=None)),
                POLICY,
            )
            self.assertNotIn("isbn", obj(export.metadata_json))
        export = convert_snapshot(
            replace(snapshot, edition=replace(snapshot.edition, asin="B000000099")), POLICY
        )
        self.assertNotIn("asin", obj(export.metadata_json))

    def test_verified_isbn10_normalizes_to_abs_single_isbn13(self) -> None:
        snapshot = self.snapshots[0]
        edition = replace(snapshot.edition, isbn_13=None, isbn_10="0000000019")
        self.assertEqual(
            obj(convert_snapshot(replace(snapshot, edition=edition), POLICY).metadata_json)["isbn"],
            "9780000000019",
        )

    def test_no_url_permissions_by_default(self) -> None:
        with self.assertRaisesRegex(ValueError, "URL"):
            convert_snapshot(self.snapshots[0])

    def test_sensitive_or_unapproved_urls_refused_not_redacted(self) -> None:
        snapshot = self.snapshots[0]
        for url in (
            "https://example.invalid/cover?token=secret",
            "https://user:pass@example.invalid/cover",
            "https://tracker.invalid/announce/secret",
            "https://example.invalid.evil/cover",
            "http://example.invalid/cover",
            "https://example.invalid/../private",
            "https://example.invalid/%2e%2e/private",
            "file:///private",
            "https://example.invalid/cover#secret",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                convert_snapshot(
                    replace(snapshot, edition=replace(snapshot.edition, cover_url=url)), POLICY
                )

    def test_private_provenance_keys_and_nested_urls_refused(self) -> None:
        snapshot = self.snapshots[0]
        assert isinstance(snapshot.edition, AggregatedEdition)
        provenance = snapshot.edition.provenance
        for field, value in (
            ("source.download_url", '"https://tracker.invalid/secret"'),
            ("source.series", '{"token":"secret"}'),
            ("source.copyright", '"https://tracker.invalid/secret"'),
            ("source.new_unreviewed_fact", '"unknown"'),
        ):
            entry = FieldValue(field, provenance.primary, value)
            changed = replace(
                snapshot.edition, provenance=replace(provenance, values=(*provenance.values, entry))
            )
            with self.subTest(field=field), self.assertRaises(ValueError):
                convert_snapshot(replace(snapshot, edition=changed), POLICY)

    def test_description_cannot_smuggle_private_task_data(self) -> None:
        snapshot = self.snapshots[0]
        for description in (
            '<a href="https://tracker.invalid/secret">download</a>',
            "magnet:?xt=secret",
            r"\\server\private",
        ):
            with self.subTest(description=description), self.assertRaises(ValueError):
                convert_snapshot(
                    replace(snapshot, edition=replace(snapshot.edition, description=description)),
                    POLICY,
                )

    def test_identity_or_size_mismatch_fails(self) -> None:
        snapshot = self.snapshots[0]
        for changed in (
            replace(snapshot, identity=EditionIdentity("other", snapshot.identity.provider_id)),
            replace(snapshot, identity=EditionIdentity("edition_metadata", "wrong:id")),
            replace(snapshot, edition=replace(snapshot.edition, description="x" * (256 * 1024))),
        ):
            with self.assertRaises(ValueError):
                convert_snapshot(changed, POLICY)

    def test_invalid_public_policy_fails(self) -> None:
        for prefix in (
            "https://example.invalid",
            "https://user:pass@example.invalid/",
            "http://example.invalid/",
            "https://example.invalid/?token=x/",
        ):
            with self.subTest(prefix=prefix), self.assertRaises(ValueError):
                ExportPolicy((prefix,))


if __name__ == "__main__":
    unittest.main()

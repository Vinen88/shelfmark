"""The observed Dark Age failure, without public catalogs or download clients."""

import json
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from mock_sources import MockCatalogSource

from shelfmark.core.edition_handoff import EditionLocator, resolve_release
from shelfmark.metadata_providers.editions.catalog_types import (
    AggregatedEdition,
    SourceError,
    SourceRecord,
)
from shelfmark.metadata_providers.editions.multisource import (
    AggregationOptions,
    MultiSourceBackend,
    encode_identity,
)
from shelfmark.metadata_providers.editions.snapshot import EditionSnapshot


class CompletenessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.snapshot = EditionSnapshot.from_json(
            (Path(__file__).parent / "fixtures/dark-age.snapshot.json").read_text()
        )
        assert isinstance(self.snapshot.edition, AggregatedEdition)
        self.edition = self.snapshot.edition
        self.payload: dict[str, object] = {
            "source": "prowlarr",
            "source_id": "synthetic-dark-age",
            "content_type": "audiobook",
            "language": "en",
            "title": "Dark Age (Part 1 of 3) by Pierce Brown [ENG / M4B]",
            "edition_context": EditionLocator(self.snapshot.identity).to_dict(),
        }

    def resolve(self, edition: AggregatedEdition) -> None:
        _, context = resolve_release(
            self.payload, lambda _: EditionSnapshot(self.snapshot.identity, edition)
        )
        assert context is not None
        self.assertEqual(context.snapshot.edition, edition)
        context.validate_release("prowlarr", "synthetic-dark-age")

    def test_observed_dark_age_and_bounded_discovery_warnings_survive(self) -> None:
        for warning in (
            "libex_live_uk: RateLimited",
            "libex_history_uk: timeout",
            "libex_history_us: timeout",
            "libex_live_ca page 2: repeated page identifiers; pagination incomplete",
            "libex_history_au page 1: skipped (deadline or request cap)",
            "libex_live_us: discovery window reached; page 2 saturated, additional pages not read",
            "Enrichment skipped: request deadline reached",
            "Enrichment skipped: search request cap reached",
            "Enrichment incomplete: using verified search records",
            "libex_live_au: SourceError",
            "Language policy excluded 66 source records (including unknown if disabled)",
            "Aggregate discovery window reached (40 of 228 observed groups)",
        ):
            with self.subTest(warning=warning):
                self.resolve(
                    replace(
                        self.edition,
                        provenance=replace(self.edition.provenance, warnings=(warning,)),
                    )
                )

    def test_missing_optional_alias_and_failed_enrichment_keep_exact_primary(self) -> None:
        ref = self.edition.provenance.primary
        record = SourceRecord(
            ref,
            replace(self.edition, catalog=ref.source, edition_id=ref.record_id),
            asin_verified=True,
            recording_isbns=("9798890557254",),
            production="dramatized",
        )
        source = MockCatalogSource(ref.source, "libex_live", (record,), marketplace="ca")
        missing = MockCatalogSource("libex_live_us", "libex_live", (), marketplace="us")
        backend = MultiSourceBackend((source, missing), AggregationOptions(timeout=0.05))
        with patch.object(
            source, "search", side_effect=AssertionError("No broad discovery on detail")
        ):
            found = backend.get("aggregate", encode_identity(ref))
            assert isinstance(found, AggregatedEdition)
            self.resolve(found)
            with patch.object(missing, "lookup", side_effect=SourceError("optional source failed")):
                found = backend.get("aggregate", encode_identity(ref))
                assert isinstance(found, AggregatedEdition)
                self.assertIn("libex_live_us: SourceError", found.provenance.warnings)
                self.resolve(found)
        with patch.object(source, "get", return_value=None):
            self.assertIsNone(backend.get("aggregate", encode_identity(ref)))
        with patch.object(
            source,
            "get",
            return_value=replace(record, reference=replace(ref, record_id="B000000000")),
        ):
            with self.assertRaisesRegex(SourceError, "different recording"):
                backend.get("aggregate", encode_identity(ref))

    def test_identifier_and_part_conflicts_still_reject(self) -> None:
        cases = (
            (replace(self.edition, asin="invalid"), "ASIN"),
            (replace(self.edition, asin=""), "ASIN"),
            (replace(self.edition, asin="B000000000"), "ASIN"),
            (replace(self.edition, isbn_13="9798890557255"), "ISBN"),
            (replace(self.edition, isbn_13=""), "ISBN"),
            (replace(self.edition, isbn_10="0306406152"), "ISBN"),
            (replace(self.edition, isbn_13="9780306406157"), "ISBN"),
            (replace(self.edition, part_number=2), "part"),
            (replace(self.edition, part_total=None), "part"),
            (replace(self.edition, edition_label="conventional"), "production"),
        )
        for edition, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                self.resolve(edition)
        for field, value, message in (
            ("identity.asin_verified", False, "ASIN"),
            ("recording.production", "conventional", "production"),
        ):
            values = tuple(
                replace(entry, value_json=json.dumps(value)) if entry.field == field else entry
                for entry in self.edition.provenance.values
            )
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, message):
                self.resolve(
                    replace(
                        self.edition, provenance=replace(self.edition.provenance, values=values)
                    )
                )

    def test_unverified_optional_isbn_keeps_verified_asin_and_stays_out_of_abs(self) -> None:
        from shelfmark.download.outputs.abs_metadata import convert_snapshot

        values = tuple(
            replace(v, value_json="[]") if v.field == "identity.recording_isbns" else v
            for v in self.edition.provenance.values
        )
        legacy = replace(self.edition, provenance=replace(self.edition.provenance, values=values))
        self.resolve(legacy)  # Previously frozen raw ISBN is optional when ASIN is verified.
        exported = json.loads(
            convert_snapshot(replace(self.snapshot, edition=legacy)).metadata_json
        )
        self.assertNotIn("isbn", exported)
        self.assertEqual(exported["asin"], legacy.asin)
        with self.assertRaisesRegex(ValueError, "verified ASIN or recording ISBN"):
            self.resolve(replace(legacy, asin=None))
        ref = self.edition.provenance.primary
        raw = SourceRecord(
            ref,
            replace(self.edition, catalog=ref.source, edition_id=ref.record_id),
            asin_verified=True,
            production="dramatized",
        )
        backend = MultiSourceBackend(
            (MockCatalogSource(ref.source, "libex_live", (raw,), marketplace="ca"),)
        )
        merged = backend.search("Dark Age")[0]
        assert isinstance(merged, AggregatedEdition)
        self.assertIsNone(merged.isbn_13)
        self.assertTrue(any(v.field == "isbn_13" for v in merged.provenance.values))
        self.resolve(merged)

    def test_conflicting_verified_alias_isbns_still_reject(self) -> None:
        values = tuple(
            replace(v, value_json='["9798890557254","9780306406157"]')
            if v.field == "identity.recording_isbns"
            else v
            for v in self.edition.provenance.values
        )
        with self.assertRaisesRegex(ValueError, "conflicts with verified"):
            self.resolve(
                replace(self.edition, provenance=replace(self.edition.provenance, values=values))
            )

    def test_verified_alias_isbn_replaces_unverified_primary_isbn(self) -> None:
        ref = self.edition.provenance.primary
        raw = SourceRecord(
            ref,
            replace(
                self.edition, catalog=ref.source, edition_id=ref.record_id, isbn_13="9780306406157"
            ),
            asin_verified=True,
            production="dramatized",
        )
        alias = replace(
            raw,
            reference=replace(ref, source="verified_alias"),
            edition=replace(raw.edition, isbn_13="9798890557254"),
            recording_isbns=("9798890557254",),
        )
        backend = MultiSourceBackend(
            (
                MockCatalogSource(ref.source, "libex_live", (raw,), marketplace="ca"),
                MockCatalogSource("verified_alias", "audiobookdb", (alias,), marketplace="ca"),
            )
        )
        merged = backend.search("Dark Age")[0]
        assert isinstance(merged, AggregatedEdition)
        self.assertEqual(merged.isbn_13, "9798890557254")
        self.assertTrue(
            any(
                v.field == "isbn_13" and json.loads(v.value_json) == "9780306406157"
                for v in merged.provenance.values
            )
        )
        self.resolve(merged)

    def test_unresolved_selected_response_and_unknown_warning_reject(self) -> None:
        for warning in (
            "Libex response incomplete: missing identifiers",
            "Unreviewed recording warning",
        ):
            with self.assertRaisesRegex(ValueError, "refresh/review detail"):
                self.resolve(
                    replace(
                        self.edition,
                        provenance=replace(self.edition.provenance, warnings=(warning,)),
                    )
                )

    def test_failed_resolution_stale_identity_and_explicit_release_conflicts(self) -> None:
        for snapshot in (
            None,
            replace(
                self.snapshot,
                identity=replace(self.snapshot.identity, provider_id="aggregate:stale"),
            ),
        ):
            with self.assertRaisesRegex(ValueError, "different identity"):
                resolve_release(self.payload, lambda _: snapshot)  # noqa: B023 -- Fixture callback runs synchronously within this iteration.
        for title in ("Dark Age Part 2", "Dark Age Part 1 of 2", "Dark Age single-narrator"):
            with self.subTest(title=title), self.assertRaisesRegex(ValueError, "conflicts"):
                resolve_release({**self.payload, "title": title}, lambda _: self.snapshot)
        with self.assertRaises(SourceError):
            resolve_release(
                self.payload,
                lambda _: (_ for _ in ()).throw(SourceError("Selected source unavailable")),
            )

    def test_insufficient_identity_and_stale_primary_reject(self) -> None:
        with self.assertRaisesRegex(ValueError, "verified ASIN or recording ISBN"):
            self.resolve(replace(self.edition, asin=None, isbn_13=None))
        primary = replace(self.edition.provenance.primary, record_id="B000000000")
        with self.assertRaisesRegex(ValueError, "aggregate identity"):
            self.resolve(
                replace(self.edition, provenance=replace(self.edition.provenance, primary=primary))
            )

    def test_native_download_without_edition_locator_is_unchanged(self) -> None:
        payload = {key: value for key, value in self.payload.items() if key != "edition_context"}
        result, context = resolve_release(
            payload, lambda _: self.fail("Native downloads must not resolve editions")
        )
        self.assertIs(result, payload)
        self.assertIsNone(context)

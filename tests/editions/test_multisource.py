"""Fictional catalog records exercise aggregation, never network or queues."""

import json
import os
import subprocess
import sys
import unittest
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from time import monotonic, sleep
from unittest.mock import patch

from mock_backend import MOCK_EDITIONS
from mock_sources import MockCatalogSource

from shelfmark.metadata_providers.editions.catalog_types import (
    AggregatedEdition,
    Availability,
    RateLimited,
    SourceError,
    SourceRecord,
    SourceRef,
)
from shelfmark.metadata_providers.editions.identity import EditionIdentity
from shelfmark.metadata_providers.editions.multisource import (
    AggregationOptions,
    MultiSourceBackend,
    decode_identity,
    encode_identity,
    equivalent,
    normalized_isbn,
)
from shelfmark.metadata_providers.editions.multisource_config import MultiSourceSettings
from shelfmark.metadata_providers.editions.presentation import display_title, search_query
from shelfmark.metadata_providers.editions.snapshot import EditionSnapshot


def record(
    source: str,
    part: int = 1,
    *,
    region: str | None = None,
    availability: Availability = Availability.UNKNOWN,
) -> SourceRecord:
    original = MOCK_EDITIONS[part - 1]
    return SourceRecord(
        SourceRef(source, original.asin or "", region, availability),
        replace(original, catalog=source, edition_id=original.asin or ""),
        asin_verified=True,
        production="dramatized",
    )


def source(
    key: str, family: str = "libex_live", *, part: int = 1, region: str | None = None
) -> MockCatalogSource:
    return MockCatalogSource(key, family, (record(key, part, region=region),), marketplace=region)


def fixture_backend() -> MultiSourceBackend:
    ca = record("live_ca", region="ca", availability=Availability.LIVE)
    us = record("live_us", region="us", availability=Availability.LIVE)
    ca = replace(ca, edition=replace(ca.edition, publisher=None, cover_url=None))
    historical = record("history", 2, availability=Availability.HISTORICAL)
    independent = replace(
        record("independent"),
        reference=SourceRef("independent", "db-only"),
        edition=replace(
            MOCK_EDITIONS[0],
            catalog="independent",
            edition_id="db-only",
            title="Another Novel",
            canonical_title="Another Novel",
            asin=None,
            isbn_10=None,
            isbn_13=None,
        ),
    )
    return MultiSourceBackend(
        (
            MockCatalogSource("live_ca", "libex_live", (ca,), marketplace="ca"),
            MockCatalogSource("live_us", "libex_live", (us,), marketplace="us"),
            MockCatalogSource("history", "libex_history", (historical,)),
            MockCatalogSource("independent", "audiobookdb", (independent,)),
        )
    )


class AggregationTests(unittest.TestCase):
    def test_ca_us_dedup_enrichment_conflicts_and_provenance(self) -> None:
        backend = fixture_backend()
        found = backend.search("Lantern")
        self.assertEqual(len(found), 2)
        first = found[0]
        assert isinstance(first, AggregatedEdition)
        self.assertEqual(first.provenance.primary.marketplace, "ca")
        self.assertEqual(len(first.provenance.references), 2)
        self.assertEqual(first.publisher, MOCK_EDITIONS[0].publisher)
        self.assertEqual(first.cover_url, MOCK_EDITIONS[0].cover_url)
        self.assertEqual(first.provenance.primary.source, "live_ca")
        self.assertEqual(first.part_number, 1)

    def test_missing_fields_fill_and_conflicts_are_not_overwritten(self) -> None:
        a, b = record("a"), record("b")
        b = replace(
            b, edition=replace(b.edition, publisher="Different publisher", release_date=None)
        )
        backend = MultiSourceBackend(
            (
                MockCatalogSource("a", "libex_live", (a,)),
                MockCatalogSource("b", "audiobookdb", (b,)),
            )
        )
        result = backend.search("Lantern")[0]
        assert isinstance(result, AggregatedEdition)
        self.assertEqual(result.publisher, a.edition.publisher)
        self.assertIn("publisher", result.provenance.conflicts)
        evidence = [v for v in result.provenance.values if v.field == "publisher"]
        self.assertEqual(
            {json.loads(v.value_json) for v in evidence},
            {a.edition.publisher, "Different publisher"},
        )

    def test_restart_detail_reconstructs_same_enrichment_without_search(self) -> None:
        original = fixture_backend().search("Lantern")[0]
        restarted = fixture_backend()
        with ExitStack() as stack:
            for adapter in restarted.sources:
                stack.enter_context(
                    patch.object(
                        adapter, "search", side_effect=AssertionError("No detail title searches")
                    )
                )
            self.assertEqual(restarted.get(original.catalog, original.edition_id), original)
        self.assertEqual(decode_identity(original.edition_id).source, "live_ca")

    def test_fresh_interpreter_restores_selected_identity_and_snapshot(self) -> None:
        original = fixture_backend().search("Lantern")[0]
        expected = EditionSnapshot(
            EditionIdentity("edition_metadata", original.provider_id), original
        ).to_json()
        script = (
            "import sys; from test_multisource import fixture_backend; "
            "from shelfmark.metadata_providers.editions.snapshot import EditionSnapshot; "
            "from shelfmark.metadata_providers.editions.identity import EditionIdentity; "
            "edition=fixture_backend().get('aggregate',sys.argv[1]); "
            "assert edition is not None; "
            "print(EditionSnapshot(EditionIdentity('edition_metadata',edition.provider_id),edition).to_json())"
        )
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(
            (
                str(Path(__file__).parent),
                str(Path(__file__).resolve().parents[2]),
                env.get("PYTHONPATH", ""),
            )
        )
        result = subprocess.run(
            [sys.executable, "-B", "-c", script, original.edition_id],
            env=env,
            text=True,
            capture_output=True,
            timeout=10,
            check=True,
        )
        self.assertEqual(result.stdout.strip(), expected)

    def test_historical_and_independent_only_records(self) -> None:
        backend = fixture_backend()
        historical = backend.search("Part 2")[0]
        assert isinstance(historical, AggregatedEdition)
        self.assertEqual(historical.provenance.primary.availability, Availability.HISTORICAL)
        self.assertEqual(len(backend.search("Another")), 1)

    def test_known_delisted_fixture_is_distinct_from_unknown_history(self) -> None:
        item = record("archive", 2, availability=Availability.DELISTED)
        found = MultiSourceBackend(
            (MockCatalogSource("archive", "libex_history", (item,)),)
        ).search("Lantern")[0]
        assert isinstance(found, AggregatedEdition)
        self.assertEqual(found.provenance.primary.availability, Availability.DELISTED)
        snapshot = EditionSnapshot(EditionIdentity("edition_metadata", found.provider_id), found)
        self.assertEqual(EditionSnapshot.from_json(snapshot.to_json()), snapshot)

    def test_lookup_only_enrichment_source_is_not_searched(self) -> None:
        first = replace(record("catalog"), edition=replace(MOCK_EDITIONS[0], publisher=None))
        enrichment = MockCatalogSource(
            "audnexus", "audnexus", (record("audnexus"),), searchable=False
        )
        with patch.object(
            enrichment, "search", side_effect=AssertionError("No title-search capability")
        ):
            backend = MultiSourceBackend(
                (MockCatalogSource("catalog", "libex_live", (first,)), enrichment)
            )
            found = backend.search("Lantern")[0]
            self.assertEqual(backend.get(found.catalog, found.edition_id), found)
        self.assertEqual(found.publisher, MOCK_EDITIONS[0].publisher)

    def test_distinct_asins_parts_and_same_canonical_title(self) -> None:
        backend = fixture_backend()
        found = backend.search("Lantern")
        self.assertEqual(found[0].canonical_title, found[1].canonical_title)
        for attr in ("edition_id", "asin"):
            self.assertNotEqual(getattr(found[0], attr), getattr(found[1], attr))
        for edition in found:
            self.assertIsNone(edition.isbn_10)  # These fixture ISBNs have no recording attestation.
            self.assertIsNone(edition.isbn_13)
        self.assertNotEqual(display_title(found[0]), display_title(found[1]))
        self.assertNotEqual(search_query(found[0]), search_query(found[1]))

    def test_three_parts_do_not_merge_or_repeat_qualifiers(self) -> None:
        records = tuple(
            replace(
                record("three"),
                reference=SourceRef("three", f"PART{part:06d}"),
                edition=replace(
                    MOCK_EDITIONS[0],
                    edition_id=f"PART{part:06d}",
                    asin=f"PART{part:06d}",
                    title=f"Novel ({part} of 3) [Dramatized Adaptation]",
                    canonical_title="Novel",
                    search_title=None,
                    edition_label="Dramatized Adaptation",
                    part_number=part,
                    part_total=3,
                ),
            )
            for part in (1, 2, 3)
        )
        backend = MultiSourceBackend((MockCatalogSource("three", "libex_history", records),))
        found = backend.search("Novel")
        self.assertEqual(len(found), 3)
        for result in found:
            self.assertEqual(display_title(result), result.title)
            self.assertEqual(search_query(result), result.title)
        unspecified_total = replace(
            MOCK_EDITIONS[0], title="Novel Book 1", part_total=None, edition_label=None
        )
        self.assertEqual(display_title(unspecified_total), "Novel Book 1 - Part 1")

    def test_narration_language_adaptation_and_identifier_conflicts_veto_merge(self) -> None:
        a, b = record("a"), record("b")
        self.assertTrue(equivalent(a, b))
        for changed in (
            replace(b, edition=replace(b.edition, asin="OTHERASIN1")),
            replace(b, edition=replace(b.edition, part_number=2)),
            replace(b, edition=replace(b.edition, narrators=("Solo Narrator",))),
            replace(b, edition=replace(b.edition, language="fr")),
            replace(b, production="conventional"),
        ):
            self.assertFalse(equivalent(a, changed))
        a = replace(a, recording_isbns=(MOCK_EDITIONS[0].isbn_13 or "",))
        b = replace(b, recording_isbns=(MOCK_EDITIONS[1].isbn_13 or "",))
        self.assertFalse(equivalent(a, b))

    def test_novel_isbn_and_fuzzy_title_are_not_equivalence_evidence(self) -> None:
        a, b = record("a"), record("b")
        a = replace(a, asin_verified=False)
        b = replace(b, asin_verified=False)
        self.assertFalse(equivalent(a, b))
        a = replace(a, recording_isbns=(MOCK_EDITIONS[0].isbn_10 or "",))
        b = replace(b, recording_isbns=(MOCK_EDITIONS[0].isbn_13 or "",))
        self.assertTrue(equivalent(a, b))
        self.assertIsNone(normalized_isbn("9DLACourtOfThorns"))
        self.assertIsNone(normalized_isbn("9780000000010"))

    def test_explicit_recording_links_and_nontransitive_conflicts(self) -> None:
        a, b, c = record("a"), record("b"), record("c", 2)
        b = replace(
            b,
            links=(a.reference, c.reference),
            asin_verified=False,
            edition=replace(b.edition, asin=None, part_number=None, part_total=None, narrators=()),
        )
        self.assertTrue(equivalent(a, b))
        self.assertTrue(equivalent(b, c))
        self.assertFalse(equivalent(a, c))
        backend = MultiSourceBackend(
            tuple(MockCatalogSource(r.reference.source, "libex_live", (r,)) for r in (a, b, c))
        )
        self.assertEqual(len(backend.search("Lantern")), 2)

    def test_same_catalog_aliases_are_retained_and_restored(self) -> None:
        first = replace(record("catalog"), reference=SourceRef("catalog", "first"))
        other = replace(
            first,
            reference=SourceRef("catalog", "second"),
            edition=replace(first.edition, title="Alternate catalog title", canonical_title=None),
        )
        adapters = (MockCatalogSource("catalog", "audiobookdb", (first, other)),)
        found = MultiSourceBackend(adapters).search("Lantern")[0]
        assert isinstance(found, AggregatedEdition)
        self.assertEqual(len(found.provenance.references), 2)
        self.assertEqual(MultiSourceBackend(adapters).get(found.catalog, found.edition_id), found)

    def test_different_regional_recordings_remain_separate(self) -> None:
        a, b = source("ca", region="ca"), source("us", region="us")
        b.records = (
            replace(b.records[0], edition=replace(b.records[0].edition, asin="OTHERASIN1")),
        )
        self.assertEqual(len(MultiSourceBackend((a, b)).search("Lantern")), 2)

    def test_language_modes_and_unknown_language_are_explicit(self) -> None:
        en = record("en")
        fr = replace(record("fr", 2), edition=replace(MOCK_EDITIONS[1], language="French"))
        unknown = replace(
            record("unknown", 2),
            edition=replace(MOCK_EDITIONS[1], language=None, asin="UNKNOWN001"),
        )
        sources = tuple(
            MockCatalogSource(r.reference.source, "audiobookdb", (r,)) for r in (fr, en, unknown)
        )
        self.assertEqual(
            len(
                MultiSourceBackend(
                    sources, AggregationOptions(language_mode="english_only")
                ).search("Lantern")
            ),
            1,
        )
        self.assertEqual(len(MultiSourceBackend(sources).search("Lantern")), 2)
        preferred = MultiSourceBackend(
            sources,
            AggregationOptions(language_mode="prefer_english", include_unknown_language=True),
        )
        found = preferred.search("Lantern")
        self.assertEqual(len(found), 3)
        self.assertEqual(found[0].language, "en")
        self.assertEqual(
            len(
                MultiSourceBackend(sources, AggregationOptions(language_mode="all")).search(
                    "Lantern"
                )
            ),
            2,
        )

    def test_priority_changes_new_primary_but_old_identity_still_resolves(self) -> None:
        a, b = source("a", "libex_live"), source("b", "audiobookdb")
        first = MultiSourceBackend((a, b)).search("Lantern")[0]
        changed = MultiSourceBackend(
            (a, b), AggregationOptions(source_priority=("audiobookdb", "libex_live"))
        )
        second = changed.search("Lantern")[0]
        self.assertNotEqual(first.edition_id, second.edition_id)
        resolved = changed.get(first.catalog, first.edition_id)
        assert resolved is not None
        self.assertEqual(resolved.edition_id, first.edition_id)
        self.assertEqual(resolved.asin, first.asin)

    def test_partial_failures_and_rate_limit_keep_available_records(self) -> None:
        a, b = source("a"), source("b")
        with (
            patch.object(b, "search", side_effect=RateLimited(30)),
            patch.object(b, "lookup", side_effect=SourceError("outage")),
        ):
            found = MultiSourceBackend((a, b)).search("Lantern")
        self.assertEqual(len(found), 1)
        assert isinstance(found[0], AggregatedEdition)
        self.assertTrue(found[0].provenance.warnings)
        with patch.object(a, "search", side_effect=SourceError("outage")):
            with self.assertRaises(SourceError):
                MultiSourceBackend((a,)).search("Lantern")

    def test_search_deadline_does_not_wait_for_bad_source(self) -> None:
        a, b = source("a"), source("b")

        def slow(*args: object) -> tuple[SourceRecord, ...]:
            sleep(0.3)
            return ()

        with patch.object(b, "search", side_effect=slow):
            started = monotonic()
            found = MultiSourceBackend((a, b), AggregationOptions(timeout=0.1)).search("Lantern")
            self.assertLess(monotonic() - started, 0.25)
            self.assertEqual(len(found), 1)

    def test_empty_partial_failure_is_not_reported_as_empty_catalog(self) -> None:
        empty, failed = source("empty"), source("failed")
        empty.records = ()
        with patch.object(failed, "search", side_effect=SourceError("Unavailable")):
            with self.assertRaisesRegex(SourceError, "discovery incomplete"):
                MultiSourceBackend((empty, failed)).search("Lantern")
        self.assertEqual(MultiSourceBackend((empty,)).search("Lantern"), [])

    def test_discovery_uses_full_budget_and_keeps_verified_results(self) -> None:
        delayed = source("delayed")

        def discovery(*args: object) -> tuple[SourceRecord, ...]:
            sleep(0.06)  # Beyond the old 40% discovery allocation of 0.04s.
            return delayed.records

        with patch.object(delayed, "search", side_effect=discovery):
            found = MultiSourceBackend((delayed,), AggregationOptions(timeout=0.1)).search(
                "Lantern"
            )
        self.assertEqual(len(found), 1)

    def test_enrichment_timeout_does_not_drop_selected_record_or_other_parts(self) -> None:
        first, second = record("catalog"), record("catalog", 2)
        catalog = MockCatalogSource("catalog", "libex_live", (first, second))

        def slow_lookup(selected: SourceRecord, timeout: float) -> tuple[SourceRecord, ...]:
            if selected.edition.part_number == 2:
                sleep(0.2)
            return (selected,)

        with patch.object(catalog, "lookup", side_effect=slow_lookup):
            found = MultiSourceBackend((catalog,), AggregationOptions(timeout=0.05)).search(
                "Lantern"
            )
        self.assertEqual([edition.part_number for edition in found], [1, 2])
        assert isinstance(found[1], AggregatedEdition)
        self.assertTrue(found[1].provenance.warnings)

    def test_source_relevance_order_is_not_replaced_by_asin_order(self) -> None:
        catalog = MockCatalogSource(
            "catalog", "libex_live", (record("catalog", 2), record("catalog"))
        )
        found = MultiSourceBackend((catalog,)).search("Lantern")
        self.assertEqual([edition.part_number for edition in found], [2, 1])
        self.assertEqual(
            [
                edition.part_number
                for edition in MultiSourceBackend((catalog,)).search("Lantern", limit=1, page=2)
            ],
            [1],
        )

    def test_discovery_and_aggregate_caps_are_explicit(self) -> None:
        first, second = source("a"), source("b", part=2)
        found = MultiSourceBackend((first, second), AggregationOptions(window=1)).search("Lantern")
        self.assertEqual(len(found), 1)
        assert isinstance(found[0], AggregatedEdition)
        self.assertTrue(
            any("discovery window reached" in warning for warning in found[0].provenance.warnings)
        )
        self.assertTrue(
            any(
                "Aggregate discovery window reached" in warning
                for warning in found[0].provenance.warnings
            )
        )

    def test_language_exclusions_are_visible_on_surviving_results(self) -> None:
        known, unknown = source("known"), source("unknown", part=2)
        unknown.records = (
            replace(unknown.records[0], edition=replace(unknown.records[0].edition, language=None)),
        )
        found = MultiSourceBackend((known, unknown)).search("Lantern")
        assert isinstance(found[0], AggregatedEdition)
        self.assertTrue(
            any("Language policy excluded 1" in warning for warning in found[0].provenance.warnings)
        )
        included = MultiSourceBackend(
            (known, unknown), AggregationOptions(include_unknown_language=True)
        ).search("Lantern")
        self.assertEqual(len(included), 2)

    def test_missing_ids_and_unavailable_selected_source(self) -> None:
        a, b = source("a"), source("b")
        backend = MultiSourceBackend((a, b))
        found = backend.search("Lantern")
        self.assertEqual(len(found), 1)
        with self.assertRaises(SourceError):
            MultiSourceBackend((b,)).get(found[0].catalog, found[0].edition_id)
        missing = replace(
            record("missing"),
            edition=replace(MOCK_EDITIONS[0], asin=None, isbn_10=None, isbn_13=None),
        )
        self.assertEqual(
            len(
                MultiSourceBackend(
                    (MockCatalogSource("missing", "audiobookdb", (missing,)),)
                ).search("Lantern")
            ),
            1,
        )

    def test_lookup_identity_validation_and_bounded_pagination(self) -> None:
        backend = fixture_backend()
        for value in ("../bad", "", "e30", "a" * 1600):
            self.assertIsNone(backend.get("aggregate", value))
        self.assertIsNone(backend.get("wrong", encode_identity(record("x").reference)))
        self.assertNotEqual(
            encode_identity(SourceRef("a:b", "c")), encode_identity(SourceRef("a", "b:c"))
        )
        first, second = (
            backend.search("Lantern", limit=1),
            backend.search("Lantern", limit=1, page=2),
        )
        self.assertNotEqual(first[0].edition_id, second[0].edition_id)
        self.assertEqual(backend.search("Lantern", page=100), [])

    def test_v2_snapshot_roundtrip_and_tampered_provenance(self) -> None:
        edition = fixture_backend().search("Lantern")[0]
        snapshot = EditionSnapshot(
            EditionIdentity("edition_metadata", edition.provider_id), edition
        )
        self.assertEqual(snapshot.to_dict()["schema_version"], 2)
        self.assertEqual(EditionSnapshot.from_json(snapshot.to_json()), snapshot)
        data = snapshot.to_dict()
        data["aggregation"]["primary"] = {
            "source": "other",
            "record_id": "id",
            "marketplace": None,
            "availability": "unknown",
        }
        with self.assertRaises(ValueError):
            EditionSnapshot.from_dict(data)
        legacy = EditionSnapshot(
            EditionIdentity("edition_metadata", MOCK_EDITIONS[0].provider_id), MOCK_EDITIONS[0]
        )
        self.assertEqual(legacy.to_dict()["schema_version"], 1)
        self.assertNotIn("aggregation", legacy.to_dict())

    def test_safe_settings_defaults_and_no_clients_without_optin(self) -> None:
        settings = MultiSourceSettings.from_mapping({})
        self.assertFalse(settings.backend().is_available())
        self.assertEqual(settings.options.marketplaces, ("ca", "us", "uk", "au"))
        self.assertFalse(settings.options.include_unknown_language)
        with patch(
            "shelfmark.metadata_providers.editions.multisource_config.HttpClient",
            side_effect=AssertionError("No client"),
        ):
            MultiSourceSettings.from_mapping({}).backend()
        enabled = MultiSourceSettings.from_mapping({"EDITION_LIBEX_ENABLED": True}).backend()
        assert isinstance(enabled, MultiSourceBackend)
        self.assertEqual(len(enabled.sources), 8)  # Construction only; no HTTP request.
        self.assertEqual(len({adapter.key for adapter in enabled.sources}), 8)
        for mapping in (
            {"EDITION_MARKETPLACES": "ca,ca"},
            {"EDITION_MARKETPLACES": "en"},
            {"EDITION_LANGUAGE_MODE": "canada"},
            {"EDITION_SOURCE_TIMEOUT": 0},
            {"EDITION_UNKNOWN_SOURCE_ENABLED": True},
        ):
            with self.assertRaises(ValueError):
                MultiSourceSettings.from_mapping(mapping)


if __name__ == "__main__":
    unittest.main()

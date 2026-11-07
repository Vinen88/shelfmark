"""Fictional records in recorded API shapes; no live metadata service is used."""

import json
import unittest
from collections.abc import Mapping
from contextlib import ExitStack
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

from app_harness import AppHarness
from mock_sources import MockCatalogSource
from test_libex import FixtureHttp
from test_libex import book as libex_book

from shelfmark.core.edition_handoff import (
    EditionLocator,
    FrozenEditionContext,
    context_from_task,
    resolve_release,
)
from shelfmark.core.edition_ownership import library_recording, match_edition_entries
from shelfmark.core.library_providers import audiobookshelf
from shelfmark.core.queue import BookQueue
from shelfmark.core.release_routes import register_release_routes
from shelfmark.download import orchestrator
from shelfmark.download.outputs.abs_metadata import ExportPolicy, convert_snapshot
from shelfmark.metadata_providers import MetadataSearchOptions
from shelfmark.metadata_providers.editions.audiobookdb import AudiobookDBSource, validate_contract
from shelfmark.metadata_providers.editions.audnexus import AudnexusSource
from shelfmark.metadata_providers.editions.catalog_types import (
    AggregatedEdition,
    RateLimited,
    SourceError,
    SourceRef,
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
from shelfmark.metadata_providers.editions.provider import EditionMetadataProvider
from shelfmark.metadata_providers.editions.snapshot import EditionSnapshot
from shelfmark.metadata_providers.editions.source_http import (
    HttpClient,
    HttpResponse,
    _NoCredentialRedirect,
)

WORK = "W346789ABCD3"
RELEASES = ("R346789ABCD3", "R346789ABCD4", "R346789ABCD6")
ASINS = ("B012345671", "B012345672", "B012345673")
ISBNS = ("9780000000019", "9780000000026", "9780000000033")


def credit(name: str, role: str) -> dict[str, object]:
    return {"person": {"id": "P346789ABCD3", "name": name}, "role": {"name": role}}


def release(part: int = 1, *, asin: bool = True) -> dict[str, object]:
    return {
        "id": RELEASES[part - 1],
        "title": f"Lantern (Part {part} of 3) [Dramatized Adaptation]",
        "book": {"id": WORK, "title": "Lantern"},
        "isbn": ISBNS[part - 1],
        "people": [credit("Example Cast", "Narrator")],
        "publisher": {"id": "P346789ABCD3", "name": "Fixture publisher"},
        "language": {"name": "English"},
        "runtimeLengthSec": 18000,
        "duration": "05:00:00",
        "releaseDate": "2025-01-15T00:00:00.000Z",
        "external": [
            {
                "id": "E346789ABCD3",
                "itemId": ASINS[part - 1],
                "category": {
                    "title": "Audible",
                    "url": "https://www.audible.com/",
                    "itemKind": "id",
                    "allowedOnRelease": True,
                },
            }
        ]
        if asin
        else [],
    }


def work(releases: list[dict[str, object]]) -> dict[str, object]:
    return {
        "id": WORK,
        "title": "Lantern",
        "people": [credit("Example Author", "Author")],
        "releases": releases,
        "series": [
            {
                "ordinal": "5",
                "position": {
                    "value": "5",
                    "label": "5",
                    "sortKey": 5,
                    "kind": "point",
                    "status": "complete",
                    "spans": [],
                },
                "series": {"id": "S346789ABCD3", "title": "Lantern Cycle"},
            }
        ],
    }


def audbook(part: int = 1) -> dict[str, object]:
    return {
        "asin": ASINS[part - 1],
        "region": "ca",
        "title": f"Lantern (Part {part} of 3) [Dramatized Adaptation]",
        "authors": [{"name": "Example Author"}],
        "narrators": [{"name": "Example Cast"}],
        "publisherName": "Fixture publisher",
        "isbn": ISBNS[part - 1],
        "language": "English",
        "releaseDate": "2025-01-15T00:00:00.000Z",
        "runtimeLengthMin": 300,
        "seriesPrimary": {"name": "Lantern Cycle", "position": "5"},
        "formatType": "unabridged",
    }


class DBHttp:
    def __init__(self, records: list[dict[str, object]] | None = None) -> None:
        records = records if records is not None else [release()]
        self.work = work(records)
        self.works = {WORK: self.work}
        self.records = {str(row["id"]): row for row in records}
        self.hits: object = [{"id": WORK, "title": "Lantern"}]
        self.calls: list[tuple[str, str, object, float]] = []
        self.fail: int | None = None
        self.resolver: object = {"id": WORK, "matchedReleaseId": RELEASES[0]}

    def get(self, path: str, parameters: Mapping[str, str], timeout: float) -> HttpResponse:
        self.calls.append(("GET", path, dict(parameters), timeout))
        if self.fail:
            return HttpResponse(self.fail, {"errors": [{"detail": "untrusted-server-body"}]}, {})
        if path.startswith("/audiobooks/external/"):
            return HttpResponse(200, self.resolver, {})
        if path.removeprefix("/books/") in self.works:
            return HttpResponse(200, self.works[path.removeprefix("/books/")], {})
        if path.removeprefix("/releases/") in self.records:
            return HttpResponse(200, self.records[path.removeprefix("/releases/")], {})
        return HttpResponse(404, {"errors": [{"status": "404"}]}, {})

    def post(self, path: str, data: Mapping[str, object], timeout: float) -> HttpResponse:
        self.calls.append(("POST", path, dict(data), timeout))
        return HttpResponse(self.fail or 200, self.hits, {})


class AudHttp:
    def __init__(self, records: list[dict[str, object]] | None = None) -> None:
        self.records = records if records is not None else [audbook()]
        self.calls: list[tuple[str, dict[str, str], float]] = []
        self.fail: int | None = None

    def get(self, path: str, parameters: Mapping[str, str], timeout: float) -> HttpResponse:
        self.calls.append((path, dict(parameters), timeout))
        if self.fail:
            return HttpResponse(self.fail, {"message": "fixture outage"}, {})
        if path == "/books/search":
            return HttpResponse(200, self.records, {})
        found = next((row for row in self.records if path == "/books/" + str(row["asin"])), None)
        return HttpResponse(200 if found else 404, found or {"message": "not found"}, {})


class LiveSourceTests(unittest.TestCase):
    def test_recorded_contract_and_breaking_changes(self) -> None:
        fixture = json.loads(
            (Path(__file__).parent / "fixtures/audiobookdb-contract.json").read_text()
        )
        validate_contract(fixture)
        changed = deepcopy(fixture)
        changed["info"]["version"] = "3.0.0"
        with self.assertRaises(SourceError):
            validate_contract(changed)
        fixture["paths"]["/search"]["post"]["responses"]["200"]["content"]["application/json"][
            "schema"
        ] = {"type": "object"}
        with self.assertRaises(SourceError):
            validate_contract(fixture)

    def test_work_discovery_expands_distinct_recordings_and_author_filter(self) -> None:
        http = DBHttp([release(i) for i in (1, 2, 3)])
        source = AudiobookDBSource(http)
        found = source.search("Lantern", "title", 10, 2)
        self.assertEqual([r.reference.record_id for r in found], list(RELEASES))
        self.assertEqual([r.edition.part_number for r in found], [1, 2, 3])
        self.assertEqual(found[1].edition.parent_work_id, "audiobookdb:" + WORK)
        self.assertEqual(found[1].edition.series_position, 5)
        self.assertEqual(found[1].edition.authors, ("Example Author",))
        self.assertEqual(found[1].recording_isbns, (ISBNS[1],))
        self.assertEqual([call[0] for call in http.calls], ["POST", "GET"])
        source.search("Example Author", "author", 10, 2)
        self.assertEqual(
            http.calls[-2][2],
            {"type": "books", "filters": {"person": "Example Author", "role": "Author"}},
        )

    def test_work_only_external_resolution_is_not_recording_identity(self) -> None:
        http = DBHttp()
        http.resolver = {"id": WORK}
        self.assertIsNone(AudiobookDBSource(http).get(ASINS[0], 2))
        self.assertEqual(len(http.calls), 1)

    def test_exact_external_and_release_detail(self) -> None:
        http = DBHttp()
        found = AudiobookDBSource(http).get(ASINS[0], 2)
        assert found
        self.assertEqual(found.reference.record_id, RELEASES[0])
        self.assertEqual(len(http.calls), 3)
        self.assertTrue(all(call[3] <= 2 for call in http.calls))
        self.assertEqual(AudiobookDBSource(http).get(RELEASES[0], 2), found)
        http.records[RELEASES[0]] = release(2)
        with self.assertRaises(SourceError):
            AudiobookDBSource(http).get(RELEASES[0], 2)

    def test_no_asin_release_and_work_isbn_never_borrowed(self) -> None:
        row = release(asin=False)
        http = DBHttp([row])
        source = AudiobookDBSource(http)
        result = source.search(ISBNS[0], "isbn", 10, 2)[0]
        self.assertIsNone(result.edition.asin)
        self.assertEqual(result.recording_isbns, (ISBNS[0],))
        del row["isbn"]
        http.work["external"] = [
            {"itemId": ISBNS[0], "category": {"title": "ISBN", "itemKind": "id"}}
        ]
        result = source.search("Lantern", "title", 10, 2)[0]
        self.assertIsNone(result.edition.isbn_13)
        self.assertEqual(result.recording_isbns, ())

    def test_missing_optional_metadata_and_bad_identifier_fields(self) -> None:
        http = DBHttp()
        http.records[RELEASES[0]] = {"id": RELEASES[0], "title": "Minimal recording"}
        result = AudiobookDBSource(http).get(RELEASES[0], 2)
        assert result
        self.assertEqual(result.edition.narrators, ())
        invalid_fields: tuple[dict[str, object], ...] = (
            {"isbn": "9780000000000"},
            {"releaseDate": "2025-01-15Tgarbage"},
            {"runtimeLengthSec": -1},
            {"people": {}},
            {"external": False},
        )
        for change in invalid_fields:
            http.records[RELEASES[0]] = {**release(), **change}
            with self.assertRaises(SourceError):
                AudiobookDBSource(http).get(RELEASES[0], 2)

    def test_malformed_envelope_and_auth_outages(self) -> None:
        http = DBHttp()
        http.hits = {"books": []}
        with self.assertRaises(SourceError):
            AudiobookDBSource(http).search("Lantern", "title", 10, 2)
        for status in (401, 403, 429, 500, 503):
            http.fail = status
            with self.assertRaisesRegex(SourceError, str(status)):
                AudiobookDBSource(http).get(RELEASES[0], 2)
        http.fail = None
        self.assertIsNone(AudiobookDBSource(http).get("R346789ABCD9", 2))

    def test_audnexus_search_detail_region_and_no_isbn_query_fallback(self) -> None:
        http = AudHttp()
        source = AudnexusSource(http, "ca")
        found = source.search("Lantern", "title", 10, 2)
        self.assertEqual(http.calls[-1][:2], ("/books/search", {"q": "Lantern", "region": "ca"}))
        self.assertEqual(found[0], source.get(ASINS[0], 2))
        self.assertEqual(http.calls[-1][1], {"region": "ca", "update": "0", "seedAuthors": "0"})
        before = len(http.calls)
        self.assertEqual(source.search(ISBNS[0], "isbn", 10, 2), ())
        self.assertEqual(len(http.calls), before)
        self.assertEqual(found[0].recording_isbns, ())
        self.assertIsNone(found[0].edition.isbn_13)
        self.assertIn(("isbn", json.dumps(ISBNS[0])), found[0].facts)
        selected = MultiSourceBackend((source,)).search("Lantern")[0]
        snapshot = EditionSnapshot(
            EditionIdentity("edition_metadata", selected.provider_id), selected
        )
        _, frozen = resolve_release(
            {
                "content_type": "audiobook",
                "source": "prowlarr",
                "source_id": "audnexus-only",
                "edition_context": EditionLocator(snapshot.identity).to_dict(),
            },
            lambda identity: snapshot,
        )
        self.assertIsNotNone(frozen)
        exported = convert_snapshot(snapshot, ExportPolicy(("https://www.audible.ca/pd/",)))
        self.assertEqual(json.loads(exported.metadata_json)["asin"], ASINS[0])
        self.assertNotIn("isbn", json.loads(exported.metadata_json))
        self.assertIsNone(source.get("B012345679", 2))
        http.records[0]["region"] = "us"
        with self.assertRaises(SourceError):
            source.get(ASINS[0], 2)

    def test_audnexus_exact_enrichment_and_missing_search_endpoint(self) -> None:
        http = AudHttp()
        source = AudnexusSource(http, "ca")
        selected = AudiobookDBSource(DBHttp()).get(RELEASES[0], 2)
        assert selected
        self.assertEqual(source.lookup(selected, 2)[0].edition.asin, ASINS[0])
        http.fail = 404
        with self.assertRaises(SourceError):
            AudnexusSource(http, "ca").search("Lantern", "title", 10, 2)

    def test_three_source_dedup_and_regional_duplicates(self) -> None:
        raw = libex_book(ASINS[0])
        raw.update({"title": release()["title"], "isbn": ISBNS[0]})
        libex = FixtureHttp()
        libex.next = HttpResponse(200, [raw], {})
        backend = MultiSourceBackend(
            (LibexSource(libex, "ca"), AudiobookDBSource(DBHttp()), AudnexusSource(AudHttp(), "ca"))
        )
        results = backend.search("Lantern")
        self.assertEqual(len(results), 1)
        assert isinstance(results[0], AggregatedEdition)
        self.assertEqual(len(results[0].provenance.references), 3)
        self.assertEqual(results[0].provenance.primary.source, "libex_live_ca")
        presentation = EditionMetadataProvider(backend).search(
            MetadataSearchOptions(query="Lantern")
        )[0]
        self.assertTrue(
            any(
                field.label == "Metadata sources" and field.value == "AudiobookDB, Audnexus"
                for field in presentation.display_fields
            )
        )
        second = audbook()
        second["region"] = "us"
        regional = MultiSourceBackend(
            (AudnexusSource(AudHttp(), "ca"), AudnexusSource(AudHttp([second]), "us"))
        )
        self.assertEqual(len(regional.search("Lantern")), 1)

    def test_different_narrators_productions_parts_and_identifiers_do_not_merge(self) -> None:
        adb = AudiobookDBSource(DBHttp()).get(RELEASES[0], 2)
        aud = AudnexusSource(AudHttp(), "ca").get(ASINS[0], 2)
        assert adb and aud
        self.assertTrue(equivalent(adb, aud))
        for other in (
            replace(aud, edition=replace(aud.edition, narrators=("Another Narrator",))),
            replace(aud, production="conventional"),
            replace(aud, edition=replace(aud.edition, part_number=2)),
            replace(aud, edition=replace(aud.edition, asin=ASINS[2])),
            replace(aud, recording_isbns=(ISBNS[1],)),
        ):
            self.assertFalse(equivalent(adb, other))
        backend = MultiSourceBackend((AudiobookDBSource(DBHttp([release(i) for i in (1, 2, 3)])),))
        self.assertEqual(len(backend.search("Lantern")), 3)

    def test_optional_outages_preserve_libex_and_independent_source_discovery(self) -> None:
        raw = libex_book(ASINS[0])
        raw["title"] = release()["title"]
        libex = FixtureHttp()
        libex.next = HttpResponse(200, [raw], {})
        adb, aud = DBHttp(), AudHttp()
        adb.fail = aud.fail = 503
        backend = MultiSourceBackend(
            (LibexSource(libex, "ca"), AudiobookDBSource(adb), AudnexusSource(aud, "ca"))
        )
        self.assertEqual(len(backend.search("Lantern")), 1)
        for source in (
            AudiobookDBSource(DBHttp([release(asin=False)])),
            AudnexusSource(AudHttp(), "ca"),
        ):
            result = MultiSourceBackend((source,)).search("Lantern")
            self.assertEqual(len(result), 1)

    def test_stable_restart_get_handoff_and_abs_export_without_asin(self) -> None:
        row = release(2, asin=False)

        def backend() -> MultiSourceBackend:
            return MultiSourceBackend((AudiobookDBSource(DBHttp([row])),))

        first = backend().search("Lantern")[0]
        self.assertEqual(decode_identity(first.edition_id).record_id, RELEASES[1])
        detail = backend().get(first.catalog, first.edition_id)
        assert detail
        self.assertEqual(detail.provider_id, first.provider_id)
        snapshot = EditionSnapshot(EditionIdentity("edition_metadata", first.provider_id), detail)
        payload, frozen = resolve_release(
            {
                "content_type": "audiobook",
                "source": "prowlarr",
                "source_id": "fixture-release-2",
                "edition_context": EditionLocator(snapshot.identity).to_dict(),
            },
            lambda identity: snapshot,
        )
        assert frozen
        self.assertEqual(frozen.snapshot.edition.part_number, 2)
        self.assertEqual(FrozenEditionContext.from_json(frozen.to_json()).snapshot, snapshot)
        self.assertEqual(payload["source_id"], "fixture-release-2")
        exported = convert_snapshot(
            snapshot, ExportPolicy(("https://audiobookdb.org/api/releases/",))
        )
        metadata = json.loads(exported.metadata_json)
        self.assertEqual(metadata["isbn"], ISBNS[1])
        self.assertNotIn("asin", metadata)
        self.assertEqual(metadata["narrators"], ["Example Cast"])

    def test_no_asin_primary_keeps_its_identity_when_verified_alias_has_asin(self) -> None:
        independent = AudiobookDBSource(DBHttp([release(asin=False)]))
        audnexus = AudnexusSource(AudHttp(), "ca")
        # Same raw Audible ISBN does not prove this is the independent recording.
        self.assertEqual(len(MultiSourceBackend((independent, audnexus)).search("Lantern")), 2)
        raw_alias = audnexus.get(ASINS[0], 2)
        assert raw_alias
        attested = replace(
            raw_alias,
            reference=SourceRef("verified_alias", ASINS[0], "ca"),
            recording_isbns=(ISBNS[0],),
        )
        alias = MockCatalogSource("verified_alias", "audnexus", (attested,), marketplace="ca")
        backend = MultiSourceBackend((independent, alias))
        results = backend.search("Lantern")
        self.assertEqual(len(results), 1)
        assert isinstance(results[0], AggregatedEdition)
        self.assertIsNone(results[0].asin)
        self.assertEqual(len(results[0].provenance.references), 2)
        detail = backend.get(results[0].catalog, results[0].edition_id)
        assert detail
        snapshot = EditionSnapshot(EditionIdentity("edition_metadata", detail.provider_id), detail)
        _, frozen = resolve_release(
            {
                "content_type": "audiobook",
                "source": "prowlarr",
                "source_id": "independent",
                "edition_context": EditionLocator(snapshot.identity).to_dict(),
            },
            lambda identity: snapshot,
        )
        self.assertIsNotNone(frozen)

    def test_unverified_alias_isbn_cannot_poison_valid_primary_get(self) -> None:
        primary = release()
        del primary["isbn"]
        libex = FixtureHttp()
        raw = libex_book(ASINS[0])
        raw["title"] = primary["title"]
        libex.next = HttpResponse(200, [raw], {})
        backend = MultiSourceBackend(
            (AudiobookDBSource(DBHttp([primary])), LibexSource(libex, "ca")),
            AggregationOptions(source_priority=("audiobookdb", "libex_live")),
        )
        original = backend.search("Lantern")[0]
        self.assertIsNone(original.isbn_13)
        libex.next = HttpResponse(200, raw, {})
        detail = backend.get(original.catalog, original.edition_id)
        assert detail
        self.assertIsNone(detail.isbn_13)
        self.assertEqual(detail.asin, ASINS[0])
        snapshot = EditionSnapshot(EditionIdentity("edition_metadata", detail.provider_id), detail)
        _, frozen = resolve_release(
            {
                "content_type": "audiobook",
                "source": "prowlarr",
                "source_id": "unpoisoned",
                "edition_context": EditionLocator(snapshot.identity).to_dict(),
            },
            lambda identity: snapshot,
        )
        self.assertIsNotNone(frozen)

    def test_request_budget_counts_inner_http_fanout(self) -> None:
        http = DBHttp()
        source = AudiobookDBSource(http)
        with self.assertRaises(SourceError):
            MultiSourceBackend((source,), AggregationOptions(max_search_calls=3)).search("Lantern")
        self.assertEqual(http.calls, [])
        self.assertEqual(
            len(
                MultiSourceBackend((source,), AggregationOptions(max_search_calls=4)).search(
                    "Lantern"
                )
            ),
            1,
        )
        self.assertLessEqual(len(http.calls), 4)

    def test_audiobookdb_fanout_is_bounded_and_reports_truncation(self) -> None:
        http = DBHttp()
        keys = (WORK, "W346789ABCD4", "W346789ABCD6")
        for part, key in enumerate(keys, 1):
            recording = release(part)
            recording["book"] = {"id": key, "title": "Lantern"}
            doc = work([recording])
            doc["id"] = key
            http.works[key] = doc
        http.hits = [{"id": key} for key in (*keys, "W346789ABCD7")]
        found = AudiobookDBSource(http).search("Lantern", "title", 10, 2)
        self.assertEqual(len(found), 3)
        self.assertEqual(len(http.calls), 4)
        self.assertTrue(all("discovery window reached" in r.warnings[0] for r in found))

    def test_new_source_priority_is_configurable_without_rebinding_old_ids(self) -> None:
        sources = (AudiobookDBSource(DBHttp()), AudnexusSource(AudHttp(), "ca"))
        first = MultiSourceBackend(sources).search("Lantern")[0]
        prioritized = MultiSourceBackend(
            sources, AggregationOptions(source_priority=("audnexus", "audiobookdb"))
        )
        result = prioritized.search("Lantern")[0]
        self.assertEqual(decode_identity(result.edition_id).source, "audnexus_ca")
        restored = prioritized.get(first.catalog, first.edition_id)
        assert restored
        self.assertEqual(restored.provider_id, first.provider_id)

    def test_native_get_freezes_exact_new_source_parts_with_no_download_worker(self) -> None:
        with ExitStack() as stack:
            provider = EditionMetadataProvider(
                MultiSourceBackend(
                    (AudiobookDBSource(DBHttp([release(1), release(2, asin=False), release(3)])),)
                )
            )
            stack.enter_context(
                patch("shelfmark.metadata_providers.get_provider", return_value=provider)
            )
            queue = BookQueue()
            stack.enter_context(patch.object(orchestrator, "book_queue", queue))
            stack.enter_context(
                patch.object(orchestrator, "_source_unavailable_message", return_value=None)
            )
            stack.enter_context(
                patch.object(orchestrator, "_build_retry_resolution_fields", return_value={})
            )
            stack.enter_context(patch.object(orchestrator, "ws_manager", None))
            config = stack.enter_context(patch.object(orchestrator, "config"))
            config.get.return_value = "folder"
            harness = AppHarness()
            harness.app.view_functions.pop("api_download_release")
            harness.app.view_functions.pop("api_inspect_release")
            register_release_routes(
                harness.app,
                harness.login,
                resolve_auth_mode=lambda: harness.mode,
                get_user_db=lambda: harness.db,
                load_policy_settings=lambda: harness.settings,
                resolve_status_scope=lambda **kwargs: (False, 2, True),
                attach_library_ownership=lambda book, data: None,
            )
            client = harness.app.test_client()
            with client.session_transaction() as session:
                session["user_id"], session["db_user_id"], session["is_admin"] = (
                    "fixture-user",
                    2,
                    False,
                )
            books = provider.search(MetadataSearchOptions(query="Lantern"))
            self.assertEqual(len(books), 3)
            for book in books:
                selected = provider.get_snapshot(book.provider_id)
                assert selected and selected.edition.part_number
                part = selected.edition.part_number
                task_id = f"new-source-part-{part}"
                response = client.post(
                    "/api/releases/download",
                    json={
                        "source": "prowlarr",
                        "source_id": task_id,
                        "title": book.title,
                        "release_title": book.title,
                        "author": "Example Author",
                        "language": "en",
                        "content_type": "audiobook",
                        "format": "m4b",
                        "edition_context": EditionLocator(selected.identity).to_dict(),
                    },
                )
                self.assertEqual(response.status_code, 200, response.get_json())
                task = queue.get_task(task_id)
                assert task
                frozen = context_from_task(task)
                assert frozen
                self.assertEqual(frozen.snapshot.identity, selected.identity)
                self.assertEqual(frozen.snapshot.edition.isbn_13, ISBNS[part - 1])
                self.assertEqual(frozen.snapshot.edition.part_number, part)
                restored = orchestrator._restore_task_from_retry_payload(
                    orchestrator.serialize_task_for_retry(task)
                )
                assert restored
                self.assertEqual(context_from_task(restored), frozen)

    def test_no_asin_ownership_requires_recording_isbn_and_part_corroboration(self) -> None:
        provider = EditionMetadataProvider(
            MultiSourceBackend((AudiobookDBSource(DBHttp([release(2, asin=False)])),))
        )
        book = provider.search(MetadataSearchOptions(query="Lantern"))[0]
        metadata = {
            "title": book.title,
            "isbn": ISBNS[1],
            "narrators": ["Example Cast"],
            "publisher": "Fixture publisher",
            "language": "English",
        }
        raw = {"media": {"metadata": metadata}}
        entry = audiobookshelf._item_to_entry(raw)
        object.__setattr__(entry, "_edition_recording", library_recording(raw))
        self.assertEqual(match_edition_entries(book, [entry]), "owned")
        metadata["title"] = "Lantern (Part 1 of 3) [Dramatized Adaptation]"
        object.__setattr__(entry, "_edition_recording", library_recording(raw))
        self.assertIsNone(match_edition_entries(book, [entry]))

    def test_source_defaults_independent_optins_and_key_redaction(self) -> None:
        with patch(
            "shelfmark.metadata_providers.editions.multisource_config.HttpClient",
            side_effect=AssertionError("No client"),
        ):
            self.assertFalse(MultiSourceSettings.from_mapping({}).backend().is_available())
        base: dict[str, object] = {"EDITION_AUDIOBOOKDB_ENABLED": True}
        settings = MultiSourceSettings.from_mapping(
            {**base, "EDITION_AUDIOBOOKDB_API_KEY": "synthetic-private-key"}
        )
        self.assertNotIn("synthetic-private-key", repr(settings))
        self.assertTrue(settings.backend().is_available())
        with self.assertRaises(ValueError):
            MultiSourceSettings.from_mapping({"EDITION_AUDNEXUS_ENABLED": True}).backend()
        aud = MultiSourceSettings.from_mapping(
            {
                "EDITION_AUDNEXUS_ENABLED": True,
                "EDITION_AUDNEXUS_URL": "http://audnexus.example.invalid",
            }
        ).backend()
        self.assertTrue(aud.is_available())

    def test_configured_audiobookdb_supplies_user_agent_without_extra_setting(self) -> None:
        from shelfmark.metadata_providers.editions.multisource_config import _client

        _client.cache_clear()
        response = MagicMock()
        response.__enter__.return_value = response
        response.status, response.headers, response.read.return_value = 200, {}, b"[]"
        with patch("shelfmark.metadata_providers.editions.source_http.build_opener") as opener:
            opener.return_value.open.return_value = response
            backend = MultiSourceSettings.from_mapping(
                {
                    "EDITION_AUDIOBOOKDB_ENABLED": True,
                    "EDITION_AUDIOBOOKDB_API_KEY": "synthetic-header-key",
                }
            ).backend()
            self.assertEqual(backend.search("Fictional title"), [])
            request = opener.return_value.open.call_args.args[0]
            self.assertEqual(request.get_header("User-agent"), "Shelfmark/editions")
            self.assertEqual(request.get_header("X-api-key"), "synthetic-header-key")
        _client.cache_clear()

    def test_http_post_headers_no_key_in_payload_and_no_credential_redirect(self) -> None:
        response = MagicMock()
        response.__enter__.return_value = response
        response.status = 200
        response.headers = {}
        response.read.return_value = b"[]"
        with patch("shelfmark.metadata_providers.editions.source_http.build_opener") as opener:
            opener.return_value.open.return_value = response
            client = HttpClient(
                "https://example.invalid/api",
                api_key="synthetic-private-key",
                user_agent="ShelfmarkFixture/1.0 (contact@example.invalid)",
                min_interval=0,
            )
            client.post("/search", {"q": "Lantern", "type": "books"}, 2)
            request = opener.return_value.open.call_args.args[0]
            self.assertEqual(request.get_method(), "POST")
            self.assertEqual(request.get_header("X-api-key"), "synthetic-private-key")
            self.assertNotIn(b"synthetic-private-key", request.data)
            with self.assertRaises(SourceError):
                _NoCredentialRedirect().redirect_request(
                    request, None, 302, "Moved", {}, "https://elsewhere.invalid/"
                )
        with self.assertRaises(ValueError):
            HttpClient("http://example.invalid", api_key="key")
        with self.assertRaises(ValueError):
            HttpClient("http://example.invalid", user_agent="bad\r\nheader")

    def test_post_timeout_malformed_json_and_retry_after_are_bounded(self) -> None:
        client = HttpClient("https://example.invalid", min_interval=0)
        with patch(
            "shelfmark.metadata_providers.editions.source_http.urlopen",
            side_effect=TimeoutError("private socket detail"),
        ):
            with self.assertRaisesRegex(SourceError, "Metadata service unavailable"):
                client.post("/search", {"q": "Lantern"}, 1)
        response = MagicMock()
        response.__enter__.return_value = response
        response.status = 200
        response.headers = {}
        for body in (b"not-json", b'{"id":1,"id":2}'):
            response.read.return_value = body
            with patch(
                "shelfmark.metadata_providers.editions.source_http.urlopen", return_value=response
            ):
                with self.assertRaises(SourceError):
                    client.post("/search", {"q": "Lantern"}, 1)
        response.status = 429
        response.headers = {"Retry-After": "2"}
        with patch(
            "shelfmark.metadata_providers.editions.source_http.urlopen", return_value=response
        ) as request:
            with self.assertRaises(RateLimited) as error:
                client.post("/search", {"q": "Lantern"}, 1)
            self.assertEqual(error.exception.retry_after, 2)
            with self.assertRaises(RateLimited):
                client.post("/search", {"q": "Lantern"}, 0.001)
            self.assertEqual(request.call_count, 1)


if __name__ == "__main__":
    unittest.main()

"""Native queue/request/history tests; fixture sources and /tmp databases only."""

import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier
from typing import cast
from unittest.mock import MagicMock, patch

from app_harness import AppHarness
from mock_backend import MOCK_EDITIONS
from test_search_refinement import RegionalHttp, culling_books, regional_sources

from shelfmark.core import auto_download, library_index, request_routes, requests_service
from shelfmark.core.download_history_service import DownloadHistoryService
from shelfmark.core.download_recording import register_download_recording
from shelfmark.core.edition_handoff import (
    MAX_CONTEXT_BYTES,
    EditionLocator,
    FrozenEditionContext,
    bind_request_locator,
    context_from_task,
)
from shelfmark.core.models import DownloadTask, QueueStatus
from shelfmark.core.queue import BookQueue
from shelfmark.core.release_routes import register_release_routes
from shelfmark.core.user_db import UserDB
from shelfmark.download import orchestrator
from shelfmark.metadata_providers import BookMetadata, MetadataSearchOptions
from shelfmark.metadata_providers.editions.catalog_types import AggregatedEdition
from shelfmark.metadata_providers.editions.identity import EditionIdentity
from shelfmark.metadata_providers.editions.multisource import MultiSourceBackend
from shelfmark.metadata_providers.editions.provider import EditionMetadataProvider
from shelfmark.metadata_providers.editions.snapshot import EditionSnapshot
from shelfmark.release_sources import Release


class HandoffTests(unittest.TestCase):
    def setUp(self) -> None:
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.provider = EditionMetadataProvider(
            MultiSourceBackend(regional_sources(RegionalHttp()))
        )
        self.stack.enter_context(
            patch("shelfmark.metadata_providers.get_provider", return_value=self.provider)
        )
        self.queue = BookQueue()  # Native queue implementation; no worker/coordinator.
        self.stack.enter_context(patch.object(orchestrator, "book_queue", self.queue))
        self.stack.enter_context(
            patch.object(orchestrator, "_source_unavailable_message", return_value=None)
        )
        self.stack.enter_context(
            patch.object(
                orchestrator,
                "_build_retry_resolution_fields",
                return_value={
                    "retry_download_url": "https://example.invalid/synthetic.torrent",
                    "retry_download_protocol": "torrent",
                    "can_retry_without_staged_source": True,
                    "retry_source_context": {"fixture": True},
                },
            )
        )
        self.stack.enter_context(patch.object(orchestrator, "ws_manager", None))
        config = self.stack.enter_context(patch.object(orchestrator, "config"))
        config.get.return_value = "folder"
        self.books: dict[int, BookMetadata] = {}
        for book in self.provider.search(MetadataSearchOptions(query="Lantern")):
            snapshot = self.provider.get_snapshot(book.provider_id)
            assert snapshot is not None and snapshot.edition.part_number is not None
            self.books[snapshot.edition.part_number] = book
        self.assertEqual(set(self.books), {1, 2, 3})

    def payload(
        self, part: int = 1, source: str = "prowlarr", suffix: str = ""
    ) -> dict[str, object]:
        book = self.books[part]
        return {
            "source": source,
            "source_id": f"fixture-{source}-{part}-{suffix}",
            "title": book.title,
            "release_title": book.title,
            "content_type": "audiobook",
            "author": "Example Author",
            "language": "en",
            "format": "m4b",
            "protocol": "torrent",
            "download_url": "https://example.invalid/synthetic.torrent",
            "extra": {"client": "qbittorrent-style fixture"},
            "edition_context": EditionLocator(
                EditionIdentity(book.provider, book.provider_id)
            ).to_dict(),
        }

    def task(self, payload: dict[str, object]) -> DownloadTask:
        task = self.queue.get_task(cast("str", payload["source_id"]))
        assert task is not None
        return task

    def database(self) -> UserDB:
        root = self.stack.enter_context(TemporaryDirectory())
        db = UserDB(str(Path(root) / "native-test.sqlite3"))
        db.initialize()
        self.admin_id = cast("int", db.create_user("fixture-admin", role="admin")["id"])
        return db

    def request(
        self, db: UserDB, part: int = 1, *, release_level: bool = False
    ) -> dict[str, object]:
        user = db.create_user(f"fictional-user-{part}")
        book = self.books[part]
        return cast(
            "dict[str, object]",
            requests_service.create_request(
                db,
                user_id=user["id"],
                source_hint="prowlarr",
                content_type="audiobook",
                request_level="release" if release_level else "book",
                policy_mode="request_release" if release_level else "request_book",
                book_data={
                    "provider": book.provider,
                    "provider_id": book.provider_id,
                    "title": book.title,
                    "author": "Example Author",
                    "content_type": "audiobook",
                },
                release_data=self.payload(part) if release_level else None,
            ),
        )

    def test_three_parts_two_sources_queue_independently_before_history_hook(self) -> None:
        seen: list[tuple[str, int | None]] = []

        def queued(task_id: str, task: DownloadTask) -> None:
            context = context_from_task(task)
            assert context is not None
            seen.append((task_id, context.snapshot.edition.part_number))

        self.queue.set_queue_hook(queued)
        for source in ("prowlarr", "audiobookbay"):
            for part in (1, 2, 3):
                payload = self.payload(part, source)
                self.assertEqual(orchestrator.queue_release(payload, user_id=part), (True, None))
                task = self.task(payload)
                context = context_from_task(task)
                assert context is not None
                self.assertEqual(context.snapshot.edition.part_number, part)
                self.assertEqual(
                    context.snapshot.identity.provider_id, self.books[part].provider_id
                )
                self.assertEqual((task.source, task.task_id), (source, payload["source_id"]))
                self.assertEqual(task.user_id, part)
        self.assertEqual(len(seen), 6)

    def test_verified_regional_asins_all_survive_native_retry(self) -> None:
        for part in (1, 2, 3):
            payload = self.payload(part)
            self.assertTrue(orchestrator.queue_release(payload)[0])
            task = self.task(payload)
            restored = orchestrator._restore_task_from_retry_payload(
                orchestrator.serialize_task_for_retry(task)
            )
            assert restored is not None
            self.assertEqual(restored.output_args, task.output_args)
            context = context_from_task(restored)
            assert context is not None and isinstance(context.snapshot.edition, AggregatedEdition)
            asins = {
                json.loads(value.value_json)
                for value in context.snapshot.edition.provenance.values
                if value.field == "asin"
            }
            self.assertEqual(
                asins, {f"B0{market}0000{part:02d}" for market in ("CA", "US", "UK", "AU")}
            )

    def test_identical_canonical_titles_never_replace_independent_snapshots(self) -> None:
        snapshots: dict[str, EditionSnapshot] = {}
        for book in self.books.values():
            original = self.provider.get_snapshot(book.provider_id)
            assert original is not None
            snapshots[book.provider_id] = EditionSnapshot(
                original.identity, replace(original.edition, canonical_title="The Lantern Archive")
            )
        with patch.object(self.provider, "get_snapshot", side_effect=lambda key: snapshots[key]):
            for part in (1, 2, 3):
                self.assertTrue(orchestrator.queue_release(self.payload(part))[0])
        contexts = [context_from_task(self.task(self.payload(part))) for part in (1, 2, 3)]
        self.assertEqual(
            {c.snapshot.canonical_title for c in contexts if c}, {"The Lantern Archive"}
        )
        self.assertEqual(len({c.snapshot.identity for c in contexts if c}), 3)

    def test_concurrent_users_have_no_shared_selection_state(self) -> None:
        barrier = Barrier(3)

        def submit(part: int) -> tuple[bool, str | None]:
            barrier.wait(timeout=10)
            return orchestrator.queue_release(
                self.payload(part), user_id=100 + part, username=f"fixture-{part}"
            )

        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(submit, (1, 2, 3)))
        self.assertEqual(results, [(True, None)] * 3)
        for part in (1, 2, 3):
            task = self.task(self.payload(part))
            context = context_from_task(task)
            assert context is not None
            self.assertEqual(
                (task.user_id, context.snapshot.edition.part_number), (100 + part, part)
            )

    def test_duplicate_source_id_does_not_reassign_existing_user_or_edition(self) -> None:
        first, second = self.payload(1), self.payload(2)
        second["source_id"] = first["source_id"]
        self.assertTrue(orchestrator.queue_release(first, user_id=11)[0])
        original = self.task(first)
        frozen = dict(original.output_args)
        self.assertFalse(orchestrator.queue_release(second, user_id=22)[0])
        self.assertIs(self.task(first), original)
        self.assertEqual((original.output_args, original.user_id), (frozen, 11))

    def test_native_book_requests_and_admin_approvals_use_stored_identity(self) -> None:
        db = self.database()
        for part in (1, 2, 3):
            row = self.request(db, part)
            release = self.payload(part)
            del release["edition_context"]  # Legacy approval still has stored book identity.
            result = requests_service.fulfil_request(
                db,
                request_id=cast("int", row["id"]),
                admin_user_id=self.admin_id,
                queue_release=orchestrator.queue_release,
                release_data=release,
            )
            task = self.task(release)
            context = context_from_task(task)
            assert context is not None
            self.assertEqual(context.snapshot.identity.provider_id, self.books[part].provider_id)
            self.assertEqual((task.request_id, task.user_id), (row["id"], row["user_id"]))
            self.assertEqual(result["status"], "fulfilled")

    def test_native_release_request_approval_keeps_context(self) -> None:
        db = self.database()
        row = self.request(db, 2, release_level=True)
        requests_service.fulfil_request(
            db,
            request_id=cast("int", row["id"]),
            admin_user_id=self.admin_id,
            queue_release=orchestrator.queue_release,
        )
        context = context_from_task(self.task(self.payload(2)))
        assert context is not None
        self.assertEqual(context.snapshot.edition.part_number, 2)

    def test_conflicting_approval_rejected_before_native_claim(self) -> None:
        db = self.database()
        row = self.request(db, 1)
        with self.assertRaises(requests_service.RequestServiceError) as error:
            requests_service.fulfil_request(
                db,
                request_id=cast("int", row["id"]),
                admin_user_id=self.admin_id,
                queue_release=orchestrator.queue_release,
                release_data=self.payload(2),
            )
        self.assertEqual(error.exception.code, "edition_context_invalid")
        pending = db.get_request(cast("int", row["id"]))
        assert pending is not None
        self.assertEqual(pending["status"], "pending")
        self.assertIsNone(self.queue.get_task(cast("str", self.payload(2)["source_id"])))

    def test_immediate_request_policy_branch_binds_prepared_book(self) -> None:
        db = self.database()
        row = self.request(db, 3)
        release = self.payload(3)
        del release["edition_context"]
        args = {
            "book_data": row["book_data"],
            "release_data": release,
            "user_id": row["user_id"],
            "content_type": "audiobook",
        }
        request_routes._queue_prepared_download_submission(
            db, queue_release=orchestrator.queue_release, create_args=args, request_title="fixture"
        )
        context = context_from_task(self.task(release))
        assert context is not None
        self.assertEqual(context.snapshot.edition.part_number, 3)

    def test_auto_request_path_uses_native_fulfilment_without_auto_hook(self) -> None:
        db = self.database()
        row = self.request(db, 2)
        release = Release(
            source="prowlarr",
            source_id="fixture-auto",
            title="Unqualified indexer title",
            content_type="audiobook",
            format="m4b",
        )
        with (
            patch.object(library_index, "is_in_library", return_value=False) as owned,
            patch.object(
                auto_download, "search_source_releases", return_value=(None, [release], None)
            ),
            patch.object(auto_download, "strict_match", return_value=True),
            patch.object(auto_download, "_inspect_candidate", return_value=(False, True)),
            patch.object(auto_download, "_acted_on_since_listed", return_value=False),
        ):
            result = auto_download.auto_download_request(
                db,
                row,
                sources=["prowlarr"],
                content_type="audiobook",
                min_seeders=0,
                queue_release=orchestrator.queue_release,
                admin_user_id=self.admin_id,
            )
        self.assertEqual(result.status, "queued")
        self.assertTrue(owned.call_args.kwargs["strict"])
        task = self.queue.get_task("fixture-auto")
        assert task is not None
        context = context_from_task(task)
        assert context is not None
        self.assertEqual(context.snapshot.edition.part_number, 2)
        self.assertEqual(task.title, self.books[2].title)

    def test_native_history_hook_and_restart_retain_frozen_context(self) -> None:
        db = self.database()
        user = db.create_user("history-fixture")
        history = DownloadHistoryService(db.db_path)
        register_download_recording(
            get_user_db=lambda: db,
            get_download_history_service=lambda: history,
            get_activity_view_state_service=lambda: None,
            emit_request_update_events=lambda rows: None,
            ws_manager=None,
        )
        payload = self.payload(3)
        self.assertTrue(orchestrator.queue_release(payload, user_id=user["id"])[0])
        task = self.task(payload)
        record = history.get_by_task_id(task.task_id)
        assert record is not None
        self.assertNotIn("output_args", orchestrator._task_to_dict(task))
        self.assertNotIn("edition_context", orchestrator._task_to_dict(task))
        self.assertNotIn("retry_payload", history.to_download_payload(record))
        frozen = dict(task.output_args)
        self.queue.update_status(task.task_id, QueueStatus.ERROR)
        persisted = DownloadHistoryService(db.db_path).get_by_task_id(task.task_id)
        assert persisted is not None
        restored = orchestrator._restore_task_from_retry_payload(persisted["retry_payload"])
        assert restored is not None
        self.assertEqual(restored.output_args, frozen)
        self.assertEqual(context_from_task(restored), context_from_task(task))
        # A new in-memory native queue simulates the process losing its tasks.
        fresh = BookQueue()
        with (
            patch.object(orchestrator, "book_queue", fresh),
            patch.object(
                self.provider,
                "get_snapshot",
                side_effect=AssertionError("No catalog refresh on retry"),
            ),
        ):
            self.assertTrue(
                orchestrator.retry_persisted_download(
                    persisted["retry_payload"], final_status="error"
                )[0]
            )
        retried = fresh.get_task(task.task_id)
        assert retried is not None
        self.assertEqual(retried.output_args, frozen)
        stored_args = cast("dict[str, object]", record["retry_payload"])["output_args"]
        assert isinstance(stored_args, dict)
        self.assertIn("edition_context", stored_args)

    def test_in_memory_retry_retains_original_snapshot_without_lookup(self) -> None:
        payload = self.payload(1)
        self.assertTrue(orchestrator.queue_release(payload)[0])
        task = self.task(payload)
        frozen = dict(task.output_args)
        self.queue.update_status(task.task_id, QueueStatus.ERROR)
        with patch.object(
            self.provider, "get_snapshot", side_effect=AssertionError("Unexpected metadata lookup")
        ):
            self.assertTrue(orchestrator.retry_download(task.task_id)[0])
        self.assertEqual(self.task(payload).output_args, frozen)

    def test_queue_false_after_insertion_is_not_retried_or_treated_as_empty(self) -> None:
        manager = MagicMock()
        manager.broadcast_status_update.side_effect = OSError(
            "fixture broadcast failed after queue.add"
        )
        seen: list[str] = []
        self.queue.set_queue_hook(lambda task_id, task: seen.append(task_id))
        with patch.object(orchestrator, "ws_manager", manager):
            self.assertFalse(orchestrator.queue_release(self.payload(1))[0])
        self.assertEqual(len(seen), 1)
        self.assertIsNotNone(context_from_task(self.task(self.payload(1))))

    def test_native_request_rollback_can_coexist_with_ambiguous_inserted_job(self) -> None:
        db = self.database()
        row = self.request(db, 1)
        manager = MagicMock()
        manager.broadcast_status_update.side_effect = OSError("fixture post-insertion failure")
        seen: list[str] = []
        self.queue.set_queue_hook(lambda task_id, task: seen.append(task_id))
        with patch.object(orchestrator, "ws_manager", manager):
            with self.assertRaises(requests_service.RequestServiceError):
                requests_service.fulfil_request(
                    db,
                    request_id=cast("int", row["id"]),
                    admin_user_id=self.admin_id,
                    queue_release=orchestrator.queue_release,
                    release_data=self.payload(1),
                )
        pending = db.get_request(cast("int", row["id"]))
        assert pending is not None
        self.assertEqual(
            pending["status"], "pending"
        )  # Native limitation, not a no-insertion guarantee.
        self.assertEqual(len(seen), 1)
        self.assertIsNotNone(context_from_task(self.task(self.payload(1))))

    def test_queue_source_failure_and_request_rollback(self) -> None:
        db = self.database()
        row = self.request(db, 1)
        with patch.object(
            orchestrator, "_source_unavailable_message", return_value="Fixture source unavailable"
        ):
            with self.assertRaises(requests_service.RequestServiceError):
                requests_service.fulfil_request(
                    db,
                    request_id=cast("int", row["id"]),
                    admin_user_id=self.admin_id,
                    queue_release=orchestrator.queue_release,
                    release_data=self.payload(1),
                )
        pending = db.get_request(cast("int", row["id"]))
        assert pending is not None
        self.assertEqual(pending["status"], "pending")
        self.assertIsNone(self.queue.get_task(cast("str", self.payload(1)["source_id"])))

    def test_missing_tampered_or_forged_locator_never_dispatches(self) -> None:
        locator = cast("dict[str, object]", self.payload()["edition_context"])
        variants: tuple[object, ...] = (
            None,
            "serialized snapshot",
            {},
            {**locator, "version": True},
            {**locator, "version": 2},
            {**locator, "provider_id": ""},
            {**locator, "provider_id": "missing:edition"},
            {**locator, "snapshot": {}},
            {**locator, "owner": "admin"},
        )
        for raw in variants:
            with self.subTest(raw=raw):
                payload = {**self.payload(), "edition_context": raw}
                self.assertFalse(orchestrator.queue_release(payload)[0])
                self.assertIsNone(self.queue.get_task(cast("str", payload["source_id"])))

    def test_wrong_detail_identity_and_unavailable_provider(self) -> None:
        other = self.provider.get_snapshot(self.books[2].provider_id)
        with patch.object(self.provider, "get_snapshot", return_value=other):
            self.assertFalse(orchestrator.queue_release(self.payload(1))[0])
        with patch.object(self.provider, "is_available", return_value=False):
            self.assertFalse(orchestrator.queue_release(self.payload(1))[0])

    def test_advisory_title_detects_parts_style_and_language_but_not_identity(self) -> None:
        for updates in (
            {"release_title": "Lantern Part 2 of 3"},
            {"release_title": "Lantern Part 1 of 2"},
            {"release_title": "Lantern single-narrator recording"},
            {"language": "fr"},
        ):
            with self.subTest(updates=updates):
                self.assertFalse(orchestrator.queue_release({**self.payload(1), **updates})[0])
        # A plausible title does not repair a nonexistent metadata ID.
        with patch.object(self.provider, "get_snapshot", return_value=None):
            self.assertFalse(orchestrator.queue_release(self.payload(1))[0])
        # Unabridged is not proof of conventional narration.
        self.assertTrue(
            orchestrator.queue_release(
                {**self.payload(1), "release_title": "Unabridged Part 1 of 3"}
            )[0]
        )

    def test_missing_locator_is_legacy_download_no_identity_guess(self) -> None:
        payload = self.payload(1)
        del payload["edition_context"]
        payload["source_id"] = self.books[1].provider_id
        with patch.object(
            self.provider, "get_snapshot", side_effect=AssertionError("Must not infer identity")
        ):
            self.assertTrue(orchestrator.queue_release(payload)[0])
        self.assertEqual(self.task(payload).output_args, {})

    def test_ordinary_provider_remains_native(self) -> None:
        native = {
            **self.payload(1),
            "edition_context": {"version": 1, "provider": "googlebooks", "provider_id": "native"},
        }
        with patch.object(
            self.provider, "get_snapshot", side_effect=AssertionError("Not our provider")
        ):
            self.assertTrue(orchestrator.queue_release(native)[0])
        self.assertEqual(self.task(native).output_args, {})

    def test_pre_resolved_keyword_cannot_bypass_adapter(self) -> None:
        with self.assertRaises(TypeError):
            orchestrator.queue_release(self.payload(), edition_context="forged snapshot")
        self.assertIsNone(self.queue.get_task(cast("str", self.payload()["source_id"])))

    def test_context_size_and_incomplete_detail_fail_before_add(self) -> None:
        original = self.provider.get_snapshot(self.books[1].provider_id)
        assert original is not None and isinstance(original.edition, AggregatedEdition)
        for edition in (
            replace(original.edition, description="Ã©" * MAX_CONTEXT_BYTES),
            replace(
                original.edition,
                provenance=replace(
                    original.edition.provenance,
                    warnings=("Libex response incomplete: identity fields missing",),
                ),
            ),
        ):
            with patch.object(
                self.provider,
                "get_snapshot",
                return_value=EditionSnapshot(original.identity, edition),
            ):
                self.assertFalse(orchestrator.queue_release(self.payload())[0])
        self.assertIsNone(self.queue.get_task(cast("str", self.payload()["source_id"])))

    def test_dark_age_with_optional_catalog_outage_reaches_native_queue(self) -> None:
        snapshot = EditionSnapshot.from_json(
            (Path(__file__).parent / "fixtures/dark-age.snapshot.json").read_text()
        )
        payload = {
            **self.payload(),
            "title": "Dark Age (Part 1 of 3) by Pierce Brown [ENG / M4B]",
            "release_title": "Dark Age (Part 1 of 3) by Pierce Brown [ENG / M4B]",
            "edition_context": EditionLocator(snapshot.identity).to_dict(),
        }
        with patch.object(self.provider, "get_snapshot", return_value=snapshot):
            self.assertTrue(orchestrator.queue_release(payload)[0])
            task = self.task(payload)
            context = context_from_task(task)
            assert context is not None and isinstance(context.snapshot.edition, AggregatedEdition)
            self.assertEqual(context.snapshot, snapshot)
            self.assertEqual(
                context.snapshot.edition.provenance.warnings, ("libex_live_uk: RateLimited",)
            )
            self.assertEqual(
                (
                    context.snapshot.edition.asin,
                    context.snapshot.edition.isbn_13,
                    context.snapshot.edition.part_number,
                    context.snapshot.edition.part_total,
                ),
                ("B0FF5F3BJC", "9798890557254", 1, 3),
            )
            self.assertEqual(task.title, snapshot.edition_title)
            conflict = {
                **payload,
                "source_id": "conflicting-dark-age",
                "release_title": "Dark Age Part 2",
            }
            self.assertFalse(orchestrator.queue_release(conflict)[0])
            self.assertIsNone(self.queue.get_task("conflicting-dark-age"))

    def test_informational_historical_notice_is_preserved_not_a_live_lookup_failure(self) -> None:
        original = self.provider.get_snapshot(self.books[1].provider_id)
        assert original is not None and isinstance(original.edition, AggregatedEdition)
        edition = replace(
            original.edition,
            provenance=replace(
                original.edition.provenance,
                warnings=("Historical delisting report; current availability unverified",),
            ),
        )
        with patch.object(
            self.provider, "get_snapshot", return_value=EditionSnapshot(original.identity, edition)
        ):
            self.assertTrue(orchestrator.queue_release(self.payload())[0])
        context = context_from_task(self.task(self.payload()))
        assert context is not None and isinstance(context.snapshot.edition, AggregatedEdition)
        self.assertEqual(context.snapshot.edition.provenance.warnings, edition.provenance.warnings)

    def test_unknown_release_language_is_not_a_known_conflict(self) -> None:
        payload = {**self.payload(), "language": "und"}
        self.assertTrue(orchestrator.queue_release(payload)[0])
        context = context_from_task(self.task(payload))
        assert context is not None
        self.assertEqual(context.snapshot.edition.language, "english")

    def test_frozen_schema_roundtrip_missing_identifiers_and_release_binding(self) -> None:
        edition = replace(MOCK_EDITIONS[0], isbn_10=None, isbn_13=None, asin=None)
        snapshot = EditionSnapshot(
            EditionIdentity("edition_metadata", edition.provider_id), edition
        )
        original = FrozenEditionContext(
            snapshot.to_json(), "audiobookbay", "release-not-an-edition-ID"
        )
        self.assertEqual(FrozenEditionContext.from_json(original.to_json()), original)
        self.assertIsNone(original.snapshot.edition.asin)
        with self.assertRaises(ValueError):
            original.validate_release("prowlarr", original.source_id)
        data = json.loads(original.to_json())
        data["provider_id"] = "another:edition"
        with self.assertRaises(ValueError):
            FrozenEditionContext.from_json(json.dumps(data))
        with self.assertRaises(ValueError):
            FrozenEditionContext.from_json('{"version":1,"version":1}')

    def test_original_helper_does_not_reconstruct_book_id_or_trust_owner(self) -> None:
        book = {"provider": "edition_metadata", "id": self.books[1].provider_id}
        with self.assertRaises(ValueError):
            bind_request_locator(book, self.payload())
        payload = self.payload()
        payload["user_id"] = 999
        payload["is_admin"] = True
        self.assertTrue(
            orchestrator.queue_release(payload, user_id=21, username="authenticated-user")[0]
        )
        self.assertEqual(self.task(payload).user_id, 21)
        context = context_from_task(self.task(payload))
        assert context is not None
        self.assertNotIn("authenticated-user", context.to_json())

    def test_culling_trials_native_get_freezes_each_recording_and_all_regional_asins(self) -> None:
        from shelfmark.core.edition_ownership import ownership_evidence
        from shelfmark.download.outputs.abs_metadata import ExportPolicy, convert_snapshot

        http = RegionalHttp()
        http.books = culling_books()
        provider = EditionMetadataProvider(MultiSourceBackend(regional_sources(http)))
        self.stack.enter_context(
            patch("shelfmark.metadata_providers.get_provider", return_value=provider)
        )
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
        books = provider.search(MetadataSearchOptions(query="The Culling Trials: Book 1"))
        self.assertEqual(len(books), 2)
        for book in books:
            snapshot = provider.get_snapshot(book.provider_id)
            assert snapshot
            task_id = "synthetic-culling-" + str(snapshot.edition.asin)
            payload: dict[str, object] = {
                "source": "prowlarr",
                "source_id": task_id,
                "title": book.title,
                "release_title": book.title,
                "language": "en",
                "content_type": "audiobook",
                "format": "m4b",
                "edition_context": EditionLocator(snapshot.identity).to_dict(),
            }
            response = client.post("/api/releases/download", json=payload)
            self.assertEqual(response.status_code, 200, response.get_json())
            task = self.task(payload)
            frozen = context_from_task(task)
            assert frozen
            self.assertEqual(frozen.snapshot.identity, snapshot.identity)
            self.assertEqual(frozen.snapshot.edition, snapshot.edition)
            self.assertEqual(len(ownership_evidence(frozen.snapshot.edition).asins), 4)
            self.assertEqual((task.source, task.task_id), ("prowlarr", task_id))
            exported = json.loads(
                convert_snapshot(
                    frozen.snapshot, ExportPolicy(public_url_prefixes=("https://example.invalid/",))
                ).metadata_json
            )
            self.assertEqual(exported.get("isbn"), snapshot.edition.isbn_13)
            self.assertEqual(exported["asin"], snapshot.edition.asin)
            restored = orchestrator._restore_task_from_retry_payload(
                orchestrator.serialize_task_for_retry(task)
            )
            assert restored
            self.assertEqual(context_from_task(restored), frozen)

    def test_native_direct_http_auth_policy_and_user_scope(self) -> None:
        harness = AppHarness()
        # Replace only the fixture's placeholder endpoint with the actual route.
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
        payload = {**self.payload(2), "user_id": 999, "is_admin": True}
        self.assertEqual(client.post("/api/releases/download", json=payload).status_code, 401)
        with client.session_transaction() as session:
            session["user_id"], session["db_user_id"], session["is_admin"] = (
                "fixture-user",
                2,
                False,
            )
        harness.settings["REQUEST_POLICY_DEFAULT_AUDIOBOOK"] = "request_book"
        self.assertEqual(client.post("/api/releases/download", json=payload).status_code, 403)
        self.assertIsNone(self.queue.get_task(cast("str", payload["source_id"])))
        harness.settings["REQUEST_POLICY_DEFAULT_AUDIOBOOK"] = "download"
        response = client.post("/api/releases/download", json=payload)
        self.assertEqual(response.status_code, 200, response.get_json())
        task = self.task(payload)
        self.assertEqual(task.user_id, 2)
        context = context_from_task(task)
        assert context is not None
        self.assertEqual(context.snapshot.edition.part_number, 2)
        # The route holds the module, so it follows the installed binding.
        from shelfmark.core import release_routes

        backend: object = release_routes.backend
        self.assertIs(backend.queue_release, orchestrator.queue_release)

    def test_native_admin_http_imported_alias_reaches_trusted_queue(self) -> None:
        db = self.database()
        row = self.request(db, 3)
        harness = AppHarness()
        request_routes.register_request_routes(
            harness.app,
            db,
            resolve_auth_mode=lambda: harness.mode,
            queue_release=orchestrator.queue_release,
        )
        client = harness.app.test_client()
        path = f"/api/admin/requests/{row['id']}/fulfil"
        payload = {"release_data": self.payload(3), "is_admin": True}
        self.assertEqual(client.post(path, json=payload).status_code, 401)
        with client.session_transaction() as session:
            session["user_id"], session["db_user_id"], session["is_admin"] = (
                "fixture-user",
                row["user_id"],
                False,
            )
        self.assertEqual(client.post(path, json=payload).status_code, 403)
        with client.session_transaction() as session:
            session["user_id"], session["db_user_id"], session["is_admin"] = (
                "fixture-admin",
                self.admin_id,
                True,
            )
        with (
            patch.object(request_routes, "notify_user"),
            patch.object(request_routes, "notify_admin"),
        ):
            response = client.post(path, json=payload)
        self.assertEqual(response.status_code, 200, response.get_json())
        task = self.task(self.payload(3))
        self.assertEqual(task.user_id, row["user_id"])
        self.assertIs(request_routes.fulfil_request, requests_service.fulfil_request)
        self.assertIsNotNone(context_from_task(task))

    def test_native_immediate_request_http_and_main_forwarding_callback(self) -> None:
        import __future__

        import ast
        from collections.abc import Callable

        db = self.database()
        row = self.request(db, 1)
        harness = AppHarness()
        main_path = Path(request_routes.__file__).resolve().parents[1] / "main.py"
        tree = ast.parse(main_path.read_text())
        node = next(
            n
            for n in tree.body
            if isinstance(n, ast.FunctionDef) and n.name == "_queue_release_for_routes"
        )
        namespace: dict[str, object] = {"backend": orchestrator}
        exec(
            compile(
                ast.Module(body=[node], type_ignores=[]),
                str(main_path),
                "exec",
                flags=__future__.annotations.compiler_flag,
            ),
            namespace,
        )
        callback = cast(
            "Callable[..., tuple[bool, str | None]]", namespace["_queue_release_for_routes"]
        )
        request_routes.register_request_routes(
            harness.app, db, resolve_auth_mode=lambda: harness.mode, queue_release=callback
        )
        client = harness.app.test_client()
        with client.session_transaction() as session:
            session["user_id"], session["db_user_id"], session["is_admin"] = (
                "fixture-user",
                row["user_id"],
                False,
            )
        payload = {
            "book_data": row["book_data"],
            "release_data": self.payload(1),
            "context": {
                "source": "prowlarr",
                "content_type": "audiobook",
                "request_level": "release",
            },
        }
        with patch.object(
            request_routes, "load_users_request_policy_settings", return_value=harness.settings
        ):
            response = client.post("/api/requests", json=payload)
        self.assertEqual(response.status_code, 200, response.get_json())
        task = self.task(self.payload(1))
        self.assertIsNone(task.request_id)  # Native immediate branch creates no request.
        self.assertEqual(task.user_id, row["user_id"])
        self.assertIsNotNone(context_from_task(task))

    def test_reader_rejects_rebound_retry_source_and_unknown_context_version(self) -> None:
        payload = self.payload()
        self.assertTrue(orchestrator.queue_release(payload)[0])
        saved = orchestrator.serialize_task_for_retry(self.task(payload))
        saved["task_id"] = "a-different-release"
        restored = orchestrator._restore_task_from_retry_payload(saved)
        assert restored is not None
        with self.assertRaises(ValueError):
            context_from_task(restored)
        raw = json.loads(cast("str", restored.output_args["edition_context"]))
        raw["version"] = True
        with self.assertRaises(ValueError):
            FrozenEditionContext.from_json(json.dumps(raw))

    def test_ordinary_request_context_remains_uninterpreted(self) -> None:
        release: dict[str, object] = {
            **self.payload(),
            "edition_context": {"provider": "another-provider", "other_contract": True},
        }
        self.assertIs(bind_request_locator({"provider": "another-provider"}, release), release)
        self.assertTrue(orchestrator.queue_release(release)[0])
        self.assertEqual(self.task(release).output_args, {})

    def test_source_binding_cannot_change_during_provider_resolution(self) -> None:
        payload = self.payload(1)
        original_id = cast("str", payload["source_id"])
        snapshot = self.provider.get_snapshot(self.books[1].provider_id)

        def lookup(key: str) -> EditionSnapshot | None:
            payload["source"], payload["source_id"] = "audiobookbay", "different-release"
            return snapshot

        with patch.object(self.provider, "get_snapshot", side_effect=lookup):
            self.assertTrue(orchestrator.queue_release(payload)[0])
        task = self.queue.get_task(original_id)
        assert task is not None
        self.assertEqual(task.source, "prowlarr")
        self.assertIsNotNone(context_from_task(task))

    def test_native_batch_immediate_requests_keep_each_edition(self) -> None:
        db = self.database()
        user = db.create_user("batch-fixture")
        harness = AppHarness()
        request_routes.register_request_routes(
            harness.app,
            db,
            resolve_auth_mode=lambda: harness.mode,
            queue_release=orchestrator.queue_release,
        )
        client = harness.app.test_client()
        with client.session_transaction() as session:
            session["user_id"], session["db_user_id"], session["is_admin"] = (
                "batch-fixture",
                user["id"],
                False,
            )
        requests = [
            {
                "book_data": {
                    "provider": self.books[part].provider,
                    "provider_id": self.books[part].provider_id,
                    "title": self.books[part].title,
                    "author": "Example Author",
                },
                "release_data": self.payload(part),
                "context": {
                    "source": "prowlarr",
                    "content_type": "audiobook",
                    "request_level": "release",
                },
            }
            for part in (1, 2)
        ]
        with patch.object(
            request_routes, "load_users_request_policy_settings", return_value=harness.settings
        ):
            response = client.post("/api/requests/batch", json={"requests": requests})
        self.assertEqual(response.status_code, 200, response.get_json())
        for part in (1, 2):
            task = self.task(self.payload(part))
            context = context_from_task(task)
            assert context is not None
            self.assertEqual(
                (task.user_id, context.snapshot.edition.part_number), (user["id"], part)
            )

    def test_native_persisted_retry_http_enforces_owner_and_ignores_forged_body(self) -> None:
        from flask import session

        from shelfmark.core.queue_routes import register_queue_routes

        db = self.database()
        user = db.create_user("retry-owner")
        intruder = db.create_user("different-user")
        history = DownloadHistoryService(db.db_path)
        register_download_recording(
            get_user_db=lambda: db,
            get_download_history_service=lambda: history,
            get_activity_view_state_service=lambda: None,
            emit_request_update_events=lambda rows: None,
            ws_manager=None,
        )
        payload = self.payload(1)
        self.assertTrue(
            orchestrator.queue_release(payload, user_id=user["id"], username="retry-owner")[0]
        )
        original = self.task(payload)
        self.queue.update_status(original.task_id, QueueStatus.ERROR)
        fresh = BookQueue()
        harness = AppHarness()

        def scope(*, require_authenticated: bool = True) -> tuple[bool, int | None, bool]:
            value = session.get("db_user_id")
            return (
                bool(session.get("is_admin")),
                value if isinstance(value, int) else None,
                isinstance(value, int),
            )

        register_queue_routes(
            harness.app,
            harness.login,
            get_user_db=lambda: db,
            get_download_history_service=lambda: history,
            resolve_status_scope=scope,
            emit_request_update_events=lambda rows: None,
        )
        client = harness.app.test_client()
        path = f"/api/download/{original.task_id}/retry"
        forged = {
            "edition_context": self.payload(2)["edition_context"],
            "user_id": user["id"],
            "is_admin": True,
        }
        with patch.object(orchestrator, "book_queue", fresh):
            self.assertEqual(client.post(path, json=forged).status_code, 401)
            with client.session_transaction() as session_data:
                session_data["user_id"], session_data["db_user_id"] = (
                    "different-user",
                    intruder["id"],
                )
                session_data["is_admin"] = False
            self.assertEqual(client.post(path, json=forged).status_code, 403)
            with client.session_transaction() as session_data:
                session_data["user_id"], session_data["db_user_id"] = "retry-owner", user["id"]
            with patch.object(
                self.provider,
                "get_snapshot",
                side_effect=AssertionError("Retry must not resolve a new edition"),
            ):
                response = client.post(path, json=forged)
            self.assertEqual(response.status_code, 200, response.get_json())
        retried = fresh.get_task(original.task_id)
        assert retried is not None
        self.assertEqual(retried.output_args, original.output_args)
        self.assertEqual(context_from_task(retried), context_from_task(original))

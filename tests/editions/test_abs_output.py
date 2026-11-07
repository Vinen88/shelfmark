"""Synthetic native-folder output tests; no real clients or libraries."""

import errno
import json
import os
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from threading import Event
from typing import ClassVar
from unittest.mock import Mock, patch

from test_abs_metadata import snapshots

from shelfmark.core.edition_handoff import FrozenEditionContext
from shelfmark.core.models import DownloadTask
from shelfmark.download.outputs import edition as abs_output
from shelfmark.download.outputs.abs_metadata import ExportPolicy, convert_snapshot
from shelfmark.download.postprocess import policy, transfer
from shelfmark.metadata_providers.editions.snapshot import EditionSnapshot


class OutputTests(unittest.TestCase):
    snapshots: ClassVar[tuple[EditionSnapshot, ...]]

    @classmethod
    def setUpClass(cls) -> None:
        cls.snapshots = snapshots()

    def setUp(self) -> None:
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.files = [self.source / f"track-{n}.m4b" for n in (1, 2)]
        for n, path in enumerate(self.files):
            path.write_bytes(bytes([n + 1]) * 120)
        self.destination = self.root / "library"
        self.destination.mkdir()
        for context in (
            patch.object(
                abs_output,
                "convert_snapshot",
                side_effect=lambda snapshot: convert_snapshot(
                    snapshot, ExportPolicy(("https://example.invalid/",))
                ),
            ),
            patch.object(
                transfer, "get_template", return_value="{Author}/{Title}/{Title} - {PartNumber}"
            ),
            patch.object(
                policy, "get_template", return_value="{Author}/{Title}/{Title} - {PartNumber}"
            ),
            patch.object(policy, "get_word_separator", return_value=" "),
            patch.object(transfer, "get_word_separator", return_value=" "),
        ):
            context.start()
            self.addCleanup(context.stop)

    def task(self, part: int = 0, source: str = "prowlarr") -> DownloadTask:
        snapshot = self.snapshots[part]
        return DownloadTask(
            task_id="fixture",
            source=source,
            content_type="audiobook",
            title=snapshot.edition_title,
            author="Example Author",
            format="m4b",
            output_mode="folder",
            output_args={
                "edition_context": FrozenEditionContext(
                    snapshot.to_json(), source, "fixture"
                ).to_json()
            },
        )

    def prepare(self, task: DownloadTask) -> abs_output.Publication:
        result = abs_output.prepare(task, self.files, self.destination, "organize", Event())
        assert result is not None
        return result

    def test_parts_and_both_sources_copy_and_hardlink(self) -> None:
        for part, source, hardlink in ((0, "prowlarr", True), (1, "audiobookbay", False)):
            task = self.task(part, source)
            attempt = self.prepare(task)
            metadata = json.loads(attempt.sidecars[0][0].read_text())
            self.assertEqual(metadata["asin"], self.snapshots[part].edition.asin)
            self.assertEqual(metadata["series"], ["The Lantern Cycle #1"])
            self.assertIn(f"Part {part + 1} of 2", metadata["title"])
            paths, error, _ = transfer.transfer_book_files(
                self.files,
                self.destination,
                task,
                use_hardlink=hardlink,
                is_torrent=True,
                preserve_source=True,
                organization_mode="organize",
            )
            self.assertIsNone(error)
            attempt.verify(paths, Event())
            for origin, final in zip(self.files, paths, strict=True):
                self.assertEqual(origin.read_bytes(), final.read_bytes())
                self.assertEqual(origin.stat().st_ino == final.stat().st_ino, hardlink)
            present = self.prepare(task)
            self.assertTrue(present.already_present)
            self.assertEqual(present.paths, attempt.paths)

    def test_incomplete_cancelled_and_changed_metadata(self) -> None:
        task = self.task()
        attempt = self.prepare(task)
        with self.assertRaises(ValueError):
            attempt.verify([], Event())
        paths, _, _ = transfer.transfer_book_files(
            self.files,
            self.destination,
            task,
            use_hardlink=False,
            is_torrent=True,
            organization_mode="organize",
        )
        cancel = Event()
        cancel.set()
        with self.assertRaises(ValueError):
            attempt.verify(paths, cancel)
        attempt.sidecars[0][0].write_text("user edit")
        with self.assertRaises(ValueError):
            attempt.verify(paths, Event())
        self.assertEqual(attempt.sidecars[0][0].read_text(), "user edit")

    def test_missing_invalid_context_and_native_output(self) -> None:
        task = self.task()
        task.output_args = {}
        self.assertIsNone(
            abs_output.prepare(task, self.files, self.destination, "organize", Event())
        )
        task.output_args = {"edition_context": "invalid"}
        with self.assertRaises(ValueError):
            self.prepare(task)

    def test_unsupported_pack_layout_and_script(self) -> None:
        task = self.task()
        task.multi_book = True
        with self.assertRaises(ValueError):
            self.prepare(task)
        task.multi_book = False
        with self.assertRaises(ValueError):
            abs_output.prepare(task, self.files, self.destination, "none", Event())
        from shelfmark.core.config import config

        with patch.object(config, "CUSTOM_SCRIPT", "/tmp/custom", create=True):
            with self.assertRaises(ValueError):
                self.prepare(task)

    def test_collision_preserves_user_files_and_cancel_before_writing(self) -> None:
        task = self.task()
        attempt = self.prepare(task)
        user_cover = attempt.paths[0].parent / "cover.jpg"
        user_cover.write_bytes(b"user cover")
        attempt.sidecars[0][0].write_text("user metadata")
        alternate = self.prepare(task)
        self.assertNotEqual(alternate.paths[0].parent, attempt.paths[0].parent)
        self.assertEqual(user_cover.read_bytes(), b"user cover")
        self.assertEqual(attempt.sidecars[0][0].read_text(), "user metadata")
        cancel = Event()
        cancel.set()
        with self.assertRaises(ValueError):
            abs_output.prepare(self.task(1), self.files, self.destination, "organize", cancel)
        self.assertEqual(len(list(self.destination.rglob("metadata.json"))), 2)

    def test_case_collision_and_destination_symlink(self) -> None:
        (self.destination / "example author").mkdir()
        attempt = self.prepare(self.task())
        self.assertIn("example author", attempt.paths[0].parts)
        other = self.root / "other"
        other.mkdir()
        link = self.root / "linked"
        link.symlink_to(other, target_is_directory=True)
        with self.assertRaises(ValueError):
            abs_output.prepare(self.task(), self.files, link, "organize", Event())
        self.assertEqual(list(other.iterdir()), [])

    def complete(
        self, task: DownloadTask, attempt: abs_output.Publication, hardlink: bool = False
    ) -> list[Path]:
        paths, error, _ = transfer.transfer_book_files(
            list(attempt.sources),
            self.destination,
            task,
            use_hardlink=hardlink,
            is_torrent=True,
            preserve_source=True,
            organization_mode="organize",
            destination_paths=list(attempt.transfer_paths),
        )
        self.assertIsNone(error)
        attempt.verify(paths, Event())
        return list(attempt.paths)

    def test_same_asin_different_format_requires_separate_output(self) -> None:
        task = self.task()
        m4b = self.prepare(task)
        self.complete(task, m4b, True)
        self.files = [source.with_suffix(".mp3") for source in self.files]
        for index, path in enumerate(self.files):
            path.write_bytes(bytes([index + 1]) * 120)
        task.format = "mp3"
        mp3 = self.prepare(task)
        self.assertFalse(mp3.already_present)
        self.assertNotEqual(mp3.paths[0].parent, m4b.paths[0].parent)
        self.complete(task, mp3)
        self.assertTrue(self.prepare(task).already_present)
        self.assertTrue(all(path.exists() for path in m4b.paths))

    def test_completed_inventory_with_same_size_changed_audio_is_not_reused(self) -> None:
        task = self.task()
        first = self.prepare(task)
        self.complete(task, first)
        first.paths[0].write_bytes(b"x" * first.sizes[0])
        second = self.prepare(task)
        self.assertFalse(second.already_present)
        self.assertNotEqual(second.paths[0].parent, first.paths[0].parent)
        self.assertEqual(first.paths[0].read_bytes(), b"x" * first.sizes[0])

    def test_existing_directory_and_file_get_meaningful_destinations(self) -> None:
        task = self.task()
        first = self.prepare(task)
        sentinel = first.paths[0].parent / "user.txt"
        sentinel.write_text("preserve")
        first.sidecars[0][0].write_text("user metadata")
        second = self.prepare(task)
        self.assertIn(self.snapshots[0].edition.narrators[0], second.paths[0].parent.name)
        self.complete(task, second, True)
        self.assertEqual(sentinel.read_text(), "preserve")
        self.assertEqual(first.sidecars[0][0].read_text(), "user metadata")
        self.assertTrue(self.prepare(task).already_present)
        # The book directory itself may instead be an unrelated existing file.
        other = self.task(1)
        from shelfmark.core.naming import build_library_path

        planned = build_library_path(
            str(self.destination),
            policy.get_template(is_audiobook=True, organization_mode="organize"),
            transfer.build_file_metadata(other, self.files[0], part_number="01"),
            extension="m4b",
        ).parent
        planned.write_bytes(b"unrelated file")
        publication = self.prepare(other)
        self.assertNotEqual(publication.paths[0].parent, planned)
        self.complete(other, publication)
        self.assertEqual(planned.read_bytes(), b"unrelated file")

    def test_different_recordings_same_native_title_and_stable_identifier_fallback(self) -> None:
        first, second = self.task(), self.task(1)
        second.title = first.title
        original = self.prepare(first)
        self.complete(first, original)
        alternate = self.prepare(second)
        self.assertNotEqual(alternate.paths[0].parent, original.paths[0].parent)
        self.complete(second, alternate)
        self.assertTrue(self.prepare(second).already_present)
        self.assertEqual(
            json.loads(original.sidecars[0][0].read_text())["asin"], self.snapshots[0].edition.asin
        )
        self.assertEqual(
            json.loads(alternate.sidecars[0][0].read_text())["asin"], self.snapshots[1].edition.asin
        )
        third = self.task()
        # Occupied directories with user metadata never get adopted.
        original.sidecars[0][0].write_text("changed")
        fallback = self.prepare(third)
        self.assertNotEqual(fallback.paths, original.paths)

    def test_duplicate_generated_filenames_and_source_basenames_preserve_every_track(self) -> None:
        task = self.task()
        with (
            patch.object(policy, "get_template", return_value="{Author}/{Title}/{Title}"),
            patch.object(transfer, "get_template", return_value="{Author}/{Title}/{Title}"),
        ):
            attempt = self.prepare(task)
            self.assertEqual(len({p.name.casefold() for p in attempt.paths}), 2)
            self.complete(task, attempt, True)
        # Same basenames in disc subdirectories are classified by the native pack planner.
        self.files = []
        for n in (1, 2):
            disc = self.source / f"Disc {n}"
            disc.mkdir()
            audio = disc / "track.mp3"
            audio.write_bytes(bytes([n]) * 120)
            self.files.append(audio)
        attempt = self.prepare(self.task(1))
        self.complete(self.task(1), attempt)
        self.assertEqual(
            [p.read_bytes() for p in attempt.paths], [p.read_bytes() for p in self.files]
        )

    def test_interrupted_import_resumes_verified_tracks_without_overwriting(self) -> None:
        task = self.task()
        attempt = self.prepare(task)
        os.link(self.files[0], attempt.paths[0])
        inode = attempt.paths[0].stat().st_ino
        resumed = self.prepare(task)
        self.assertFalse(resumed.already_present)
        self.assertEqual(resumed.sources, (self.files[1],))
        self.complete(task, resumed, True)
        self.assertEqual(resumed.paths[0].stat().st_ino, inode)
        self.assertTrue(self.prepare(task).already_present)
        self.assertEqual(self.files[0].read_bytes(), bytes([1]) * 120)

    def test_partial_or_changed_audio_is_never_treated_as_complete(self) -> None:
        task = self.task()
        attempt = self.prepare(task)
        attempt.paths[0].write_bytes(b"user data")
        another = self.prepare(task)
        self.assertNotEqual(another.paths[0].parent, attempt.paths[0].parent)
        self.assertEqual(attempt.paths[0].read_bytes(), b"user data")
        self.complete(task, another)
        another.paths[1].unlink()
        self.assertFalse(self.prepare(task).already_present)

    def test_single_file_legacy_completion_uses_native_history_not_sidecars_alone(self) -> None:
        task = self.task()
        self.files = self.files[:1]
        attempt = self.prepare(task)
        self.complete(task, attempt)
        data = json.loads(attempt.sidecars[1][0].read_text())
        del data["files"]
        attempt.sidecars[1][0].write_text(json.dumps(data))
        config_dir = self.root / "config"
        config_dir.mkdir()
        with sqlite3.connect(config_dir / "users.db") as connection:
            connection.execute(
                "CREATE TABLE download_history(final_status TEXT, download_path TEXT)"
            )
            connection.execute(
                "INSERT INTO download_history VALUES(?,?)", ("complete", str(attempt.paths[0]))
            )
        connection.close()
        with patch("shelfmark.config.env.CONFIG_DIR", config_dir):
            present = self.prepare(task)
            self.assertTrue(present.already_present)
            self.assertEqual(present.paths, attempt.paths)
            present.verify([], Event())

    def test_hardlink_cross_device_fallback_preserves_audio(self) -> None:
        task = self.task()
        attempt = self.prepare(task)
        link = os.link

        def cross_device(source: str | Path, target: str | Path, **kwargs: object) -> None:
            if Path(source) in self.files:
                raise OSError(errno.EXDEV, "synthetic cross-device")
            link(source, target)

        with patch("os.link", side_effect=cross_device):
            self.complete(task, attempt, True)
        for source, target in zip(self.files, attempt.paths, strict=True):
            self.assertEqual(source.read_bytes(), target.read_bytes())
            self.assertNotEqual(source.stat().st_ino, target.stat().st_ino)

    def test_specific_errors_and_no_private_exception_text(self) -> None:
        task = self.task()
        with self.assertRaisesRegex(abs_output.OutputError, "Source audio file is missing"):
            abs_output.prepare(
                task, [self.source / "missing.mp3"], self.destination, "organize", Event()
            )
        with self.assertRaisesRegex(abs_output.OutputError, "Unsafe audiobook destination"):
            abs_output.prepare(
                task, self.files, self.destination / ".." / "escape", "organize", Event()
            )
        snapshot = replace(
            self.snapshots[0],
            edition=replace(
                self.snapshots[0].edition, cover_url="https://unapproved.invalid/cover"
            ),
        )
        task.output_args = {
            "edition_context": FrozenEditionContext(
                snapshot.to_json(), task.source, task.task_id
            ).to_json()
        }
        with self.assertRaisesRegex(abs_output.OutputError, "unsupported URL"):
            self.prepare(task)
        with self.assertLogs(abs_output.logger, level="WARNING") as logs:
            message = abs_output.failure_message(
                PermissionError("https://private.invalid/?secret=123"), "transfer"
            )
        self.assertEqual(message, "Audio output permission denied")
        self.assertNotIn("private.invalid", str(logs.output))

    def test_case_only_audio_collision_resumes_using_existing_spelling(self) -> None:
        task = self.task()
        attempt = self.prepare(task)
        renamed = attempt.paths[0].with_name(attempt.paths[0].name.lower())
        os.link(self.files[0], renamed)
        resumed = self.prepare(task)
        self.assertEqual(resumed.paths[0], renamed)
        self.assertEqual(resumed.sources, (self.files[1],))
        self.complete(task, resumed)
        self.assertFalse(attempt.paths[0].exists())

    def test_added_catalog_alias_does_not_duplicate_a_completed_exact_recording(self) -> None:
        from shelfmark.metadata_providers.editions.catalog_types import AggregatedEdition, SourceRef

        task = self.task()
        attempt = self.prepare(task)
        self.complete(task, attempt)
        snapshot = self.snapshots[0]
        edition = snapshot.edition
        assert isinstance(edition, AggregatedEdition)
        snapshot = replace(
            snapshot,
            edition=replace(
                edition,
                provenance=replace(
                    edition.provenance,
                    references=(
                        *edition.provenance.references,
                        SourceRef("fixture_alias", "extra-release"),
                    ),
                ),
            ),
        )
        task.output_args = {
            "edition_context": FrozenEditionContext(
                snapshot.to_json(), task.source, task.task_id
            ).to_json()
        }
        self.assertTrue(self.prepare(task).already_present)

    def test_unplanned_audio_is_not_adopted_under_the_selected_metadata(self) -> None:
        task = self.task()
        attempt = self.prepare(task)
        unrelated = attempt.paths[0].parent / "another-book.mp3"
        unrelated.write_bytes(b"unrelated audio")
        alternate = self.prepare(task)
        self.assertNotEqual(alternate.paths[0].parent, unrelated.parent)
        self.assertEqual(unrelated.read_bytes(), b"unrelated audio")
        self.complete(task, alternate)

    def test_completed_user_metadata_is_preserved_with_recording_contradictions_checked(
        self,
    ) -> None:
        task = self.task()
        attempt = self.prepare(task)
        self.complete(task, attempt)
        metadata = json.loads(attempt.sidecars[0][0].read_text())
        metadata["description"] = "User synopsis"
        edited = json.dumps(metadata)
        attempt.sidecars[0][0].write_text(edited)
        present = self.prepare(task)
        self.assertTrue(present.already_present)
        present.verify([], Event())
        self.assertEqual(attempt.sidecars[0][0].read_text(), edited)
        metadata["asin"] = "B0OTHER001"
        attempt.sidecars[0][0].write_text(json.dumps(metadata))
        self.assertFalse(self.prepare(task).already_present)

    def test_three_graphicaudio_parts_keep_independent_metadata_and_native_paths(self) -> None:
        from test_search_refinement import RegionalHttp, regional_sources

        from shelfmark.metadata_providers.editions.identity import EditionIdentity
        from shelfmark.metadata_providers.editions.multisource import MultiSourceBackend

        editions = MultiSourceBackend(regional_sources(RegionalHttp(3))).search("Lantern")
        destinations = []
        for edition in editions:
            snapshot = EditionSnapshot(
                EditionIdentity("edition_metadata", edition.provider_id), edition
            )
            task = DownloadTask(
                task_id="fixture",
                source="prowlarr",
                content_type="audiobook",
                title=snapshot.edition_title,
                author="Example Author",
                format="m4b",
                output_mode="folder",
                output_args={
                    "edition_context": FrozenEditionContext(
                        snapshot.to_json(), "prowlarr", "fixture"
                    ).to_json()
                },
            )
            attempt = self.prepare(task)
            self.complete(task, attempt, True)
            destinations.append(attempt.paths[0].parent)
            metadata = json.loads(attempt.sidecars[0][0].read_text())
            self.assertEqual(metadata["asin"], edition.asin)
            self.assertIn(f"Part {edition.part_number} of 3", metadata["title"])
        self.assertEqual(len(set(destinations)), 3)

    def test_native_collision_transfer_and_already_present_status_use_final_paths(self) -> None:
        from shelfmark.download.outputs import folder
        from shelfmark.download.postprocess import pipeline

        task = self.task()
        occupied = self.prepare(task)
        occupied.sidecars[0][0].write_text("user metadata")
        plan = Mock(
            destination=self.destination,
            organization_mode="organize",
            use_hardlink=True,
            hardlink_source=self.source,
            output_mode="folder",
        )
        prepared = Mock(files=self.files, working_path=self.source, cleanup_paths=[])
        prepared.output_plan.stage_action = "none"
        messages: list[tuple[str, str | None]] = []

        def status(state: str, message: str | None = None) -> None:
            messages.append((state, message))

        with (
            patch.object(folder, "_build_processing_plan", return_value=plan),
            patch.object(pipeline, "prepare_output_files", return_value=prepared),
            patch.object(pipeline, "cleanup_output_staging"),
            patch.object(pipeline, "maybe_run_custom_script", return_value=True),
            patch.object(abs_output, "notify_scan") as scan,
        ):
            self.assertIsNotNone(folder.process_folder_output(self.source, task, Event(), status))
            scan.assert_called_once()
            final = tuple(task.download_paths)
            self.assertNotEqual(Path(final[0]).parent, occupied.paths[0].parent)
            with patch.object(pipeline, "transfer_book_files") as transfer_again:
                self.assertIsNotNone(
                    folder.process_folder_output(self.source, task, Event(), status)
                )
                transfer_again.assert_not_called()
            scan.assert_called_once()  # Already-present is not a new ABS scan.
            self.assertEqual(tuple(task.download_paths), final)
            self.assertEqual(
                messages[-1], ("complete", "Destination already contains this recording")
            )
        self.assertEqual(occupied.sidecars[0][0].read_text(), "user metadata")

    def test_scan_statuses_bounded_and_no_force(self) -> None:
        from shelfmark.core.library_providers import audiobookshelf as client

        for code in (200, 409, 401, 503):
            session = Mock()
            session.__enter__ = Mock(return_value=session)
            session.__exit__ = Mock(return_value=False)
            session.post.return_value.status_code = code
            with (
                patch.dict(os.environ, {"SHELFMARK_EDITION_ABS_LIBRARY_ID": "destination"}),
                patch.object(
                    client, "_config", return_value=(True, "http://abs", "secret", ["other"])
                ),
                patch.object(client, "_session", return_value=session),
            ):
                abs_output.notify_scan()
                session.post.assert_called_once_with(
                    "http://abs/api/libraries/destination/scan", timeout=15, allow_redirects=False
                )
        with (
            patch.object(client, "_config", side_effect=OSError("private credentials")),
            self.assertLogs(abs_output.logger, level="WARNING") as logs,
        ):
            abs_output.notify_scan()
        self.assertNotIn("private credentials", str(logs.output))

    def test_scan_selects_only_an_unambiguous_book_library(self) -> None:
        from shelfmark.core.library_providers import audiobookshelf as client

        for configured, available, expected in (
            (["selected"], [], "selected"),
            (
                [],
                [{"id": "only", "mediaType": "book"}, {"id": "podcasts", "mediaType": "podcast"}],
                "only",
            ),
            ([], [], None),
            ([], [{"id": "a", "mediaType": "book"}, {"id": "b", "mediaType": "book"}], None),
            (
                ["b", "other"],
                [{"id": "a", "mediaType": "book"}, {"id": "b", "mediaType": "book"}],
                "b",
            ),
            ([], [{"id": "../unsafe", "mediaType": "book"}], None),
        ):
            with self.subTest(configured=configured, available=available):
                session = Mock()
                session.__enter__ = Mock(return_value=session)
                session.__exit__ = Mock(return_value=False)
                session.get.return_value.json.return_value = {"libraries": available}
                session.post.return_value.status_code = 200
                with (
                    patch.dict(os.environ, {"SHELFMARK_EDITION_ABS_LIBRARY_ID": ""}),
                    patch.object(
                        client, "_config", return_value=(True, "http://abs", "secret", configured)
                    ),
                    patch.object(client, "_session", return_value=session),
                ):
                    abs_output.notify_scan()
                if expected:
                    session.post.assert_called_once_with(
                        f"http://abs/api/libraries/{expected}/scan",
                        timeout=15,
                        allow_redirects=False,
                    )
                else:
                    session.post.assert_not_called()
                if len(configured) == 1:
                    session.get.assert_not_called()
                else:
                    session.get.assert_called_once_with(
                        "http://abs/api/libraries", timeout=15, allow_redirects=False
                    )

    def test_native_handler_scan_only_after_verified_transfer(self) -> None:
        from shelfmark.download.outputs import folder
        from shelfmark.download.postprocess import pipeline

        for failure in (False, True):
            task = self.task(int(failure))
            plan = Mock(
                destination=self.destination,
                organization_mode="organize",
                use_hardlink=True,
                stage_action="none",
                hardlink_source=self.source,
                output_mode="folder",
            )
            prepared = Mock(files=self.files, working_path=self.source, cleanup_paths=[])
            prepared.output_plan.stage_action = "none"
            events: list[str] = []

            def status(state: str, message: str | None = None) -> None:
                events.append(state)  # noqa: B023 -- Fixture callback runs synchronously within this iteration.

            def scan() -> None:
                self.assertNotIn("complete", events)  # noqa: B023 -- Fixture callback runs synchronously within this iteration.
                self.assertTrue(all(Path(p).is_file() for p in paths_seen))  # noqa: B023 -- Fixture callback runs synchronously within this iteration.
                events.append("scan")  # noqa: B023 -- Fixture callback runs synchronously within this iteration.

            paths_seen: list[str] = []
            original = transfer.transfer_book_files

            def native_transfer(*args: object, **kwargs: object) -> object:
                # Explicit fixture arguments retain the real native transfer.
                self.assertTrue(list(self.destination.rglob("metadata.json")))
                if failure:  # noqa: B023 -- Fixture callback runs synchronously within this iteration.
                    return [], "Synthetic transfer failure", {}
                result = original(  # noqa: B023 -- Invoked synchronously in this fixture iteration.
                    self.files,
                    self.destination,
                    task,  # noqa: B023 -- Invoked synchronously in this fixture iteration.
                    use_hardlink=True,
                    is_torrent=True,
                    preserve_source=True,
                    organization_mode="organize",
                )
                paths_seen.extend(str(p) for p in result[0])  # noqa: B023 -- Fixture callback runs synchronously within this iteration.
                return result

            with (
                patch.object(folder, "_build_processing_plan", return_value=plan),
                patch.object(pipeline, "prepare_output_files", return_value=prepared),
                patch.object(pipeline, "transfer_book_files", side_effect=native_transfer),
                patch.object(pipeline, "cleanup_output_staging"),
                patch.object(pipeline, "maybe_run_custom_script", return_value=True),
                patch.object(abs_output, "notify_scan", side_effect=scan),
            ):
                result = folder.process_folder_output(self.source, task, Event(), status)
            if failure:
                self.assertIsNone(result)
                self.assertNotIn("scan", events)
                self.assertNotIn("complete", events)
            else:
                self.assertIsNotNone(result)
                self.assertEqual(events[-2:], ["scan", "complete"])
                self.assertEqual(task.download_paths, paths_seen)


if __name__ == "__main__":
    unittest.main()

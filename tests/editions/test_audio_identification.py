"""Local tag fixtures and explicit download intentions; no external transports."""

import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from mock_backend import MOCK_EDITIONS, MockBackend
from mutagen.id3 import ID3, TALB, TCOM, TLAN, TPUB, TXXX

from shelfmark.core.edition_handoff import (
    FrozenEditionContext,
    bind_request_locator,
    resolve_release,
)
from shelfmark.download.outputs.abs_metadata import convert_work
from shelfmark.download.outputs.audio_identification import identify, inspect_audio
from shelfmark.metadata_providers import MetadataSearchOptions
from shelfmark.metadata_providers.editions.identity import EditionIdentity
from shelfmark.metadata_providers.editions.provider import EditionMetadataProvider
from shelfmark.metadata_providers.editions.snapshot import EditionSnapshot


def snapshot(**changes):
    edition = replace(MOCK_EDITIONS[0], **changes)
    return EditionSnapshot(EditionIdentity("edition_metadata", edition.provider_id), edition)


def tagged(
    path,
    *,
    title="The Lantern Archive - GraphicAudio Part 1 of 2",
    asin="MOCKASIN01",
    narrator="Example Narrator 1",
    author="Example Author",
    **values,
):
    # MPEG headers sufficient for stream-info inspection; tests never decode audio.
    path.write_bytes((b"\xff\xfb\x90\x00" + bytes(413)) * 12)
    tags = ID3()
    if title is not None:
        tags.add(TALB(encoding=3, text=[title]))
    if author is not None:
        tags.add(TCOM(encoding=3, text=[author]))
    if asin is not None:
        tags.add(TXXX(encoding=3, desc="ASIN", text=[asin]))
    if narrator is not None:
        tags.add(TXXX(encoding=3, desc="NARRATOR", text=[narrator, "Example Cast 1"]))
    tags.add(TPUB(encoding=3, text=["GraphicAudio (fictional test fixture)"]))
    tags.add(TLAN(encoding=3, text=["en"]))
    for key, value in values.items():
        tags.add(TXXX(encoding=3, desc=key, text=[value]))
    tags.save(path)
    return path


class AudioIdentificationTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / "a misleading other book.mp3"

    def context(self, *, work=False, selected=None, candidates=()):
        return FrozenEditionContext(
            (selected or snapshot()).to_json(),
            "prowlarr",
            "fixture",
            work,
            tuple(value.to_json() for value in candidates),
        )

    def test_exact_asin_with_corroboration_and_read_only_headers(self):
        tagged(self.path)
        before = hashlib.sha256(self.path.read_bytes()).digest()
        result = identify([self.path], self.context())
        self.assertEqual(result.outcome, "exact")
        self.assertEqual(result.snapshot.identity, snapshot().identity)
        self.assertGreater(result.observed["duration_seconds"], 0)
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).digest(), before)

    def test_identifier_alone_is_not_proof_and_missing_tags_are_unknown(self):
        for changes in (
            {"narrator": None, "publisher": "different"},
            {"asin": None, "narrator": None},
            {"asin": "invalid"},
        ):
            tagged(self.path, **changes)
            result = identify([self.path], self.context(work=True))
            self.assertEqual(result.outcome, "work")
        self.path.write_bytes(b"unsupported audio header")
        self.assertEqual(identify([self.path], self.context()).outcome, "work")

    def test_wrong_work_author_and_part_are_specific_conflicts(self):
        for changes in (
            {"title": "A Different Book"},
            {"author": "Different Author"},
            {"title": "The Lantern Archive Part 2 of 2"},
        ):
            tagged(self.path, **changes)
            result = identify([self.path], self.context())
            self.assertEqual(result.outcome, "conflict")
            self.assertIn("retained", result.reason)

    def test_different_recording_requires_get_book_for_controlled_fallback(self):
        tagged(self.path, asin="OTHERASIN1", narrator="Different Narrator")
        result = identify([self.path], self.context())
        self.assertEqual(result.outcome, "conflict")
        self.assertIn("Get book", result.reason)
        self.assertEqual(identify([self.path], self.context(work=True)).outcome, "work")
        tagged(self.path, asin=None, narrator="Different Narrator")
        self.assertEqual(identify([self.path], self.context()).outcome, "conflict")

    def test_incompatible_catalog_duration_vetoes_identifiers_but_never_proves_identity(self):
        from test_abs_metadata import snapshots

        selected = snapshots()[0]
        edition = selected.edition
        tagged(self.path, title=edition.title, asin=edition.asin, narrator=edition.narrators[0])
        self.assertEqual(
            identify([self.path], self.context(work=True, selected=selected)).outcome, "work"
        )

    def test_generic_intent_identifies_another_discovered_recording(self):
        other = snapshot(
            edition_id="other-recording",
            asin="OTHERASIN1",
            narrators=("Different Narrator", "Example Cast 1"),
        )
        tagged(self.path, asin="OTHERASIN1", narrator="Different Narrator")
        result = identify([self.path], self.context(work=True, candidates=(snapshot(), other)))
        self.assertEqual(result.outcome, "exact")
        self.assertEqual(result.snapshot.identity, other.identity)

    def test_recording_isbn_requires_attestation_and_corroboration(self):
        from test_abs_metadata import snapshots

        selected = snapshots()[0]
        edition = selected.edition
        tagged(
            self.path,
            title=edition.title,
            asin=None,
            narrator=edition.narrators[0],
            isbn=edition.isbn_13,
        )
        # This fixture's remaining cast member and publisher are supplied explicitly.
        tags = ID3(self.path)
        tags.add(TXXX(encoding=3, desc="NARRATOR", text=list(edition.narrators)))
        tags.add(TPUB(encoding=3, text=[edition.publisher]))
        tags.save(self.path)
        seconds = inspect_audio([self.path])["duration_seconds"]
        selected = replace(
            selected,
            edition=replace(
                selected.edition,
                provenance=replace(
                    selected.edition.provenance,
                    values=tuple(
                        replace(value, value_json=json.dumps(seconds / 60))
                        if value.field == "source.lengthMinutes"
                        else value
                        for value in selected.edition.provenance.values
                    ),
                ),
            ),
        )
        self.assertEqual(identify([self.path], self.context(selected=selected)).outcome, "exact")
        tagged(self.path, asin=edition.asin, isbn="9780000000026")
        self.assertEqual(
            identify([self.path], self.context(work=True, selected=selected)).outcome, "work"
        )

    def test_all_three_graphicaudio_parts_remain_recording_specific(self):
        for part in (1, 2, 3):
            selected = snapshot(part_number=part, part_total=3, asin=f"PARTASIN0{part}")
            tagged(
                self.path, title=f"The Lantern Archive Part {part} of 3", asin=f"PARTASIN0{part}"
            )
            self.assertEqual(
                identify([self.path], self.context(selected=selected)).outcome, "exact"
            )
            tagged(self.path, title=f"The Lantern Archive Part {part} of 3", asin=None)
            result = identify([self.path], self.context(work=True, selected=selected))
            self.assertEqual(result.outcome, "work")
            self.assertEqual(result.observed["recording_part"], (part, 3))

    def test_multitrack_conflicting_ids_never_become_exact(self):
        tagged(self.path)
        second = tagged(self.path.with_name("2.mp3"), asin="OTHERASIN1")
        self.assertEqual(identify([self.path, second], self.context(work=True)).outcome, "work")
        self.assertEqual(identify([self.path, second], self.context()).outcome, "conflict")
        unrelated = self.path.with_name("notes.txt")
        unrelated.write_text("ASIN: MOCKASIN01")
        self.assertEqual(inspect_audio([unrelated])["audio_files"], 0)

    def test_work_export_has_no_selected_recording_metadata(self):
        exported = convert_work(snapshot(), "fixture-import")
        data = json.loads(exported.metadata_json)
        self.assertEqual(
            data,
            {
                "title": "The Lantern Archive",
                "authors": ["Example Author"],
                "series": ["The Lantern Cycle #1"],
            },
        )
        self.assertNotIn("snapshot", json.loads(exported.provenance_json))
        for field in ("asin", "isbn", "narrators", "publisher", "publishedYear"):
            self.assertNotIn(field, data)

    def test_generic_handoff_freezes_native_source_binding_and_survives_restart(self):
        selected = snapshot(asin=None, isbn_10=None, isbn_13=None)
        data, context = resolve_release(
            {
                "source": "audiobookbay",
                "source_id": "native-id",
                "content_type": "audiobook",
                "work_context": {"provider_id": selected.identity.provider_id, "candidate_ids": []},
            },
            lambda identity: selected,
        )
        self.assertEqual(data["title"], "The Lantern Archive")
        self.assertTrue(context.work)
        restored = FrozenEditionContext.from_json(context.to_json())
        restored.validate_release("audiobookbay", "native-id")
        self.assertEqual(restored, context)
        with self.assertRaises(ValueError):
            restored.validate_release("prowlarr", "native-id")

    def test_request_binding_preserves_explicit_work_intention(self):
        book = {
            "provider": "edition_metadata",
            "provider_id": "mock:work",
            "download_intent": "work",
            "edition_candidate_ids": ["mock:work"],
        }
        release = {"source": "prowlarr", "source_id": "release"}
        bound = bind_request_locator(book, release)
        self.assertEqual(bound["work_context"]["provider_id"], "mock:work")
        self.assertNotIn("edition_context", bound)
        with self.assertRaises(ValueError):
            bind_request_locator(book, {**release, "edition_context": {}})
        with self.assertRaises(ValueError):
            bind_request_locator(
                book, {**release, "work_context": {"provider_id": "different-work"}}
            )
        with self.assertRaises(ValueError):
            bind_request_locator({"provider": "hardcover", "provider_id": "another"}, bound)

    def test_cached_discovery_avoids_catalog_lookup(self):
        backend = MockBackend()
        provider = EditionMetadataProvider(backend)
        books = provider.search(MetadataSearchOptions(query="Lantern"))
        with patch.object(backend, "get", side_effect=AssertionError("No extra lookup")):
            self.assertIsNotNone(provider.get_snapshot(books[0].provider_id))

    def test_batched_ownership_refresh_is_authenticated_cached_and_library_confirmed(self):
        from app_harness import AppHarness
        from test_ownership import FixtureLibrary

        from shelfmark.core import library_index
        from shelfmark.core.library_providers.audiobookshelf import _item_to_entry
        from shelfmark.core.metadata_routes import register_metadata_routes

        backend = MockBackend()
        provider = EditionMetadataProvider(backend)
        books = provider.search(MetadataSearchOptions(query="Lantern"))
        library = FixtureLibrary(
            [
                _item_to_entry(
                    {
                        "media": {
                            "metadata": {
                                "title": "The Lantern Archive",
                                "authorName": "Example Author",
                                "seriesName": "The Lantern Cycle #1",
                            }
                        }
                    }
                )
            ]
        )
        harness = AppHarness()
        harness.app.view_functions.pop("api_metadata_search")
        register_metadata_routes(
            harness.app,
            login_required=harness.login,
            resolve_auth_mode=lambda: "builtin",
            attach_library_ownership=lambda book, data: None,
        )
        client = harness.app.test_client()
        ids = [book.provider_id for book in books]
        self.assertNotEqual(
            client.post("/api/metadata/ownership", json={"ids": ids}).status_code, 200
        )
        with client.session_transaction() as session:
            session.update(user_id="fixture", db_user_id=2, is_admin=False)
        with (
            patch("shelfmark.metadata_providers.get_provider", return_value=provider),
            patch.object(library_index, "all_providers", return_value=[library]) as providers,
            patch.object(library_index, "_cache", {}),
            patch.object(library, "fetch_entries", wraps=library.fetch_entries) as fetch,
            patch.object(
                backend, "get", side_effect=AssertionError("No catalog query for ownership")
            ),
        ):
            response = client.post("/api/metadata/ownership", json={"ids": ids})
            self.assertEqual(response.status_code, 200)
            for holding in response.get_json()["books"].values():
                self.assertTrue(holding["work_owned"])
                self.assertIsNone(holding["library"]["audiobook"])
            self.assertEqual(fetch.call_count, 1)
            library.failure = True
            response = client.post("/api/metadata/ownership", json={"ids": ids})
            self.assertTrue(
                all(holding["work_owned"] for holding in response.get_json()["books"].values())
            )
            providers.return_value = []  # Disabled/inaccessible providers supply no ownership.
            response = client.post("/api/metadata/ownership", json={"ids": ids})
            self.assertFalse(
                any(holding["work_owned"] for holding in response.get_json()["books"].values())
            )

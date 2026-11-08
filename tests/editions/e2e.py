"""Synthetic catalog/release transports, real native queue/output and scratch ABS.

Run only through run_e2e.py. ABS watchers are disabled so successful ingestion
demonstrates the fork's automatic scan notification. All audio is synthetic.
"""

import hashlib
import json
import os
import shutil
import socket
import time
import wave
from pathlib import Path
from threading import Event
from unittest.mock import MagicMock, patch
from urllib.request import Request, urlopen

from mutagen import File
from mutagen.id3 import TALB, TCOM, TLAN, TPUB, TXXX
from mutagen.mp4 import MP4

os.environ.update(
    {
        "CONFIG_DIR": "/tmp/config",
        "LOG_ROOT": "/tmp/logs",
        "TMP_DIR": "/tmp/staging",
        "ENABLE_LOGGING": "false",
        "METADATA_PROVIDER_AUDIOBOOK": "edition_metadata",
        "EDITION_LIBEX_ENABLED": "true",
        "DESTINATION_AUDIOBOOK": "/media/Audiobooks",
        "FILE_ORGANIZATION_AUDIOBOOK": "organize",
        "TEMPLATE_AUDIOBOOK_ORGANIZE": "{Author}/{Title}/{Title} - {PartNumber}",
        "SUPPORTED_AUDIOBOOK_FORMATS": "m4b,mp3,wav",
        "AUDIOBOOKSHELF_URL": "http://127.0.0.1:18087",
        "LIBRARY_CHECK_AUDIOBOOKSHELF_ENABLED": "true",
    }
)

# A second boundary in addition to the Docker internal network: only scratch ABS.
native_connect = socket.socket.connect


def local_only(connection, address):
    if not isinstance(address, tuple) or address[0] not in {"127.0.0.1", "::1"}:
        raise AssertionError("External connections are prohibited")
    return native_connect(connection, address)


socket.socket.connect = local_only

from app_harness import AppHarness
from test_search_refinement import RegionalHttp

from shelfmark.core import library_index
from shelfmark.core.config import config
from shelfmark.core.edition_handoff import EditionLocator, context_from_task
from shelfmark.core.edition_ownership import ownership_evidence, work_in_entries
from shelfmark.core.library_providers import audiobookshelf
from shelfmark.core.queue import book_queue
from shelfmark.core.release_routes import register_release_routes
from shelfmark.core.search_plan import build_release_search_plan
from shelfmark.download import orchestrator
from shelfmark.download.outputs import edition as edition_output
from shelfmark.download.outputs.folder import process_folder_output
from shelfmark.metadata_providers import MetadataSearchOptions, get_configured_provider
from shelfmark.metadata_providers.editions.source_http import HttpClient
from shelfmark.release_sources.prowlarr.source import ProwlarrSource

BASE = "http://127.0.0.1:18087"
token = ""


def api(route, method="GET", body=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    request = Request(
        BASE + route,
        headers=headers,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
    )
    with urlopen(request, timeout=10) as response:
        raw = response.read().decode()
    try:
        return json.loads(raw)
    except ValueError:
        return raw


for _attempt in range(100):
    try:
        api("/ping")
        break
    except OSError:
        time.sleep(0.1)
else:
    raise AssertionError("Scratch ABS did not start")
api("/init", "POST", {"newRoot": {"username": "fixture", "password": "Synthetic-Only-42851"}})
token = api("/login", "POST", {"username": "fixture", "password": "Synthetic-Only-42851"})["user"][
    "accessToken"
]
library = Path("/media/Audiobooks")
library.mkdir()
created = api(
    "/api/libraries",
    "POST",
    {
        "name": "Native fork fixture",
        "folders": [{"fullPath": str(library)}],
        "mediaType": "book",
        "settings": {"disableWatcher": True},
    },
)
library_id = created["id"]
assert created["settings"]["disableWatcher"] is True
os.environ["AUDIOBOOKSHELF_TOKEN"] = token
os.environ["AUDIOBOOKSHELF_LIBRARY_IDS"] = library_id
config.refresh(force=True)

http = RegionalHttp(2)
extra = []
for number, narrator, language in (
    (3, "Conventional Narrator", "english"),
    (4, "Different Narrator", "english"),
    (5, "French Narrator", "french"),
    (6, "Unidentified Fixture Narrator", "english"),
):
    row = dict(http.books[0])
    row.update(
        asin=f"B0TEST00{number:02d}",
        title="The Lantern Archive",
        narrators=[{"name": narrator}],
        publisher="Fixture Press",
        language=language,
        isbn=None,
        skuGroup=f"BK_TEST_{number:06d}",
        series=[{"name": "The Lantern Cycle", "position": "2"}],
    )
    extra.append(row)
    if number == 6:
        row.update(
            title="The Lantern Promise", series=[{"name": "The Lantern Cycle", "position": "3"}]
        )
http.books = (*http.books, *extra)
for row in http.books:
    row["imageUrl"] = "https://m.media-amazon.com/images/synthetic.jpg"
    row["lengthMinutes"] = 2 if "Part " in row["title"] or row["asin"] == "B0TEST0004" else 1


def items():
    results = api(f"/api/libraries/{library_id}/items?limit=50")["results"]
    return [api("/api/items/" + item["id"] + "?expanded=1") for item in results]


def make_audio(label, extension, count):
    directory = Path("/media/Torrents") / label
    directory.mkdir(parents=True)
    for number in range(1, count + 1):
        path = directory / f"track-{number:02d}.{extension}"
        if extension == "wav":
            with wave.open(str(path), "wb") as audio:
                audio.setnchannels(1)
                audio.setsampwidth(2)
                audio.setframerate(8000)
                audio.writeframes((b"\x01\x00" if label == "partial" else b"\x00\x00") * 8000 * 60)
        else:
            shutil.copyfile(f"/media/fixtures/track.{extension}", path)
    return directory


def tag_audio(directory, edition):
    """Only generated test audio is tagged; production inspection is read-only."""
    for path in directory.iterdir():
        audio = File(path)
        assert audio is not None
        if audio.tags is None:
            audio.add_tags()
        if isinstance(audio, MP4):
            audio["©alb"] = [edition.title]
            audio["©wrt"] = list(edition.authors)
            for key, values in (
                ("ASIN", [edition.asin]),
                ("NARRATOR", list(edition.narrators)),
                ("PUBLISHER", [edition.publisher]),
                ("LANGUAGE", [edition.language]),
            ):
                audio["----:com.apple.iTunes:" + key] = [
                    value.encode() for value in values if value
                ]
        else:
            audio.tags.add(TALB(encoding=3, text=[edition.title]))
            audio.tags.add(TCOM(encoding=3, text=list(edition.authors)))
            audio.tags.add(TLAN(encoding=3, text=[edition.language or "en"]))
            audio.tags.add(TPUB(encoding=3, text=[edition.publisher or ""]))
            audio.tags.add(TXXX(encoding=3, desc="ASIN", text=[edition.asin]))
            audio.tags.add(TXXX(encoding=3, desc="NARRATOR", text=list(edition.narrators)))
        audio.save()


with (
    patch.object(HttpClient, "get", side_effect=http.get),
    patch.object(orchestrator, "_source_unavailable_message", return_value=None),
    patch.object(orchestrator, "_build_retry_resolution_fields", return_value={}),
    patch.object(orchestrator, "ws_manager", None),
):
    provider = get_configured_provider("audiobook")
    assert provider and provider.is_available()
    books = provider.search(MetadataSearchOptions(query="Lantern"))
    selected = {provider.get_snapshot(book.provider_id).edition.asin: book for book in books}
    ga = sorted(
        (book for book in books if provider.get_snapshot(book.provider_id).edition.part_number),
        key=lambda book: book.title,
    )
    assert len(ga) == 2
    french = provider.search(
        MetadataSearchOptions(query="", fields={"series": "The Lantern Cycle"}, language="fr")
    )
    assert french and all(book.language in {"french", "fr"} for book in french)
    harness = AppHarness()
    for endpoint in ("api_download_release", "api_inspect_release"):
        harness.app.view_functions.pop(endpoint)
    register_release_routes(
        harness.app,
        harness.login,
        resolve_auth_mode=lambda: harness.mode,
        get_user_db=lambda: harness.db,
        load_policy_settings=lambda: harness.settings,
        resolve_status_scope=lambda **kwargs: (False, 2, True),
        attach_library_ownership=lambda book, data: None,
    )
    web = harness.app.test_client()
    with web.session_transaction() as session:
        session.update(user_id="fixture", db_user_id=2, is_admin=False)

    def queue(book, label, source="prowlarr", extension="wav", work=False):
        if source == "prowlarr":
            indexer = MagicMock()
            indexer.indexer_timeout = 10
            indexer.get_enabled_indexers_detailed.return_value = [
                {"id": 7, "name": "Synthetic fixture"}
            ]
            indexer.get_enriched_indexer_ids.return_value = []
            indexer.get_disabled_indexers.return_value = {}
            indexer.torznab_search.return_value = [
                {
                    "guid": label,
                    "title": book.title + " [ENG / M4B]",
                    "indexerId": 7,
                    "protocol": "torrent",
                    "downloadUrl": "https://example.invalid/synthetic",
                }
            ]
            source_object = ProwlarrSource()
            with (
                patch.object(source_object, "_get_client", return_value=indexer),
                patch.object(source_object, "_get_selected_indexer_ids", return_value=[7]),
                patch("shelfmark.release_sources.prowlarr.source.config") as settings,
            ):
                settings.get.side_effect = lambda key, default=None: default
                releases = source_object.search(
                    book, build_release_search_plan(book), content_type="audiobook"
                )
            assert len(releases) == 1 and indexer.torznab_search.call_count == 1
            identity = releases[0].source_id
        else:
            identity = label
        payload = {
            "source": source,
            "source_id": identity,
            "title": book.title,
            "release_title": book.title,
            "content_type": "audiobook",
            "author": "Example Author",
            "format": extension,
            "edition_context": EditionLocator(
                ownership_evidence(provider.get_snapshot(book.provider_id).edition).identity
            ).to_dict(),
        }
        if work:
            payload.pop("edition_context")
            payload["work_context"] = {
                "provider_id": book.provider_id,
                "candidate_ids": [
                    candidate.provider_id
                    for candidate in books
                    if candidate.series_position == book.series_position
                ],
            }
        response = web.post("/api/releases/download", json=payload)
        assert response.status_code == 200, response.get_json()
        task = book_queue.get_task(identity)
        assert context_from_task(task).snapshot.identity.provider_id == book.provider_id
        return task

    scenarios = [
        (ga[0], "ga-part-1", "prowlarr", "wav", 2),
        (ga[1], "ga-part-2", "audiobookbay", "wav", 2),
        (selected["B0TEST0003"], "conventional", "prowlarr", "m4b", 1),
        (selected["B0TEST0004"], "other-recording", "prowlarr", "mp3", 2),
        (selected["B0TEST0003"], "different-format", "prowlarr", "mp3", 1),
    ]
    directories = {}
    for index, (book, label, source_name, extension, count) in enumerate(scenarios):
        source = make_audio(label, extension, count)
        tag_audio(source, provider.get_snapshot(book.provider_id).edition)
        sources = sorted(source.iterdir())
        digests = [hashlib.sha256(path.read_bytes()).digest() for path in sources]
        task = queue(book, label, source_name, extension)
        task.original_download_path = str(source) if source_name == "prowlarr" else None
        states = []
        result = process_folder_output(
            source,
            task,
            Event(),
            lambda state, message=None, states=states: states.append(state),
            preserve_source_on_failure=True,
        )
        assert result and states[-1] == "complete", (label, states)
        final = [Path(path) for path in task.download_paths]
        output = final[0].parent
        directories[label] = output
        context = context_from_task(task)
        assert (
            json.loads((output / "metadata.json").read_text())["asin"]
            == context.snapshot.edition.asin
        )
        assert (
            json.loads((output / ".shelfmark-edition.json").read_text())["snapshot"]
            == context.snapshot.to_dict()
        )
        assert [hashlib.sha256(path.read_bytes()).digest() for path in final] == digests
        assert all(
            (origin.stat().st_ino == target.stat().st_ino) == (source_name == "prowlarr")
            for origin, target in zip(sources, final, strict=True)
        )
        for _attempt in range(150):
            observed = items()
            if len(observed) == index + 1:
                break
            time.sleep(0.1)
        else:
            raise AssertionError(f"Automatic ABS scan failed for {label}")
        entries = [audiobookshelf._item_to_entry(item) for item in observed]
        assert library_index.match_entries(book, entries) == "owned"
        if index == 0:
            assert library_index.match_entries(ga[1], entries) is None
            assert work_in_entries(ga[1], entries)
        print(
            "PASS",
            label,
            "native queue/output, exact metadata, automatic ABS scan and ownership",
            flush=True,
        )
    assert directories["conventional"] != directories["different-format"]
    # Existing completion reuses the same exact output without another scan.
    book, label, source_name, extension, count = scenarios[-1]
    existing = queue(book, "completed-again", source_name, extension)
    existing.original_download_path = str(Path("/media/Torrents") / label)
    with patch.object(edition_output, "notify_scan") as scan:
        result = process_folder_output(
            Path(existing.original_download_path),
            existing,
            Event(),
            lambda *args: None,
            preserve_source_on_failure=True,
        )
    assert Path(result).parent == directories[label]
    scan.assert_not_called()
    # A failed transfer must not complete or notify ABS; native retry resumes it.
    failed_source = make_audio("partial", "wav", 2)
    tag_audio(failed_source, provider.get_snapshot(ga[0].provider_id).edition)
    failed = queue(ga[0], "partial")
    failed.original_download_path = str(failed_source)
    with (
        patch(
            "shelfmark.download.postprocess.pipeline.transfer_book_files",
            return_value=([], "synthetic failure", {}),
        ),
        patch.object(edition_output, "notify_scan") as scan,
    ):
        states = []
        assert (
            process_folder_output(
                failed_source,
                failed,
                Event(),
                lambda state, message=None: states.append(state),
                preserve_source_on_failure=True,
            )
            is None
        )
    assert states[-1] == "error"
    scan.assert_not_called()
    assert process_folder_output(
        failed_source, failed, Event(), lambda *args: None, preserve_source_on_failure=True
    )
    print(
        "PASS exact completed output, failed/partial transfer recovery and language-specific series",
        flush=True,
    )
    # Generic Get book can associate a different returned recording after inspecting real MP3 tags.
    requested, actual = selected["B0TEST0003"], selected["B0TEST0004"]
    source = make_audio("generic-identified", "mp3", 2)
    tag_audio(source, provider.get_snapshot(actual.provider_id).edition)
    before = [hashlib.sha256(path.read_bytes()).digest() for path in sorted(source.iterdir())]
    task = queue(requested, "generic-identified", "prowlarr", "mp3", work=True)
    task.original_download_path = str(source)
    result = process_folder_output(
        source, task, Event(), lambda *args: None, preserve_source_on_failure=True
    )
    assert result and task.output_args["edition_identification"] == "exact"
    assert json.loads((Path(result).parent / "metadata.json").read_text())["asin"] == "B0TEST0004"
    assert [
        hashlib.sha256(Path(path).read_bytes()).digest() for path in task.download_paths
    ] == before
    # An explicit request may not silently substitute that same recording.
    task = queue(requested, "explicit-conflict", "prowlarr", "mp3")
    task.original_download_path = str(source)
    errors = []
    assert (
        process_folder_output(
            source,
            task,
            Event(),
            lambda state, message=None: errors.append((state, message)),
            preserve_source_on_failure=True,
        )
        is None
    )
    assert errors[-1][0] == "error" and "Get book" in errors[-1][1]
    assert all(path.is_file() for path in source.iterdir())
    # Untagged M4B through native AudiobookBay: literary facts only, confirmed by real ABS.
    book = selected["B0TEST0006"]
    source = make_audio("generic-unknown", "m4b", 1)
    original = next(source.iterdir())
    before = hashlib.sha256(original.read_bytes()).digest()
    task = queue(book, "generic-unknown", "audiobookbay", "m4b", work=True)
    result = process_folder_output(
        source, task, Event(), lambda *args: None, preserve_source_on_failure=True
    )
    assert result and task.output_args["edition_identification"] == "work"
    assert hashlib.sha256(Path(result).read_bytes()).digest() == before
    assert original.stat().st_ino != Path(result).stat().st_ino
    metadata = json.loads((Path(result).parent / "metadata.json").read_text())
    assert metadata["title"] == "The Lantern Promise"
    assert not set(metadata) & {"asin", "isbn", "narrators", "publisher", "publishedYear"}
    assert "snapshot" not in json.loads(
        (Path(result).parent / ".shelfmark-edition.json").read_text()
    )
    for _attempt in range(150):
        entries = [audiobookshelf._item_to_entry(item) for item in items()]
        if work_in_entries(book, entries):
            break
        time.sleep(0.1)
    else:
        raise AssertionError("ABS did not confirm work-level import")
    assert library_index.match_entries(book, entries) is None
    print(
        "PASS generic identified recording, explicit conflict, unknown-edition export and confirmed work-only ABS ownership",
        flush=True,
    )
print(
    "PASS synthetic acceptance; no production mounts, live downloads or external network",
    flush=True,
)

"""Recording metadata publication for native folder output."""

import hashlib
import json
import os
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

from shelfmark.core.edition_handoff import (
    _check_advisory_titles,
    _validate_selected_recording,
    context_from_task,
)
from shelfmark.core.edition_ownership import _owned, library_recording, ownership_evidence
from shelfmark.core.logger import setup_logger
from shelfmark.download.fs import run_blocking_io
from shelfmark.download.outputs.abs_metadata import MetadataUrlError, convert_snapshot, convert_work
from shelfmark.download.outputs.audio_identification import identify
from shelfmark.metadata_providers.editions.snapshot import EditionSnapshot

if TYPE_CHECKING:
    from threading import Event

    from shelfmark.core.models import DownloadTask

logger = setup_logger(__name__)


class OutputError(ValueError):
    """Fixed, safe explanations that can be shown in task history."""


def failure_message(error: Exception, stage: str) -> str:
    # Exception text can contain a private URL or filesystem path.
    logger.warning(
        "Edition output %s failed: %s (errno=%s)",
        stage,
        type(error).__name__,
        getattr(error, "errno", None),
    )
    if isinstance(error, OutputError):
        return str(error)
    if isinstance(error, FileNotFoundError):
        return (
            "Source audio file is missing"
            if stage == "preparation"
            else "Audio transfer is incomplete"
        )
    if isinstance(error, PermissionError):
        return "Audio output permission denied"
    return (
        "Audio transfer failed"
        if stage == "transfer"
        else "Edition output could not be prepared or verified"
    )


def _digest(path: Path) -> bytes:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").digest()


def _safe_path(path: Path) -> None:
    if not path.is_absolute() or ".." in path.parts:
        raise OutputError("Unsafe audiobook destination path")
    for component in (*reversed(path.parents), path):
        if component.is_symlink() or component.is_junction():
            raise OutputError("Audio paths cannot contain symlinks or junctions")


def _case_path(path: Path) -> Path:
    """Use existing spelling on Windows mounts; avoid case-only forks on Linux."""
    result = Path(path.anchor)
    for name in path.parts[1:]:
        if result.is_dir():
            matches = sorted(p for p in result.iterdir() if p.name.casefold() == name.casefold())
            result = next(
                (p for p in matches if p.name == name), matches[0] if matches else result / name
            )
        else:
            result /= name
        _safe_path(result)
    return result


def _legacy_complete(directory: Path) -> list[Path]:
    """Older single-file imports have native completion history, but no inventory."""
    from shelfmark.config.env import CONFIG_DIR
    from shelfmark.core.utils import AUDIOBOOK_FORMATS

    files = [p for p in directory.iterdir() if p.suffix.lstrip(".").lower() in AUDIOBOOK_FORMATS]
    database = CONFIG_DIR / "users.db"
    if len(files) != 1 or not database.is_file():
        return []
    try:
        with closing(
            sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
        ) as connection:
            completed = connection.execute(
                "SELECT 1 FROM download_history WHERE final_status='complete' "
                "AND download_path=? LIMIT 1",
                (str(files[0]),),
            ).fetchone()
        return files if completed else []  # noqa: TRY300 -- Keep the existing nonblocking failure boundary.
    except sqlite3.Error:
        return []  # Unproven completion is never treated as success.


@dataclass(frozen=True)
class Publication:
    paths: tuple[Path, ...]
    sizes: tuple[int, ...]
    sidecars: tuple[tuple[Path, str], ...]
    sources: tuple[Path, ...]
    transfer_paths: tuple[Path, ...]
    already_present: bool = False

    def verify(self, paths: list[Path], cancel: Event) -> None:
        if cancel.is_set():
            raise OutputError("Audio transfer was cancelled")
        if tuple(paths) != self.transfer_paths:
            raise OutputError("Audio transfer returned unexpected destination paths")
        for path, size in zip(self.paths, self.sizes, strict=True):
            _safe_path(path)
            if not path.is_file() or path.stat().st_size != size:
                raise OutputError("Audio transfer is incomplete")
        for path, value in self.sidecars:
            _safe_path(path)
            if path.read_text(encoding="utf-8") != value:
                raise OutputError("Edition metadata changed during transfer")
        if not self.already_present:
            path, text = self.sidecars[1]
            data = json.loads(text)
            data["files"] = [
                {"name": p.name, "size": size}
                for p, size in zip(self.paths, self.sizes, strict=True)
            ]
            path.write_text(
                json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                encoding="utf-8",
            )


def prepare(
    task: DownloadTask, files: list[Path], destination: Path, organization_mode: str, cancel: Event
) -> Publication | None:
    try:
        context = context_from_task(task)
    except ValueError:
        raise OutputError(
            "Edition snapshot is invalid or conflicts with the selected recording"
        ) from None
    if context is None:
        return None
    from shelfmark.core.config import config
    from shelfmark.core.library_providers import LibraryEntry
    from shelfmark.core.naming import assign_part_numbers, build_library_path, sanitize_filename
    from shelfmark.core.utils import AUDIOBOOK_FORMATS
    from shelfmark.download.postprocess import transfer
    from shelfmark.download.postprocess.packs import drop_ignored_files, group_files_into_books
    from shelfmark.download.postprocess.policy import get_template, get_word_separator

    if task.multi_book or task.book_plan:
        raise OutputError("Download layout contains multiple books")
    if task.content_type != "audiobook" or organization_mode != "organize":
        raise OutputError("Edition export requires organize folder output for one audiobook")
    if getattr(config, "CUSTOM_SCRIPT", None):
        raise OutputError("Edition export does not support custom output scripts")
    if cancel.is_set():
        raise OutputError("Audio transfer was cancelled")
    snapshot = context.snapshot
    try:
        if not context.work:
            _validate_selected_recording(snapshot)
    except ValueError:
        raise OutputError("Edition recording identifiers or frozen metadata are invalid") from None
    try:
        if not context.work:
            _check_advisory_titles({"release_title": task.retry_release_name}, snapshot)
    except ValueError:
        raise OutputError("Release metadata conflicts with the selected recording") from None
    files = drop_ignored_files(files, task.ignored_files)
    if not files or any(not p.is_file() for p in files):
        raise OutputError("Source audio file is missing")
    for source in files:
        _safe_path(source)
    folders = {p.parent for p in files}
    discs = len({p.parent for p in folders}) == 1 and all(
        re.fullmatch(r"(?:cd|disc|disk)[ _-]*\d+", p.name, re.IGNORECASE) for p in folders
    )
    if (
        len(folders) > 1
        and not discs
        and len(
            group_files_into_books(files, series_name=task.series_name, author_name=task.author)
        )
        != 1
    ):
        raise OutputError("Download layout contains multiple books or ambiguous source folders")
    identification = identify(files, context)
    if identification.outcome == "conflict":
        raise OutputError(identification.reason)
    task.output_args["edition_identification"] = identification.outcome
    if identification.snapshot is not None:
        snapshot = identification.snapshot
        task.output_args["identified_edition"] = snapshot.identity.provider_id
    import_id = hashlib.sha256(f"{context.source}:{context.source_id}".encode()).hexdigest()
    try:
        if identification.outcome == "exact":
            exported = convert_snapshot(snapshot)
        else:
            exported = convert_work(
                snapshot,
                import_id,
                identification.observed.get("recording_part"),
            )
        published_metadata = json.loads(exported.metadata_json)
        # Naming must follow what will actually be published, not the selected recording.
        task.title = published_metadata["title"]
        task.author = ", ".join(published_metadata.get("authors", []))
        task.narrator = ", ".join(published_metadata.get("narrators", [])) or None
        task.year = published_metadata.get("publishedYear")
        task.subtitle = published_metadata.get("subtitle")
    except MetadataUrlError:
        raise OutputError("Edition metadata contains an unsupported URL") from None
    except ValueError:
        raise OutputError("Edition metadata is invalid or unsupported") from None
    _safe_path(destination)
    template = get_template(is_audiobook=True, organization_mode="organize")
    numbered: list[tuple[Path, str | None]] = []
    if len(files) > 1:
        numbered.extend(assign_part_numbers(sorted(files), max(len(str(len(files))), 2)))
    else:
        numbered.append((files[0], None))
    try:
        paths = tuple(
            build_library_path(
                str(destination),
                template,
                transfer.build_file_metadata(task, source, part_number=part),
                extension=source.suffix.lstrip(".") or task.format or None,
                word_separator=get_word_separator(),
            )
            for source, part in numbered
        )
    except ValueError:
        raise OutputError("Unsafe audiobook destination path or naming template") from None
    sizes = tuple(source.stat().st_size for source, _ in numbered)
    if any(size == 0 for size in sizes):
        raise OutputError("Source audio file is empty")
    directory = paths[0].parent
    if (
        any(path.parent != directory for path in paths)
        or directory == destination
        or not directory.is_relative_to(destination)
        or any(p.name.casefold() in {"metadata.json", ".shelfmark-edition.json"} for p in paths)
    ):
        raise OutputError("Unsafe output template: one dedicated audiobook directory is required")
    names = [p.name for p in paths]
    if len({n.casefold() for n in names}) != len(names):
        names = [
            sanitize_filename(p.stem, max_length=220) + f" - {i:02d}" + p.suffix
            for i, p in enumerate(paths, 1)
        ]
    edition = snapshot.edition
    label = next(
        (
            v
            for v in (
                f"Part {edition.part_number}" if edition.part_number else None,
                ", ".join(edition.narrators),
                str(edition.publish_year) if edition.publish_year else None,
                edition.publisher,
            )
            if v and v.casefold() not in directory.name.casefold()
        ),
        "Edition",
    )
    if identification.outcome == "work":
        label = "Unidentified recording"
    stable = (
        import_id[:10]
        if identification.outcome == "work"
        else hashlib.sha256(snapshot.identity.provider_id.encode()).hexdigest()[:10]
    )
    stem = sanitize_filename(directory.name, max_length=160)
    label = sanitize_filename(label, max_length=60)
    for attempt in range(100):
        suffix = (
            ""
            if attempt == 0
            else f" - {label}"
            if attempt == 1
            else f" - {label} - {stable}" + (f"-{attempt - 2}" if attempt > 2 else "")
        )
        candidate = _case_path(directory if attempt == 0 else directory.with_name(stem + suffix))
        targets = tuple(_case_path(candidate / name) for name in names)
        sidecars = (
            (candidate / "metadata.json", exported.metadata_json),
            (candidate / ".shelfmark-edition.json", exported.provenance_json),
        )
        if candidate.exists():
            if not candidate.is_dir():
                continue
            try:
                for path, _ in sidecars:
                    _safe_path(path)
                texts = tuple(path.read_text(encoding="utf-8") for path, _ in sidecars)
                previous = json.loads(texts[1])
                if identification.outcome == "work":
                    if (
                        previous.get("identification") != "work"
                        or previous.get("import_id") != import_id
                        or previous.get("work") != published_metadata
                    ):
                        continue
                    # An unresolved import has no catalog snapshot or recording claims.
                    old = None
                else:
                    old = EditionSnapshot.from_dict(previous["snapshot"])
                observed, selected = (
                    ownership_evidence(old.edition if old else edition),
                    ownership_evidence(edition),
                )
                # Extra catalog aliases do not change an otherwise identical exact primary.
                if replace(observed, external_ids=selected.external_ids) != selected:
                    continue
                metadata = json.loads(texts[0])
                if not isinstance(metadata, dict):
                    continue
                if old is None and any(
                    metadata.get(key) != value for key, value in published_metadata.items()
                ):
                    continue
                asin = metadata.get("asin")
                if asin is not None and not isinstance(asin, str):
                    continue
                entry = LibraryEntry(
                    tokens=frozenset(),
                    isbns=frozenset(),
                    asins=frozenset({asin}) if asin else frozenset(),
                    external_ids=frozenset({(old.identity.provider, old.identity.provider_id)})
                    if old
                    else frozenset(),
                )
                object.__setattr__(
                    entry,
                    "_edition_recording",
                    library_recording({"media": {"metadata": metadata}}),
                )
                if old and not _owned(selected, entry):
                    continue
                inventory = previous.get("files")
                completed = (
                    [(candidate / entry["name"], entry["size"]) for entry in inventory]
                    if isinstance(inventory, list)
                    else [(p, p.stat().st_size) for p in _legacy_complete(candidate)]
                )
                audio = {
                    p
                    for p in candidate.rglob("*")
                    if p.suffix.lstrip(".").lower() in AUDIOBOOK_FORMATS
                }
                for path in audio:
                    _safe_path(path)
                completed_paths = {p for p, _ in completed}
                if (
                    completed
                    and len(completed_paths) == len(completed)
                    and completed_paths == audio
                    and completed_paths == set(targets)
                    and dict(completed) == dict(zip(targets, sizes, strict=True))
                    and all(
                        run_blocking_io(_digest, source) == run_blocking_io(_digest, target)
                        for (source, _), target in zip(numbered, targets, strict=True)
                    )
                    and all(
                        p.parent == candidate
                        and type(size) is int
                        and size > 0
                        and not p.is_symlink()
                        and p.is_file()
                        and p.stat().st_size == size
                        for p, size in completed
                    )
                ):
                    return Publication(
                        tuple(p for p, _ in completed),
                        tuple(size for _, size in completed),
                        tuple((p, text) for (p, _), text in zip(sidecars, texts, strict=True)),
                        (),
                        (),
                        True,
                    )
                # Only resume our unchanged metadata-first output. Verify retained audio against its source.
                if texts != (exported.metadata_json, exported.provenance_json) or audio - set(
                    targets
                ):
                    continue
                remaining = []
                for (source, _), target, size in zip(numbered, targets, sizes, strict=True):
                    _safe_path(target)
                    if target.exists():
                        if (
                            not target.is_file()
                            or target.stat().st_size != size
                            or run_blocking_io(_digest, source) != run_blocking_io(_digest, target)
                        ):
                            break
                    else:
                        remaining.append((source, target))
                else:
                    return Publication(
                        targets,
                        sizes,
                        sidecars,
                        tuple(p for p, _ in remaining),
                        tuple(p for _, p in remaining),
                    )
            except OutputError:
                raise
            except OSError, ValueError, KeyError, TypeError:
                continue
        else:
            for path in targets:
                _safe_path(path)
            candidate.parent.mkdir(parents=True, exist_ok=True)
            try:
                candidate.mkdir(exist_ok=False)
            except FileExistsError:
                continue
            for path, value in sidecars:
                with path.open("x", encoding="utf-8") as stream:
                    stream.write(value)
            break
    else:
        raise OutputError("Unable to find a safe unique destination")
    for warning in exported.warnings:
        logger.warning("Edition export: %s", warning)
    return Publication(targets, sizes, sidecars, tuple(p for p, _ in numbered), targets)


def notify_scan() -> None:
    """Best effort only. Never let scan failure change file completion."""
    try:
        from shelfmark.core.library_providers import audiobookshelf as abs_client

        identity = os.environ.get("SHELFMARK_EDITION_ABS_LIBRARY_ID", "").strip()
        _, url, token, libraries = abs_client._config()
        if not url or not token:
            raise ValueError("Configure the existing Shelfmark ABS URL and token")  # noqa: TRY301 -- Keep validation beside the parsed field.
        with abs_client._session(url, token) as session:
            if not identity:
                if len(libraries) == 1:
                    identity = libraries[0]
                else:
                    response = session.get(
                        f"{url}/api/libraries", timeout=15, allow_redirects=False
                    )
                    response.raise_for_status()
                    candidates = [
                        entry["id"]
                        for entry in response.json()["libraries"]
                        if entry.get("mediaType") == "book"
                        and (not libraries or entry.get("id") in libraries)
                    ]
                    if len(candidates) != 1:
                        raise ValueError("Select the destination ABS library ID")  # noqa: TRY301 -- Keep validation beside the parsed field.
                    identity = candidates[0]
            if not isinstance(identity, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", identity):
                raise ValueError("Invalid destination ABS library ID")  # noqa: TRY301 -- Keep validation beside the parsed field.
            response = session.post(
                f"{url}/api/libraries/{identity}/scan", timeout=15, allow_redirects=False
            )
            if 200 <= response.status_code < 300:
                from shelfmark.core import library_index

                library_index.invalidate("audiobookshelf")
                logger.info(
                    "ABS scan request accepted; completion is asynchronous. Periodic scans recover missed imports."
                )
            else:
                logger.warning(
                    "ABS scan request returned HTTP %d; files remain complete. Check permissions/availability; periodic scan or manual scan can recover.",
                    response.status_code,
                )
    except Exception:  # noqa: BLE001 -- Optional source/scan failures must not invalidate valid results.
        # Never log exception text: requests errors may include credential-bearing URLs.
        logger.warning(
            "ABS scan notification failed; files remain complete. Check destination library, ABS credentials and availability; periodic scan or manual scan can recover."
        )

"""Offline, narrow compatibility edits, after Shelfmark stops. No app imports.

Edition requests are hidden from stock approval/reopen routes; history remains
visible with retries withheld. Original rows/payloads stay in users.db. This is
task preservation, not release or deployment state tracking.
"""

import argparse
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Any

PROVIDERS = {"METADATA_PROVIDER", "METADATA_PROVIDER_AUDIOBOOK", "METADATA_PROVIDER_COMBINED"}


def native_settings(data: dict[str, Any]) -> dict[str, Any]:
    result = dict(data)
    for key, value in data.items():
        if key in PROVIDERS and value == "edition_metadata":
            result[key] = "openlibrary"
        elif key.startswith(("EDITION_", "SHELFMARK_EDITION_")) and isinstance(value, bool):
            result[key] = False
        elif key.startswith(("EDITION_", "SHELFMARK_EDITION_")) and value in (
            "true",
            "1",
            "yes",
            "on",
        ):
            result[key] = "false"
        elif (
            key == "TEMPLATE_AUDIOBOOK_ORGANIZE"
            and value == "{Author}/{Title}/{Title} - {PartNumber}"
        ):
            result[key] = "{Author}/{Title}/{Title}"
    return result


def incompatible(value: object) -> bool:
    if isinstance(value, str) and ("edition_metadata" in value or "edition_context" in value):
        return True
    if isinstance(value, dict):
        return "edition_context" in value or any(incompatible(v) for v in value.values())
    if isinstance(value, list):
        return any(incompatible(v) for v in value)
    return False


def edit_database(path: Path, official: bool) -> tuple[int, int]:
    if not path.exists():
        return 0, 0
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN IMMEDIATE")
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        db.execute(
            "CREATE TABLE IF NOT EXISTS edition_fallback_hold (kind TEXT, row_id INTEGER, payload TEXT NOT NULL, PRIMARY KEY(kind, row_id))"
        )
        counts = [0, 0]
        if official:
            for i, table in enumerate(("download_requests", "download_history")):
                if table not in tables:
                    continue
                for row in db.execute(f"SELECT * FROM {table}").fetchall():
                    data = dict(row)
                    if not incompatible(data):
                        continue
                    # Never replace a previous preserved payload on repeated fallback.
                    db.execute(
                        "INSERT OR IGNORE INTO edition_fallback_hold VALUES (?, ?, ?)",
                        (table, data["id"], json.dumps(data)),
                    )
                    if table == "download_requests":
                        db.execute("DELETE FROM download_requests WHERE id=?", (data["id"],))
                    else:
                        db.execute(
                            "UPDATE download_history SET retry_payload=NULL WHERE id=?",
                            (data["id"],),
                        )
                    counts[i] += 1
            if "user_settings" in tables:
                for row in db.execute(
                    "SELECT user_id, settings_json FROM user_settings"
                ).fetchall():
                    data = json.loads(row["settings_json"])
                    updated = native_settings(data)
                    if updated != data:
                        changes = {
                            key: value for key, value in data.items() if updated[key] != value
                        }
                        db.execute(
                            "INSERT OR IGNORE INTO edition_fallback_hold VALUES (?, ?, ?)",
                            ("user_settings", row["user_id"], json.dumps(changes)),
                        )
                        db.execute(
                            "UPDATE user_settings SET settings_json=? WHERE user_id=?",
                            (json.dumps(updated), row["user_id"]),
                        )
        else:
            for row in db.execute("SELECT * FROM edition_fallback_hold").fetchall():
                table, row_id, data = row["kind"], row["row_id"], json.loads(row["payload"])
                if table == "download_requests":
                    if db.execute(
                        "SELECT 1 FROM download_requests WHERE id=?", (row_id,)
                    ).fetchone():
                        raise RuntimeError(
                            "Held request ID is occupied; manual task review required"
                        )
                    columns = list(data)
                    # Columns originate from our own stored schema, never an HTTP payload.
                    actual = {r[1] for r in db.execute("PRAGMA table_info(download_requests)")}
                    if not set(columns) <= actual:
                        raise RuntimeError("Held request schema changed")
                    names = ",".join('"' + c.replace('"', '""') + '"' for c in columns)
                    placeholders = ",".join("?" for _ in columns)
                    db.execute(
                        f"INSERT INTO download_requests ({names}) VALUES ({placeholders})",
                        list(data.values()),
                    )
                elif table == "download_history":
                    changed = db.execute(
                        "UPDATE download_history SET retry_payload=? WHERE id=? AND task_id=? AND retry_payload IS NULL",
                        (data["retry_payload"], row_id, data["task_id"]),
                    )
                    if changed.rowcount != 1:
                        raise RuntimeError("Held retry changed; manual task review required")
                elif table == "user_settings":
                    current = db.execute(
                        "SELECT settings_json FROM user_settings WHERE user_id=?", (row_id,)
                    ).fetchone()
                    if current:
                        settings = json.loads(current[0])
                        converted = native_settings(data)
                        # Restore only fields still set to our fallback value; retain later edits.
                        settings.update(
                            {
                                key: value
                                for key, value in data.items()
                                if settings.get(key) == converted[key]
                            }
                        )
                        db.execute(
                            "UPDATE user_settings SET settings_json=? WHERE user_id=?",
                            (json.dumps(settings), row_id),
                        )
                else:
                    raise RuntimeError("Unrecognized held task type")
                db.execute(
                    "DELETE FROM edition_fallback_hold WHERE kind=? AND row_id=?", (table, row_id)
                )
        return counts[0], counts[1]


def prepare(config: Path, official: bool, *, keep_config_files: bool = False) -> None:
    # Parse every settings file before changing anything. Never follow symlinks.
    paths = [config / "settings.json", *sorted((config / "plugins").glob("*.json"))]
    updates: list[tuple[Path, dict[str, Any]]] = []
    for path in paths:
        if not path.exists():
            continue
        if path.is_symlink() or not path.resolve().is_relative_to(config.resolve()):
            raise RuntimeError("Unsafe settings path")
        data = json.loads(path.read_text())
        if not isinstance(data, dict):
            raise RuntimeError("Unsupported settings structure")
        updated = native_settings(data) if official and not keep_config_files else data
        if updated != data:
            updates.append((path, updated))
    if (config / "users.db").is_symlink():
        raise RuntimeError("Unsafe database path")
    requests, retries = edit_database(config / "users.db", official)
    for path, updated in updates:
        stat = path.stat()
        fd, name = tempfile.mkstemp(dir=path.parent, prefix=".fallback-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(updated, stream, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            Path(name).chmod(stat.st_mode)
            if hasattr(os, "chown"):
                os.chown(name, stat.st_uid, stat.st_gid)
            Path(name).replace(path)
        finally:
            Path(name).unlink(missing_ok=True)
    print(
        f"Compatibility preparation complete; edition requests held: {requests}; retries withheld: {retries}"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Offline compatibility preparation; stop Shelfmark first."
    )
    parser.add_argument("mode", choices=("official", "native"))
    parser.add_argument(
        "--config",
        required=True,
        type=Path,
        help="An offline configuration directory; stop its only application first",
    )
    parser.add_argument(
        "--keep-config-files",
        action="store_true",
        help="Deployment manager supplies stock provider settings through Compose",
    )
    args = parser.parse_args()
    try:
        prepare(args.config, args.mode == "official", keep_config_files=args.keep_config_files)
    except Exception:
        # Persisted data can contain credentials/private URLs: never echo exceptions.
        print(
            "Compatibility preparation refused; data retained. Review configuration/schema privately.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None

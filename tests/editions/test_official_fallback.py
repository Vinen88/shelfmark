"""Safety regressions for stock-provider conversion and held edition tasks."""

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from scripts.fallback_config import edit_database, native_settings, prepare


class FallbackConfigTests(unittest.TestCase):
    def test_only_custom_values_are_changed(self) -> None:
        data = {
            "METADATA_PROVIDER_AUDIOBOOK": "edition_metadata",
            "EDITION_METADATA_ENABLED": True,
            "METADATA_PROVIDER": "openlibrary",
            "client_token": "synthetic-secret",
            "new_setting": 42,
            "TEMPLATE_AUDIOBOOK_ORGANIZE": "{Author}/{Title}",
        }
        updated = native_settings(data)
        self.assertEqual(updated["METADATA_PROVIDER_AUDIOBOOK"], "openlibrary")
        self.assertFalse(updated["EDITION_METADATA_ENABLED"])
        for key in (
            "METADATA_PROVIDER",
            "client_token",
            "new_setting",
            "TEMPLATE_AUDIOBOOK_ORGANIZE",
        ):
            self.assertEqual(updated[key], data[key])

    def test_hold_and_restore_requests_retries_and_user_provider(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "users.db"
            with sqlite3.connect(path) as db:
                db.executescript("""
                    CREATE TABLE download_requests(id INTEGER PRIMARY KEY AUTOINCREMENT, book_data TEXT, release_data TEXT);
                    CREATE TABLE download_history(id INTEGER PRIMARY KEY, task_id TEXT, retry_payload TEXT);
                    CREATE TABLE user_settings(user_id INTEGER PRIMARY KEY, settings_json TEXT);
                """)
                edition = json.dumps(
                    {"provider": "edition_metadata", "edition_context": {"id": "part-2"}}
                )
                db.execute("INSERT INTO download_requests VALUES (1, ?, ?)", (edition, edition))
                db.execute("INSERT INTO download_requests VALUES (2, '{}', NULL)")
                db.execute("INSERT INTO download_history VALUES (1, 'edition', ?)", (edition,))
                db.execute("INSERT INTO download_history VALUES (2, 'ordinary', '{}')")
                db.execute(
                    "INSERT INTO user_settings VALUES (1, ?)",
                    (
                        json.dumps(
                            {"METADATA_PROVIDER_AUDIOBOOK": "edition_metadata", "theme": "dark"}
                        ),
                    ),
                )
            self.assertEqual(edit_database(path, True), (1, 1))
            edit_database(path, True)  # Idempotent; never overwrites preserved payload.
            with sqlite3.connect(path) as db:
                self.assertEqual(db.execute("SELECT id FROM download_requests").fetchall(), [(2,)])
                self.assertIsNone(
                    db.execute("SELECT retry_payload FROM download_history WHERE id=1").fetchone()[
                        0
                    ]
                )
                self.assertEqual(
                    json.loads(db.execute("SELECT settings_json FROM user_settings").fetchone()[0]),
                    {"METADATA_PROVIDER_AUDIOBOOK": "openlibrary", "theme": "dark"},
                )
            edit_database(path, False)
            with sqlite3.connect(path) as db:
                self.assertEqual(
                    json.loads(db.execute("SELECT settings_json FROM user_settings").fetchone()[0]),
                    {"METADATA_PROVIDER_AUDIOBOOK": "edition_metadata", "theme": "dark"},
                )
                self.assertEqual(
                    db.execute("SELECT id FROM download_requests ORDER BY id").fetchall(),
                    [(1,), (2,)],
                )
                self.assertEqual(
                    db.execute("SELECT retry_payload FROM download_history WHERE id=1").fetchone()[
                        0
                    ],
                    edition,
                )
                self.assertEqual(
                    db.execute("SELECT count(*) FROM edition_fallback_hold").fetchone()[0], 0
                )

    def test_malformed_settings_refuse_before_database_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory)
            (config / "settings.json").write_text("[")
            with self.assertRaises(ValueError):
                prepare(config, True)
            self.assertFalse((config / "users.db").exists())

    def test_newer_retry_is_never_overwritten_or_discarded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "users.db"
            with sqlite3.connect(path) as db:
                db.execute(
                    "CREATE TABLE download_history(id INTEGER PRIMARY KEY, task_id TEXT, retry_payload TEXT)"
                )
                db.execute(
                    "INSERT INTO download_history VALUES(1, 'part2', ?)",
                    ('{"edition_context":"frozen"}',),
                )
            edit_database(path, True)
            with sqlite3.connect(path) as db:
                db.execute("UPDATE download_history SET retry_payload='newer' WHERE id=1")
            with self.assertRaisesRegex(RuntimeError, "Held retry changed"):
                edit_database(path, False)
            with sqlite3.connect(path) as db:
                self.assertEqual(
                    db.execute("SELECT retry_payload FROM download_history").fetchone()[0], "newer"
                )
                self.assertEqual(
                    db.execute("SELECT count(*) FROM edition_fallback_hold").fetchone()[0], 1
                )

    def test_manager_keeps_global_settings_and_preserves_later_user_edits(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = {"METADATA_PROVIDER_AUDIOBOOK": "edition_metadata", "theme": "dark"}
            (root / "settings.json").write_text(json.dumps(original))
            with sqlite3.connect(root / "users.db") as db:
                db.execute(
                    "CREATE TABLE user_settings(user_id INTEGER PRIMARY KEY, settings_json TEXT)"
                )
                db.execute("INSERT INTO user_settings VALUES(1,?)", (json.dumps(original),))
            prepare(root, True, keep_config_files=True)
            self.assertEqual(json.loads((root / "settings.json").read_text()), original)
            with sqlite3.connect(root / "users.db") as db:
                db.execute(
                    "UPDATE user_settings SET settings_json=? WHERE user_id=1",
                    (json.dumps({"METADATA_PROVIDER_AUDIOBOOK": "hardcover", "theme": "new"}),),
                )
            prepare(root, False, keep_config_files=True)
            with sqlite3.connect(root / "users.db") as db:
                self.assertEqual(
                    json.loads(db.execute("SELECT settings_json FROM user_settings").fetchone()[0]),
                    {"METADATA_PROVIDER_AUDIOBOOK": "hardcover", "theme": "new"},
                )

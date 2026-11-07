"""Seed/check the real native schema for disposable official-image fallback tests."""

import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, "/workspace")
from scripts.fallback_config import prepare

root = Path("/config")
if sys.argv[1] == "seed":
    from shelfmark.core.user_db import UserDB

    db = UserDB(str(root / "users.db"))
    db.initialize()
    user = db.create_user("fixture-account", role="admin")
    book = {
        "provider": "edition_metadata",
        "provider_id": "synthetic-recording",
        "title": "Fixture",
    }
    request = db.create_request(
        user_id=user["id"],
        content_type="audiobook",
        request_level="book",
        policy_mode="request_book",
        book_data=book,
    )
    with sqlite3.connect(root / "users.db") as connection:
        connection.execute(
            "INSERT INTO download_history(task_id, source, title, final_status, retry_payload) VALUES(?,?,?,?,?)",
            (
                "fixture-retry",
                "prowlarr",
                "Fixture",
                "error",
                json.dumps({"output_args": {"edition_context": "frozen fixture"}}),
            ),
        )
        connection.execute(
            "INSERT INTO user_settings VALUES (?, ?)",
            (
                user["id"],
                json.dumps({"METADATA_PROVIDER_AUDIOBOOK": "edition_metadata", "theme": "dark"}),
            ),
        )
    (root / "plugins").mkdir(exist_ok=True)
    (root / "plugins/general.json").write_text(
        json.dumps({"METADATA_PROVIDER_AUDIOBOOK": "edition_metadata", "theme": "dark"})
    )
    prepare(root, True)
else:
    prepare(root, False)
    with sqlite3.connect(root / "users.db") as connection:
        assert connection.execute("SELECT count(*) FROM download_requests").fetchone()[0] == 1
        assert connection.execute(
            "SELECT retry_payload FROM download_history WHERE task_id='fixture-retry'"
        ).fetchone()[0]
        assert connection.execute("SELECT count(*) FROM edition_fallback_hold").fetchone()[0] == 0
    print("PASS held native requests and retries restored after official startup")

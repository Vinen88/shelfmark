"""Seed/check the real native schema for disposable official-image fallback tests."""

import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, "/workspace")
from scripts.fallback_config import prepare

root = Path("/config")
if sys.argv[1] == "seed":
    from mock_backend import MOCK_EDITIONS

    from shelfmark.core.edition_handoff import FrozenEditionContext
    from shelfmark.core.user_db import UserDB
    from shelfmark.metadata_providers.editions.identity import EditionIdentity
    from shelfmark.metadata_providers.editions.snapshot import EditionSnapshot

    db = UserDB(str(root / "users.db"))
    db.initialize()
    user = db.create_user("fixture-account", role="admin")
    snapshot = EditionSnapshot(
        EditionIdentity("edition_metadata", MOCK_EDITIONS[0].provider_id), MOCK_EDITIONS[0]
    )
    book = {
        "provider": "edition_metadata",
        "provider_id": snapshot.identity.provider_id,
        "title": "Fixture",
        "download_intent": "work",
        "edition_candidate_ids": [snapshot.identity.provider_id],
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
                json.dumps(
                    {
                        "output_args": {
                            "edition_context": FrozenEditionContext(
                                snapshot.to_json(),
                                "prowlarr",
                                "fixture-retry",
                                True,
                                (snapshot.to_json(),),
                            ).to_json()
                        }
                    }
                ),
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
        payload = connection.execute(
            "SELECT retry_payload FROM download_history WHERE task_id='fixture-retry'"
        ).fetchone()[0]
        from shelfmark.core.edition_handoff import FrozenEditionContext

        assert FrozenEditionContext.from_json(
            json.loads(payload)["output_args"]["edition_context"]
        ).work
        assert (
            json.loads(connection.execute("SELECT book_data FROM download_requests").fetchone()[0])[
                "download_intent"
            ]
            == "work"
        )
        assert connection.execute("SELECT count(*) FROM edition_fallback_hold").fetchone()[0] == 0
    print(
        "PASS work intention, frozen candidate snapshots and native requests/retries restored after official startup"
    )

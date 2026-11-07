"""Real native database fixtures for deployment recovery; fictional data only."""

import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, "/workspace")
sys.path.insert(0, "/workspace/tests/editions")

root = Path("/config")
if sys.argv[1] == "seed":
    from test_abs_metadata import snapshots

    from shelfmark.core.edition_handoff import FrozenEditionContext
    from shelfmark.core.models import DownloadTask
    from shelfmark.core.user_db import UserDB
    from shelfmark.download.orchestrator import serialize_task_for_retry

    db = UserDB(str(root / "users.db"))
    db.initialize()
    user = db.create_user(
        "deployment-fixture", role="admin", password_hash="synthetic-password-hash"
    )
    snapshot = snapshots()[0]
    db.create_request(
        user_id=user["id"],
        content_type="audiobook",
        request_level="book",
        policy_mode="request_book",
        book_data={
            "provider": "edition_metadata",
            "provider_id": snapshot.identity.provider_id,
            "title": snapshot.edition_title,
        },
    )
    task = DownloadTask(
        task_id="fixture-retry",
        source="prowlarr",
        title=snapshot.edition_title,
        content_type="audiobook",
        output_args={
            "edition_context": FrozenEditionContext(
                snapshot.to_json(), "prowlarr", "fixture-retry"
            ).to_json()
        },
    )
    with sqlite3.connect(root / "users.db") as connection:
        connection.execute(
            "INSERT INTO download_history(task_id, source, title, final_status, retry_payload) VALUES(?,?,?,?,?)",
            (
                task.task_id,
                task.source,
                task.title,
                "error",
                json.dumps(serialize_task_for_retry(task)),
            ),
        )
        connection.execute(
            "INSERT INTO user_settings VALUES(?,?)",
            (
                user["id"],
                json.dumps({"METADATA_PROVIDER_AUDIOBOOK": "edition_metadata", "theme": "dark"}),
            ),
        )
    with sqlite3.connect(root / "users.db") as connection:
        (root / "fixture-expected.json").write_text(
            json.dumps(
                {
                    "book": json.loads(
                        connection.execute("SELECT book_data FROM download_requests").fetchone()[0]
                    ),
                    "retry": json.loads(
                        connection.execute("SELECT retry_payload FROM download_history").fetchone()[
                            0
                        ]
                    ),
                }
            )
        )
    (root / "plugins").mkdir(exist_ok=True)
    (root / "plugins/general.json").write_text(
        json.dumps(
            {
                "METADATA_PROVIDER_AUDIOBOOK": "edition_metadata",
                "SEARCH_PAGE_TITLE": "Fixture original",
            }
        )
    )
elif sys.argv[1] == "edit":
    path = root / "plugins/general.json"
    settings = json.loads(path.read_text())
    settings["SEARCH_PAGE_TITLE"] = "Fixture edited during official recovery"
    path.write_text(json.dumps(settings))
    with sqlite3.connect(root / "users.db") as connection:
        value = json.loads(
            connection.execute("SELECT settings_json FROM user_settings").fetchone()[0]
        )
        value["theme"] = "light"
        connection.execute("UPDATE user_settings SET settings_json=?", (json.dumps(value),))
else:
    official = sys.argv[1] == "official"
    edited = len(sys.argv) > 2
    expected = json.loads((root / "fixture-expected.json").read_text())
    with sqlite3.connect(root / "users.db") as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM users WHERE username='deployment-fixture' AND role='admin' AND password_hash='synthetic-password-hash'"
            ).fetchone()[0]
            == 1
        )
        assert connection.execute("SELECT count(*) FROM download_requests").fetchone()[0] == (
            0 if official else 1
        )
        payload = connection.execute(
            "SELECT retry_payload FROM download_history WHERE task_id='fixture-retry'"
        ).fetchone()[0]
        if official:
            assert payload is None
            held = {
                row[0]: json.loads(row[1])
                for row in connection.execute(
                    "SELECT kind, payload FROM edition_fallback_hold WHERE kind IN ('download_requests','download_history')"
                )
            }
            assert json.loads(held["download_requests"]["book_data"]) == expected["book"]
            assert json.loads(held["download_history"]["retry_payload"]) == expected["retry"]
            assert (
                connection.execute(
                    "SELECT count(*) FROM edition_fallback_hold WHERE kind IN ('download_requests','download_history')"
                ).fetchone()[0]
                == 2
            )
        else:
            from shelfmark.core.edition_handoff import FrozenEditionContext

            context = FrozenEditionContext.from_json(
                json.loads(payload)["output_args"]["edition_context"]
            )
            assert context.snapshot.edition.part_number == 1
            assert json.loads(payload) == expected["retry"]
            assert (
                json.loads(
                    connection.execute("SELECT book_data FROM download_requests").fetchone()[0]
                )
                == expected["book"]
            )
            assert (
                connection.execute("SELECT count(*) FROM edition_fallback_hold").fetchone()[0] == 0
            )
        settings = json.loads(
            connection.execute("SELECT settings_json FROM user_settings").fetchone()[0]
        )
        assert settings["METADATA_PROVIDER_AUDIOBOOK"] == (
            "openlibrary" if official else "edition_metadata"
        )
        assert settings["theme"] == ("light" if edited else "dark")
    assert (
        json.loads((root / "plugins/general.json").read_text())["METADATA_PROVIDER_AUDIOBOOK"]
        == "edition_metadata"
    )
    assert json.loads((root / "plugins/general.json").read_text())["SEARCH_PAGE_TITLE"] == (
        "Fixture edited during official recovery" if edited else "Fixture original"
    )
    print(
        "PASS accounts, original global settings, exact edition requests/retries and",
        sys.argv[1],
        "provider state",
    )

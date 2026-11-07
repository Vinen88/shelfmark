"""Check a disposable image through its real entrypoint and authenticated routes."""

import hashlib
import re
import sqlite3
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

base = "http://127.0.0.1:8084"
for _attempt in range(200):
    try:
        with urlopen(base + "/api/health", timeout=2) as response:
            assert response.status == 200
        break
    except HTTPError, URLError, TimeoutError, ConnectionError:
        time.sleep(0.1)
else:
    raise AssertionError("Native application did not become healthy")
index = Path("/app/frontend-dist/index.html").read_text().encode()
with urlopen(base, timeout=5) as response:
    assert response.status == 200 and response.read() == index
asset = re.search(rb'src="\.?(/assets/[^\"]+\.js)"', index)
assert asset
with urlopen(base + asset[1].decode(), timeout=5) as response:
    assert (
        hashlib.sha256(response.read()).digest()
        == hashlib.sha256(
            (Path("/app/frontend-dist") / asset[1].decode().lstrip("/")).read_bytes()
        ).digest()
    )
try:
    urlopen(base + "/api/status", timeout=5)
except HTTPError as error:
    assert error.code in (401, 403)
else:
    raise AssertionError("Anonymous access to the native protected route was accepted")
from shelfmark.metadata_providers import is_provider_registered

assert is_provider_registered("edition_metadata") is (sys.argv[1] == "native")
with sqlite3.connect("file:/config/users.db?mode=ro", uri=True) as connection:
    if sys.argv[1] == "official":
        assert connection.execute("SELECT count(*) FROM download_requests").fetchone()[0] == 0
        assert (
            connection.execute(
                "SELECT retry_payload FROM download_history WHERE task_id='fixture-retry'"
            ).fetchone()[0]
            is None
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM edition_fallback_hold WHERE kind IN ('download_requests','download_history')"
            ).fetchone()[0]
            == 2
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM users WHERE username='fixture-account'"
            ).fetchone()[0]
            == 1
        )
print(
    "PASS",
    sys.argv[1],
    "normal entrypoint, health, authenticated routes, built frontend and native database",
)

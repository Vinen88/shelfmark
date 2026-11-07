"""Native edition regressions use fictional catalogs and no external connections."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))


@pytest.fixture(autouse=True)
def edition_network_isolation(monkeypatch):
    def refused(*args, **kwargs):
        raise AssertionError("External network is prohibited in edition tests")

    monkeypatch.setattr("socket.socket.connect", refused)
    monkeypatch.setattr("socket.create_connection", refused)

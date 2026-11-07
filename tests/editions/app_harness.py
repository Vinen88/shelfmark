"""Official auth/policy fixture, without executing main's DB/worker startup.

The login decorator's exact AST is loaded from the installed main.py. All other
security helpers are imported normally. No replacement authentication algorithm.
"""

from __future__ import annotations
import __future__

import ast
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import wraps
from pathlib import Path
from types import ModuleType
from typing import cast
from unittest.mock import MagicMock

from flask import Flask, Response, jsonify, request, session

from shelfmark.core import release_routes
from shelfmark.core.app_setup import register_security_headers
from shelfmark.core.auth_middleware import register_auth_middleware
from shelfmark.core.auth_modes import (
    is_settings_or_onboarding_path,
    requires_admin_for_settings_access,
)
from shelfmark.core.user_db import UserDB

View = Callable[..., Response | tuple[Response, int]]
LoginDecorator = Callable[[View], View]
from mock_backend import MOCK_EDITIONS

from shelfmark.metadata_providers.editions.identity import EditionIdentity
from shelfmark.metadata_providers.editions.snapshot import EditionSnapshot


def fixture_resolver(identity: EditionIdentity) -> EditionSnapshot | None:
    for edition in MOCK_EDITIONS:
        snapshot = EditionSnapshot(
            EditionIdentity("edition_metadata", edition.provider_id), edition
        )
        if snapshot.identity == identity:
            return snapshot
    return None


@dataclass
class AppHarness:
    mode: str = "builtin"
    app: Flask = field(default_factory=lambda: Flask("edition-test"))
    db: MagicMock = field(default_factory=lambda: MagicMock(spec=UserDB))
    main: ModuleType = field(default_factory=lambda: ModuleType("shelfmark.main"))
    settings: dict[str, object] = field(
        default_factory=lambda: {
            "REQUESTS_ENABLED": True,
            "REQUEST_POLICY_DEFAULT_AUDIOBOOK": "download",
        }
    )
    login: LoginDecorator = field(init=False)

    def __post_init__(self) -> None:
        self.app.config.update(
            TESTING=True,
            SECRET_KEY="isolated-test-secret",
            SESSION_COOKIE_SAMESITE="Lax",
            SESSION_COOKIE_HTTPONLY=True,
        )
        self.db.get_user_settings.return_value = {}
        self.db.get_first_admin.return_value = {"id": 1, "username": "fixture-admin"}
        self.db.get_user_cached.return_value = {"id": 2, "username": "fixture-user", "role": "user"}
        main_path = Path(release_routes.__file__).resolve().parents[1] / "main.py"
        tree = ast.parse(main_path.read_text())
        login_node = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "login_required"
        )
        namespace: dict[str, object] = {
            "wraps": wraps,
            "jsonify": jsonify,
            "request": request,
            "session": session,
            "get_auth_mode": lambda: self.mode,
            "CWA_DB_PATH": None,
            "logger": logging.getLogger("official-auth-fixture"),
            "is_settings_or_onboarding_path": is_settings_or_onboarding_path,
            "requires_admin_for_settings_access": requires_admin_for_settings_access,
        }
        exec(
            compile(
                ast.Module(body=[login_node], type_ignores=[]),
                str(main_path),
                "exec",
                flags=__future__.annotations.compiler_flag,
            ),
            namespace,
        )
        self.login = cast("LoginDecorator", namespace["login_required"])
        register_security_headers(self.app)
        register_auth_middleware(
            self.app, resolve_auth_mode=lambda: self.mode, get_user_db=lambda: self.db
        )

        def baseline() -> Response:
            return jsonify({"stock": True})

        self.app.add_url_rule("/api/health", "health", baseline)
        # Mark normal route-registration completion without installing any download action.
        for endpoint in ("api_metadata_search", "api_download_release", "api_inspect_release"):
            self.app.add_url_rule(f"/fixture/{endpoint}", endpoint, baseline)
        self.main.__dict__.update(
            {
                "app": self.app,
                "__file__": str(main_path),
                "login_required": self.login,
                "user_db": self.db,
                "_resolve_auth_mode_for_routes": lambda: self.mode,
                "_load_policy_settings_for_routes": lambda: self.settings,
            }
        )

"""Deploy native Shelfmark, with verified recovery to the pinned official image.

Exit codes: 0 = native deploy / healthy status; 2 = healthy official recovery;
1 = failure. Docker output and resolved credentials stay private.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import subprocess
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator

ROOT = Path(__file__).resolve().parents[1]
OFFICIAL = "ghcr.io/calibrain/shelfmark@sha256:0e5c9ff5f858a9695dc18bc355d8aa29dfee5c0d9c634919484c2bab9955ad5a"
PROBE = """import urllib.request
with urllib.request.urlopen('http://127.0.0.1:8084/api/health',timeout=2) as r:
 assert r.status == 200
from shelfmark.metadata_providers import is_provider_registered
assert is_provider_registered('edition_metadata') == NATIVE
"""
INSPECT = (
    '{"id":{{json .Id}},"image":{{json .Image}},'
    '"running":{{json .State.Running}},"status":{{json .State.Status}},'
    '"health":{{if .State.Health}}{{json .State.Health.Status}}{{else}}null{{end}},'
    '"project":{{json (index .Config.Labels "com.docker.compose.project")}},'
    '"service":{{json (index .Config.Labels "com.docker.compose.service")}},'
    '"mounts":{{json .Mounts}}}'
)


class DeploymentError(RuntimeError):
    """A safe operator-facing error, without Docker/credential-bearing output."""


@contextmanager
def deployment_lock() -> Iterator[None]:
    """One foreground deployment at a time; OS releases the lock after a crash."""
    path = ROOT / ".local" / "deploy.lock"
    path.parent.mkdir(exist_ok=True)
    with path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
        handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise DeploymentError("Another deployment is active in this checkout") from None
        yield


def official_config(config: dict[str, Any]) -> dict[str, Any]:
    """Preserve ports/mounts; stock startup and settings have no edition dependency."""
    result = copy.deepcopy(config)
    service = result["services"]["shelfmark"]
    for key in ("build", "entrypoint", "command", "healthcheck", "env_file"):
        service.pop(key, None)
    service["image"] = OFFICIAL
    env = service.setdefault("environment", {})
    for key in list(env):
        if key.startswith(("EDITION_", "SHELFMARK_EDITION_")) or key in {
            "PYTHONPATH",
            "GUNICORN_CMD_ARGS",
        }:
            del env[key]
        elif key.startswith("METADATA_PROVIDER") and env[key] == "edition_metadata":
            env[key] = "openlibrary"
    env.update(METADATA_PROVIDER_AUDIOBOOK="openlibrary", OPENLIBRARY_ENABLED="true")
    env.setdefault("METADATA_PROVIDER", "openlibrary")
    env.setdefault("METADATA_PROVIDER_COMBINED", "openlibrary")
    if env.get("TEMPLATE_AUDIOBOOK_ORGANIZE") == "{Author}/{Title}/{Title} - {PartNumber}":
        env["TEMPLATE_AUDIOBOOK_ORGANIZE"] = "{Author}/{Title}/{Title}"
    return result


def normalized_source(value: str) -> str:
    value = value.replace("\\", "/").rstrip("/")
    for prefix in ("/run/desktop/mnt/host/", "/host_mnt/"):
        if value.startswith(prefix):
            value = value[len(prefix) :]
            value = value[0] + ":" + value[1:]
    return value.casefold() if len(value) > 1 and value[1] == ":" else value


class Deployment:
    def __init__(
        self,
        compose: Path,
        project: str | None = None,
        timeout: int = 90,
        docker_config: str | None = None,
    ) -> None:
        self.docker = ["docker", *(["--config", docker_config] if docker_config else [])]
        self.timeout = timeout
        self.directory = str(compose.resolve().parent)
        command = ["compose", "--project-directory", self.directory, "-f", str(compose.resolve())]
        if project:
            command.extend(["-p", project])
        self.config = json.loads(self.run(*command, "config", "--format", "json"))
        self.project = self.config["name"]
        service = self.config["services"]["shelfmark"]
        self.name = service.get("container_name", self.project + "-shelfmark-1")
        self.image = service["image"]
        mounts = [m for m in service.get("volumes", []) if m["target"] == "/config"]
        if (
            len(mounts) != 1
            or mounts[0].get("read_only")
            or service.get("environment", {}).get("CONFIG_DIR", "/config") != "/config"
        ):
            raise DeploymentError(
                "Exactly one writable /config mount and CONFIG_DIR=/config are required"
            )
        if any(
            m["target"] == "/app" or m["target"].startswith(("/app/", "/opt/shelfmark"))
            for m in service.get("volumes", [])
        ):
            raise DeploymentError("Application code mounts are unsupported")
        self.mount = mounts[0]
        self.source = self.mount["source"]
        if self.mount["type"] == "volume":
            self.source = (
                self.config.get("volumes", {}).get(self.source, {}).get("name", self.source)
            )
        elif self.mount["type"] != "bind":
            raise DeploymentError("Use a bind or named-volume configuration mount")

    def run(
        self, *args: str, input_text: str | None = None, timeout: int = 120, check: bool = True
    ) -> str:
        try:
            result = subprocess.run(
                [*self.docker, *args],
                cwd=self.directory,
                input=input_text,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise DeploymentError(
                "Docker operation unavailable or timed out; inspect logs privately"
            ) from None
        if check and result.returncode:
            raise DeploymentError(
                f"Docker operation failed (exit {result.returncode}); output withheld"
            )
        return result.stdout if result.returncode == 0 else ""

    def compose(self, config: dict[str, Any], *args: str, timeout: int = 120) -> str:
        return self.run(
            "compose",
            "--project-directory",
            self.directory,
            "-p",
            self.project,
            "-f",
            "-",
            *args,
            input_text=json.dumps(config),
            timeout=timeout,
        )

    def inspect(self, container: str) -> dict[str, Any] | None:
        raw = self.run("inspect", "--format", INSPECT, container, timeout=10, check=False)
        return json.loads(raw) if raw else None

    def check_exclusive(self, *, stopped: bool = False) -> None:
        target = self.inspect(self.name)
        if target and (target["project"], target["service"]) != (self.project, "shelfmark"):
            raise DeploymentError("Target container belongs to another Compose project/service")
        for identity in self.run("ps", "-q").split():
            data = self.inspect(identity)
            if not data:
                continue
            sharing = any(
                normalized_source(m.get("Name") or m["Source"]) == normalized_source(self.source)
                for m in data["mounts"]
            )
            if sharing and (stopped or not target or data["id"] != target["id"]):
                raise DeploymentError(
                    "Another running container uses this configuration; refusing concurrent access"
                )

    def ensure_official(self) -> str:
        if not self.run("image", "inspect", "--format", "{{.Id}}", OFFICIAL, check=False):
            self.run("pull", OFFICIAL, timeout=600)
        return self.run("image", "inspect", "--format", "{{.Id}}", OFFICIAL).strip()

    def ready(self, container: str, *, official: bool = False) -> None:
        expected = self.run(
            "image", "inspect", "--format", "{{.Id}}", OFFICIAL if official else self.image
        ).strip()
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            state = self.inspect(container)
            if not state or not state["running"] or state["health"] == "unhealthy":
                break
            if state["image"] != expected:
                raise DeploymentError("Running image differs from the verified target")
            if state["health"] in (None, "healthy"):
                try:
                    self.run(
                        "exec",
                        container,
                        "python",
                        "-B",
                        "-c",
                        PROBE.replace("NATIVE", str(not official)),
                        timeout=5,
                    )
                    return
                except DeploymentError:
                    pass
            time.sleep(1)
        raise DeploymentError("Bounded local application/provider readiness failed")

    def verify_candidate(self) -> None:
        name = self.project + "-candidate-" + uuid.uuid4().hex[:8]
        try:
            self.run(
                "run",
                "-d",
                "--name",
                name,
                "--network",
                "none",
                "--env",
                "ENABLE_LOGGING=false",
                "--env",
                "SEARCH_WARMUP_ENABLED=false",
                "--env",
                "USING_EXTERNAL_BYPASSER=true",
                self.image,
            )
            self.ready(name)
        finally:
            self.run("rm", "-f", name, check=False)

    def compatibility(self, official: bool) -> None:
        if self.mount["type"] == "bind":
            Path(self.source).mkdir(parents=True, exist_ok=True)
        else:
            self.run("volume", "create", self.source)
        self.run(
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--tmpfs",
            "/tmp",
            "--mount",
            f"type={self.mount['type']},source={self.source},target=/config",
            "--mount",
            f"type=bind,source={ROOT / 'scripts/fallback_config.py'},target=/compat.py,readonly",
            "--entrypoint",
            "python",
            OFFICIAL,
            "-I",
            "/compat.py",
            "official" if official else "native",
            "--config",
            "/config",
            "--keep-config-files",
        )

    def activate(self, *, official: bool = False) -> None:
        self.check_exclusive()
        target = official_config(self.config) if official else self.config
        self.compose(target, "stop", "--timeout", "20", "shelfmark")
        self.check_exclusive(stopped=True)
        self.compatibility(official)
        self.compose(
            target,
            "up",
            "-d",
            "--no-deps",
            "--no-build",
            "--pull",
            "never",
            "--force-recreate",
            "shelfmark",
        )
        self.ready(self.name, official=official)

    def deploy(self) -> int:
        self.check_exclusive()
        self.ensure_official()
        try:
            print("Building and verifying candidate against scratch configuration", flush=True)
            self.compose(self.config, "build", "shelfmark", timeout=1800)
            self.verify_candidate()
            self.activate()
        except DeploymentError:
            print("Custom deployment failed; restoring pinned official Shelfmark", flush=True)
            self.activate(official=True)
            print("Official recovery verified healthy", flush=True)
            return 2
        print("Native fork deployment verified healthy", flush=True)
        return 0

    def status(self) -> int:
        state = self.inspect(self.name)
        if not state:
            print("Shelfmark container absent")
            return 1
        official = (
            state["image"]
            == self.run("image", "inspect", "--format", "{{.Id}}", OFFICIAL, check=False).strip()
        )
        print(
            f"Shelfmark: {'official' if official else 'custom'}; {state['status']}; health={state['health'] or 'none'}"
        )
        self.ready(self.name, official=official)
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("deploy", "fallback", "status"))
    parser.add_argument("--compose", type=Path, default=ROOT / "compose.yaml")
    parser.add_argument("--project")
    parser.add_argument("--timeout", type=int, choices=range(5, 601), default=90, metavar="5..600")
    parser.add_argument("--docker-config")
    args = parser.parse_args()
    try:
        deployment = Deployment(args.compose, args.project, args.timeout, args.docker_config)
        if args.command == "status":
            return deployment.status()
        with deployment_lock():
            if args.command == "deploy":
                return deployment.deploy()
            deployment.check_exclusive()
            deployment.ensure_official()
            deployment.activate(official=True)
            print("Official fallback verified healthy", flush=True)
            return 2
    except (DeploymentError, ValueError, KeyError, OSError):
        print(
            "Deployment/recovery failed; data retained. Inspect service logs privately.", flush=True
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

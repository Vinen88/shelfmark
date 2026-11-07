"""Real Docker fault matrix; never uses the private deployment or media.

Build the native image first; run with Python on Windows or python3 on Linux.
Every application container uses network=none and fresh named volumes.
"""

import argparse
import json
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="shelfmark-fork-validation:local")
    parser.add_argument("--docker-config")
    args = parser.parse_args()
    docker = ["docker", *(["--config", args.docker_config] if args.docker_config else [])]
    prefix = "shelfmark-recovery-test-" + uuid.uuid4().hex[:10]
    volumes = [prefix + suffix for suffix in ("-config", "-books", "-media", "-downloads")]
    image = prefix + ":candidate"
    results = []
    edited = False

    def run(*command):
        return subprocess.run(
            [*docker, *command], capture_output=True, text=True, check=True
        ).stdout

    def fixture(mode):
        run(
            "run",
            "--rm",
            "--network",
            "none",
            "--env",
            "CONFIG_DIR=/config",
            "--env",
            "LOG_ROOT=/tmp/logs",
            "--mount",
            f"type=volume,source={volumes[0]},target=/config",
            "--mount",
            f"type=bind,source={ROOT},target=/workspace,readonly",
            "--entrypoint",
            "python",
            args.image,
            "/workspace/tests/editions/deployment_fixture.py",
            mode,
            *(["edited"] if edited else []),
        )

    ROOT.joinpath(".local").mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="recovery-", dir=ROOT / ".local") as directory:
        folder = Path(directory)
        compose = folder / "compose.json"
        config = {
            "name": prefix,
            "services": {
                "shelfmark": {
                    "container_name": prefix,
                    "image": image,
                    "build": {"context": str(folder)},
                    "network_mode": "none",
                    "restart": "no",
                    "environment": {
                        "ENABLE_LOGGING": "false",
                        "SEARCH_WARMUP_ENABLED": "false",
                        "AUTH_METHOD": "builtin",
                        "USING_EXTERNAL_BYPASSER": "true",
                        "PROWLARR_ENABLED": "false",
                        "DIRECT_DOWNLOAD_ENABLED": "false",
                        "EDITION_LIBEX_ENABLED": "false",
                        "METADATA_PROVIDER_AUDIOBOOK": "edition_metadata",
                    },
                    "volumes": [
                        {"type": "volume", "source": name, "target": target}
                        for name, target in zip(
                            volumes, ("/config", "/books", "/media", "/downloads"), strict=True
                        )
                    ],
                }
            },
            "volumes": {name: {"name": name, "external": True} for name in volumes},
        }
        base = f"FROM {args.image}\n"

        def command(name, expected):
            cli = [
                sys.executable,
                str(ROOT / "scripts/manage.py"),
                name,
                "--compose",
                str(compose),
                "--timeout",
                "20",
            ]
            if args.docker_config:
                cli.extend(["--docker-config", str(Path(args.docker_config).resolve())])
            result = subprocess.run(cli, text=True, capture_output=True, timeout=150)
            assert result.returncode == expected, (
                name,
                result.returncode,
                result.stdout,
                result.stderr,
            )
            results.append(
                {"operation": name, "exit": result.returncode, "output": result.stdout.strip()}
            )
            print(
                f"PASS {len(results)}: {name} exit={result.returncode}; {result.stdout.strip()}",
                flush=True,
            )

        try:
            for name in volumes:
                run("volume", "create", name)
            fixture("seed")
            cases = [
                ("successful fork", "", {}, 0),
                ("failed build", "RUN exit 17\n", {}, 2),
                ("invalid image entrypoint", 'ENTRYPOINT ["/missing-entrypoint"]\n', {}, 2),
                ("container startup failure", "", {"command": ["/bin/sh", "-c", "exit 23"]}, 2),
                (
                    "unhealthy container",
                    "",
                    {
                        "healthcheck": {
                            "test": ["CMD", "false"],
                            "interval": "1s",
                            "timeout": "1s",
                            "retries": 1,
                            "start_period": "0s",
                        }
                    },
                    2,
                ),
                (
                    "provider registration failure",
                    "RUN sed -i '/from shelfmark.metadata_providers.editions import provider /d' /app/shelfmark/metadata_providers/__init__.py\n",
                    {},
                    2,
                ),
                ("retry fork after recovery", "", {}, 0),
            ]
            for label, instructions, overrides, expected in cases:
                service = config["services"]["shelfmark"]
                service.pop("command", None)
                service.pop("healthcheck", None)
                service.update(overrides)
                compose.write_text(json.dumps(config), encoding="utf-8")
                (folder / "Dockerfile").write_text(
                    base + instructions, encoding="utf-8", newline="\n"
                )
                print("CASE", label, flush=True)
                command("deploy", expected)
                fixture("official" if expected == 2 else "native")
                command("status", 0)
                mounts = json.loads(run("inspect", "--format", "{{json .Mounts}}", prefix))
                assert {m["Name"] for m in mounts} == set(volumes)
                # At every completed transition, exactly one container uses the DB.
                matching = []
                for identity in run("ps", "-q").split():
                    mounted = json.loads(run("inspect", "--format", "{{json .Mounts}}", identity))
                    if any(m.get("Name") == volumes[0] for m in mounted):
                        matching.append(identity)
                assert len(matching) == 1
                if label == "failed build":
                    fixture("edit")
                    edited = True
            command("fallback", 2)
            fixture("official")
            command("deploy", 0)
            fixture("native")
            print(
                "PASS real Docker recovery matrix; accounts/settings/tasks preserved; no production mounts or network",
                flush=True,
            )
        finally:
            subprocess.run([*docker, "rm", "-f", prefix], capture_output=True, check=False)
            subprocess.run([*docker, "volume", "rm", *volumes], capture_output=True, check=False)
            subprocess.run([*docker, "image", "rm", image], capture_output=True, check=False)
            ROOT.joinpath(".local/recovery-results.json").write_text(
                json.dumps(results, indent=2), encoding="utf-8"
            )


if __name__ == "__main__":
    main()

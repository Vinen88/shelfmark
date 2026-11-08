"""Run real ABS and the native fork using disposable, externally isolated storage.

Usage: python tests/editions/run_e2e.py [--image shelfmark-fork-validation:local]
Build the native image first. Nothing in this runner reads production configuration.
"""

import argparse
import subprocess
import uuid
from pathlib import Path

ABS_IMAGE = "ghcr.io/advplyr/audiobookshelf@sha256:b897c33f05972fb1bf484f7487bc878fa91ec8691335c1db6e2929c3a7658efa"
OFFICIAL_IMAGE = "ghcr.io/calibrain/shelfmark@sha256:0e5c9ff5f858a9695dc18bc355d8aa29dfee5c0d9c634919484c2bab9955ad5a"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="shelfmark-fork-validation:local")
    parser.add_argument("--docker-config")
    args = parser.parse_args()
    docker = ["docker"]
    if args.docker_config:
        docker.extend(["--config", args.docker_config])
    prefix = "shelfmark-fork-e2e-" + uuid.uuid4().hex[:10]
    root = Path(__file__).resolve().parents[2]

    def run(*command, capture=False):
        return subprocess.run(
            [*docker, *command],
            check=True,
            text=True,
            stdout=subprocess.PIPE if capture else None,
        )

    try:
        for label, image in (("native", args.image), ("official", OFFICIAL_IMAGE)):
            name = prefix + "-" + label
            run("volume", "create", name)
            if label == "official":
                run(
                    "run",
                    "--rm",
                    "--network",
                    "none",
                    "--mount",
                    f"type=volume,source={name},target=/config",
                    "--mount",
                    f"type=bind,source={root},target=/workspace,readonly",
                    "--env",
                    "CONFIG_DIR=/config",
                    "--env",
                    "LOG_ROOT=/tmp/logs",
                    "--env",
                    "PYTHONPATH=/workspace",
                    "--entrypoint",
                    "python",
                    args.image,
                    "/workspace/tests/editions/fallback_fixture.py",
                    "seed",
                )
            run(
                "run",
                "-d",
                "--name",
                name,
                "--network",
                "none",
                "--mount",
                f"type=volume,source={name},target=/config",
                "--mount",
                f"type=bind,source={root / 'tests/editions/startup_probe.py'},target=/tmp/startup_probe.py,readonly",
                "--env",
                "ENABLE_LOGGING=false",
                "--env",
                "SEARCH_WARMUP_ENABLED=false",
                "--env",
                "AUTH_METHOD=builtin",
                "--env",
                "USING_EXTERNAL_BYPASSER=true",
                image,
            )
            run("exec", name, "python", "/tmp/startup_probe.py", label)
            run("stop", "--timeout", "10", name)
            if label == "official":
                run(
                    "run",
                    "--rm",
                    "--network",
                    "none",
                    "--mount",
                    f"type=volume,source={name},target=/config",
                    "--mount",
                    f"type=bind,source={root},target=/workspace,readonly",
                    "--env",
                    "CONFIG_DIR=/config",
                    "--env",
                    "LOG_ROOT=/tmp/logs",
                    "--env",
                    "PYTHONPATH=/workspace",
                    "--entrypoint",
                    "python",
                    args.image,
                    "/workspace/tests/editions/fallback_fixture.py",
                    "restore",
                )
        run("network", "create", "--internal", prefix)
        run("volume", "create", prefix)
        run(
            "run",
            "-d",
            "--name",
            prefix,
            "--network",
            prefix,
            "--tmpfs",
            "/config",
            "--tmpfs",
            "/metadata",
            "--mount",
            f"type=volume,source={prefix},target=/media",
            "--env",
            "PORT=18087",
            "--env",
            "LOG_LEVEL=error",
            ABS_IMAGE,
        )
        # Generate real, tiny audio streams in scratch storage. No download is used.
        run("exec", prefix, "mkdir", "-p", "/media/fixtures")
        for extension, encoding in (
            ("mp3", ["-c:a", "libmp3lame"]),
            ("m4b", ["-c:a", "aac", "-f", "mp4"]),
        ):
            run(
                "exec",
                prefix,
                "ffmpeg",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "anullsrc=r=8000:cl=mono",
                "-t",
                "60",
                *encoding,
                f"/media/fixtures/track.{extension}",
            )
        run(
            "run",
            "--rm",
            "--name",
            prefix + "-runner",
            "--network",
            "container:" + prefix,
            "--mount",
            f"type=volume,source={prefix},target=/media",
            "--mount",
            f"type=bind,source={root},target=/workspace,readonly",
            "--workdir",
            "/tmp",
            "--env",
            "PYTHONPATH=/workspace:/workspace/tests/editions",
            "--entrypoint",
            "python",
            args.image,
            "/workspace/tests/editions/e2e.py",
        )
    finally:
        # These UUID-scoped names are created above; never enumerate/delete user resources.
        subprocess.run(
            [
                *docker,
                "rm",
                "-f",
                prefix + "-runner",
                prefix + "-native",
                prefix + "-official",
                prefix,
            ],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        subprocess.run(
            [*docker, "volume", "rm", prefix, prefix + "-native", prefix + "-official"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        subprocess.run(
            [*docker, "network", "rm", prefix],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


if __name__ == "__main__":
    main()

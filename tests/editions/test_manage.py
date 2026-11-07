"""Deployment behavior contracts; Docker is mocked here, fault tests use scratch Docker."""

import ast
from unittest.mock import Mock

import pytest

from scripts import manage


def test_official_config_preserves_mounts_and_masks_only_runtime_settings():
    original = {
        "services": {
            "shelfmark": {
                "image": "native",
                "build": {"context": "."},
                "command": ["bad"],
                "entrypoint": ["bad"],
                "healthcheck": {"test": ["CMD", "false"]},
                "ports": ["8084:8084"],
                "volumes": [{"source": "library", "target": "/media"}],
                "environment": {
                    "EDITION_LIBEX_ENABLED": "true",
                    "PYTHONPATH": "legacy",
                    "METADATA_PROVIDER_AUDIOBOOK": "edition_metadata",
                    "PROWLARR_API_KEY": "synthetic-only",
                },
            }
        }
    }
    result = manage.official_config(original)["services"]["shelfmark"]
    assert result["image"] == manage.OFFICIAL
    assert result["volumes"] == original["services"]["shelfmark"]["volumes"]
    assert result["ports"] == original["services"]["shelfmark"]["ports"]
    assert result["environment"]["PROWLARR_API_KEY"] == "synthetic-only"
    assert result["environment"]["METADATA_PROVIDER_AUDIOBOOK"] == "openlibrary"
    assert not {"build", "entrypoint", "command", "healthcheck"} & result.keys()
    assert "EDITION_LIBEX_ENABLED" not in result["environment"]
    assert original["services"]["shelfmark"]["image"] == "native"


def test_build_failure_recovers_without_attempting_candidate_activation():
    deployment = object.__new__(manage.Deployment)
    deployment.config = {}
    deployment.check_exclusive = Mock()
    deployment.ensure_official = Mock()
    deployment.compose = Mock(side_effect=manage.DeploymentError("synthetic build failure"))
    deployment.verify_candidate = Mock()
    deployment.activate = Mock()
    assert deployment.deploy() == 2
    deployment.verify_candidate.assert_not_called()
    deployment.activate.assert_called_once_with(official=True)


def test_total_failure_does_not_claim_official_recovery(monkeypatch, tmp_path, capsys):
    deployment = object.__new__(manage.Deployment)
    deployment.config = {}
    deployment.check_exclusive = Mock()
    deployment.ensure_official = Mock()
    deployment.compose = Mock(side_effect=manage.DeploymentError("build"))
    deployment.activate = Mock(side_effect=manage.DeploymentError("recovery"))
    with pytest.raises(manage.DeploymentError):
        deployment.deploy()
    monkeypatch.setattr(manage, "Deployment", lambda *args, **kwargs: deployment)
    monkeypatch.setattr(manage, "ROOT", tmp_path)
    monkeypatch.setattr("sys.argv", ["manage.py", "deploy"])
    assert manage.main() == 1
    assert "verified healthy" not in capsys.readouterr().out


def test_stop_failure_cannot_start_another_database_user():
    deployment = object.__new__(manage.Deployment)
    deployment.config = {}
    deployment.check_exclusive = Mock()
    deployment.compose = Mock(side_effect=manage.DeploymentError("cannot stop"))
    deployment.compatibility = Mock()
    deployment.ready = Mock()
    with pytest.raises(manage.DeploymentError):
        deployment.activate()
    deployment.compatibility.assert_not_called()
    deployment.ready.assert_not_called()


def test_subprocess_failure_does_not_expose_private_output(monkeypatch):
    deployment = object.__new__(manage.Deployment)
    deployment.directory = "."
    deployment.docker = ["docker"]
    monkeypatch.setattr(
        manage.subprocess,
        "run",
        lambda *args, **kwargs: Mock(
            returncode=1, stdout="synthetic-private-key", stderr="synthetic-private-key"
        ),
    )
    with pytest.raises(manage.DeploymentError) as error:
        deployment.run("compose", "config")
    assert "synthetic-private-key" not in str(error.value)


def test_shared_configuration_refuses_other_running_container():
    deployment = object.__new__(manage.Deployment)
    deployment.name, deployment.project, deployment.source = "target", "project", "config-volume"
    deployment.inspect = Mock(
        side_effect=[
            None,
            {
                "id": "other",
                "mounts": [
                    {
                        "Name": "config-volume",
                        "Source": "/var/lib/docker/volumes/config-volume/_data",
                    }
                ],
            },
        ]
    )
    deployment.run = Mock(return_value="other")
    with pytest.raises(manage.DeploymentError, match="configuration"):
        deployment.check_exclusive()


def test_desktop_bind_paths_compare_to_the_same_config():
    assert manage.normalized_source("N:\\docker_images\\config") == manage.normalized_source(
        "/run/desktop/mnt/host/n/docker_images/config"
    )


def test_status_absent_is_read_only(capsys):
    deployment = object.__new__(manage.Deployment)
    deployment.name = "fixture"
    deployment.inspect = Mock(return_value=None)
    assert deployment.status() == 1
    assert "absent" in capsys.readouterr().out


def test_host_cli_retains_python_310_syntax():
    for script in ("manage.py", "fallback_config.py"):
        ast.parse((manage.ROOT / "scripts" / script).read_text(), feature_version=(3, 10))


def test_deployment_lock_refuses_a_second_operator(tmp_path, monkeypatch):
    monkeypatch.setattr(manage, "ROOT", tmp_path)
    with manage.deployment_lock():
        with pytest.raises(manage.DeploymentError, match="Another deployment"):
            with manage.deployment_lock():
                pytest.fail("Concurrent deployment lock was accepted")
    with manage.deployment_lock():
        assert (tmp_path / ".local/deploy.lock").stat().st_size == 1

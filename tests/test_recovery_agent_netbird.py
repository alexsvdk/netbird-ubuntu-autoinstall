#!/usr/bin/env python3
"""Tests for the real recovery-agent NetBird command flow."""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "samovar_recovery_agent", ROOT / "recovery" / "samovar-recovery-agent.py"
)
assert SPEC and SPEC.loader
agent = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(agent)


def test_netbird_run_supplies_home_for_systemd_services() -> None:
    result = subprocess.CompletedProcess(
        ["netbird", "profile", "list"], 0, b"", b""
    )
    with (
        patch.dict("os.environ", {}, clear=True),
        patch("subprocess.run", return_value=result) as mock_run,
    ):
        agent._run(["netbird", "profile", "list"])

    assert mock_run.call_args.kwargs["env"]["HOME"] == "/root"


def test_retry_reuses_deterministic_netbird_profile_name() -> None:
    commands: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        commands.append(cmd)
        if cmd == ["netbird", "profile", "list"]:
            return subprocess.CompletedProcess(cmd, 0, b"* old-profile\n", b"")
        if cmd[:2] == ["netbird", "status"]:
            return subprocess.CompletedProcess(
                cmd, 0, b'{"management_url":"https://api.netbird.io:443"}', b""
            )
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    config = {
        "management_url": "https://api.netbird.io:443",
        "setup_key": "test-setup-key",
    }
    with (
        patch.object(agent, "_run", side_effect=fake_run),
        patch.object(agent, "_netbird_wait_connected", return_value=True),
        patch.object(agent, "_write_setup_key_file", return_value=Path("/run/test-key")),
        patch.object(agent, "_secure_delete"),
    ):
        agent.apply_netbird(config, 2026091901)
        agent.apply_netbird(config, 2026091901)

    added = [
        cmd[3]
        for cmd in commands
        if cmd[:3] == ["netbird", "profile", "add"]
    ]
    assert added == ["samovar-gen2026091901", "samovar-gen2026091901"]


def test_rollback_does_not_start_interactive_sso() -> None:
    commands: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        commands.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    with patch.object(agent, "_run", side_effect=fake_run):
        agent._netbird_rollback("old-profile")

    assert commands == [["netbird", "profile", "select", "old-profile"]]


def test_failed_profile_is_removed_after_rollback() -> None:
    commands: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        commands.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    with patch.object(agent, "_run", side_effect=fake_run):
        agent._netbird_rollback("old-profile", "failed-profile")

    assert commands == [
        ["netbird", "profile", "select", "old-profile"],
        ["netbird", "profile", "remove", "failed-profile"],
    ]


def test_current_profile_returns_active_profile_id_not_table_header() -> None:
    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        if cmd == ["netbird", "status"]:
            return subprocess.CompletedProcess(cmd, 0, b"", b"")
        assert cmd == ["netbird", "profile", "list", "--show-id"]
        output = "ID        NAME       ACTIVE\na1b2c3d4  old-name   ✓\ndefault    default\n".encode()
        return subprocess.CompletedProcess(cmd, 0, output, b"")

    with patch.object(agent, "_run", side_effect=fake_run):
        assert agent._netbird_current_profile() == "a1b2c3d4"


def test_current_profile_prefers_daemon_status() -> None:
    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        assert cmd == ["netbird", "status"]
        return subprocess.CompletedProcess(cmd, 0, b"Profile: samovar-gen2026091901\n", b"")

    with patch.object(agent, "_run", side_effect=fake_run):
        assert agent._netbird_current_profile() == "samovar-gen2026091901"

def test_mihomo_validation_uses_the_compose_image() -> None:
    result = subprocess.CompletedProcess(["docker", "compose"], 0, b"", b"")
    with patch.object(agent, "_run", return_value=result) as mock_run:
        agent._validate_mihomo_config_file(Path("/etc/mihomo/.samovar-test.yaml"))

    command = mock_run.call_args.args[0]
    assert command[:8] == [
        "docker",
        "compose",
        "--env-file",
        "/etc/mihomo/compose.env",
        "-f",
        "/etc/mihomo/compose.yml",
        "run",
        "--rm",
    ]
    assert command[-4:] == [
        "mihomo",
        "-t",
        "-f",
        "/root/.config/mihomo/.samovar-test.yaml",
    ]

def test_failed_mihomo_does_not_rotate_netbird_profile() -> None:
    config = {
        "generation": 2026091901,
        "target": "samovar",
        "mihomo": {"enabled": True},
        "netbird": {"management_url": "https://api.netbird.io:443", "setup_key": "secret"},
    }
    with (
        patch.object(agent, "apply_mihomo", side_effect=agent.RecoveryError("broken")),
        patch.object(agent, "apply_netbird") as apply_netbird,
    ):
        try:
            agent.apply_config(config, b"{}", source_label="test")
        except agent.RecoveryError:
            pass
        else:
            raise AssertionError("apply_config should fail")

    apply_netbird.assert_not_called()


def test_mihomo_runtime_config_is_bridge_only() -> None:
    source = {
        "allow-lan": False,
        "bind-address": "127.0.0.1",
        "external-controller": "127.0.0.1:9090",
        "tun": {"enable": True, "auto-route": True},
    }

    runtime = agent._prepare_mihomo_runtime_config(source)

    assert source["allow-lan"] is False
    assert source["tun"]["enable"] is True
    assert runtime["allow-lan"] is True
    assert runtime["bind-address"] == "0.0.0.0"
    assert runtime["external-controller"] == "0.0.0.0:9090"
    assert runtime["tun"]["enable"] is False

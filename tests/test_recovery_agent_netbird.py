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


def test_retry_uses_unique_netbird_profile_names() -> None:
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
    assert len(added) == 2
    assert added[0] != added[1]
    assert all(name.startswith("samovar-gen2026091901-") for name in added)


def test_rollback_does_not_start_interactive_sso() -> None:
    commands: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        commands.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    with patch.object(agent, "_run", side_effect=fake_run):
        agent._netbird_rollback("old-profile")

    assert commands == [["netbird", "profile", "select", "old-profile"]]


def test_current_profile_returns_active_profile_id_not_table_header() -> None:
    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        assert cmd == ["netbird", "profile", "list", "--show-id"]
        output = "ID        NAME       ACTIVE\na1b2c3d4  old-name   ✓\ndefault    default\n".encode()
        return subprocess.CompletedProcess(cmd, 0, output, b"")

    with patch.object(agent, "_run", side_effect=fake_run):
        assert agent._netbird_current_profile() == "a1b2c3d4"

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

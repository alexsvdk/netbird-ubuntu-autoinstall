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

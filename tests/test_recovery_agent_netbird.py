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
    profiles: list[dict[str, object]] = [{"id": "old-id", "name": "old-profile", "active": True}]

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        commands.append(cmd)
        if cmd == ["netbird", "profile", "list", "--json"]:
            import json
            return subprocess.CompletedProcess(cmd, 0, json.dumps(profiles).encode(), b"")
        if cmd[:3] == ["netbird", "profile", "add"]:
            profiles.append({"id": "gen-id-1", "name": cmd[3], "active": False})
            return subprocess.CompletedProcess(cmd, 0, f"Profile added: gen-id-1  {cmd[3]}\n".encode(), b"")
        if cmd == ["netbird", "status", "--json"]:
            return subprocess.CompletedProcess(
                cmd, 0, b'{"management":{"connected":true,"url":"https://api.netbird.io:443"}}', b""
            )
        if cmd[:3] == ["netbird", "status", "--check"]:
            return subprocess.CompletedProcess(cmd, 0, b"", b"")
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    config = {
        "management_url": "https://api.netbird.io:443",
        "setup_key": "test-setup-key",
    }
    with (
        patch.object(agent, "_run", side_effect=fake_run),
        patch.object(agent, "_write_setup_key_file", return_value=Path("/run/test-key")),
        patch.object(agent, "_secure_delete"),
    ):
        agent.apply_netbird(config, 2026091901)
        # Second call (retry) should reuse the existing profile and not call profile add again
        agent.apply_netbird(config, 2026091901)

    added = [
        cmd[3]
        for cmd in commands
        if cmd[:3] == ["netbird", "profile", "add"]
    ]
    assert added == ["samovar-gen2026091901"]
    selects = [
        cmd[3]
        for cmd in commands
        if cmd[:3] == ["netbird", "profile", "select"]
    ]
    assert selects == ["gen-id-1", "gen-id-1"]


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
        if cmd == ["netbird", "status"]:
            return subprocess.CompletedProcess(cmd, 0, b"Profile: samovar-gen2026091901\n", b"")
        if cmd == ["netbird", "profile", "list", "--show-id"]:
            output = "ID        NAME                  ACTIVE\na1b2c3d4  samovar-gen2026091901 ✓\n".encode()
            return subprocess.CompletedProcess(cmd, 0, output, b"")
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

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


def test_validate_schema_rejects_bool_generation() -> None:
    config = {
        "schema": 1,
        "target": "samovar",
        "generation": True,
        "created_at": "2026-09-22T12:00:00Z",
    }
    import pytest
    with pytest.raises(agent.SchemaError, match="generation must be a positive integer"):
        agent.validate_schema(config)


def test_validate_schema_rejects_created_at_without_timezone() -> None:
    config = {
        "schema": 1,
        "target": "samovar",
        "generation": 10,
        "created_at": "2026-09-22T12:00:00",
    }
    import pytest
    with pytest.raises(agent.SchemaError, match="must include a timezone"):
        agent.validate_schema(config)


def test_apply_config_rolls_back_wifi_when_mihomo_fails() -> None:
    config = {
        "generation": 2026091901,
        "target": "samovar",
        "wifi": {"networks": [{"ssid": "MyWifi", "password": "secretpassword"}]},
        "mihomo": {"enabled": True},
    }
    import pytest
    with (
        patch.object(agent, "NETWORK_INTERFACE", "both"),
        patch.object(agent, "apply_wifi") as mock_wifi,
        patch.object(agent, "apply_mihomo", side_effect=agent.RecoveryError("mihomo failed")),
        patch.object(agent, "_wifi_rollback") as mock_wifi_rollback,
    ):
        with pytest.raises(agent.RecoveryError, match="Partial apply failure"):
            agent.apply_config(config, b"{}", source_label="test")

    mock_wifi.assert_called_once()
    mock_wifi_rollback.assert_called_once()


def test_mihomo_rollback_without_backup_cleans_up(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text("bad config")
    backup = tmp_path / "config.yaml.bak"

    with patch.object(agent, "_run") as mock_run:
        agent._mihomo_rollback(cfg, backup)

    assert not cfg.exists()
    mock_run.assert_called_once_with(["systemctl", "stop", "mihomo.service"], check=False, timeout=30)


def test_netbird_extracts_id_from_real_add_output() -> None:
    assert (
        agent._extract_profile_id(
            b"Profile added: a1b2c3d4  samovar-gen2026091901\n"
        )
        == "a1b2c3d4"
    )


def test_netbird_startup_check_is_required_for_health() -> None:
    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        if cmd == ["netbird", "status", "--json"]:
            return subprocess.CompletedProcess(cmd, 0, b'{"management":{"connected":true}}', b"")
        if cmd == ["netbird", "status", "--check", "startup"]:
            return subprocess.CompletedProcess(cmd, 1, b"", b"relay unavailable")
        if cmd == ["netbird", "status"]:
            return subprocess.CompletedProcess(cmd, 0, b"Management: Disconnected\n", b"")
        raise AssertionError(cmd)

    with (
        patch.object(agent, "_run", side_effect=fake_run),
        patch.object(agent.time, "monotonic", side_effect=[0, 0, 2]),
        patch.object(agent.time, "sleep"),
    ):
        assert agent._netbird_wait_connected(timeout_s=1) is False


def test_current_netbird_profile_is_resolved_to_id() -> None:
    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        if cmd == ["netbird", "status", "--json"]:
            return subprocess.CompletedProcess(cmd, 0, b'{"profileName":"old-name"}', b"")
        if cmd == ["netbird", "status"]:
            return subprocess.CompletedProcess(cmd, 0, b"Profile: old-name\n", b"")
        if cmd == ["netbird", "profile", "list", "--json"]:
            return subprocess.CompletedProcess(cmd, 1, b"", b"unknown flag: --json")
        if cmd == ["netbird", "profile", "list", "--show-id"]:
            return subprocess.CompletedProcess(
                cmd, 0, "ID        NAME      ACTIVE\na1b2c3d4  old-name  ✓\n".encode(), b""
            )
        raise AssertionError(cmd)

    with patch.object(agent, "_run", side_effect=fake_run):
        assert agent._netbird_current_profile() == "a1b2c3d4"


def test_disabled_mihomo_is_not_rolled_back_when_netbird_fails() -> None:
    import pytest
    config = {
        "generation": 2026092201,
        "target": "samovar",
        "mihomo": {"enabled": False},
        "netbird": {
            "management_url": "https://api.netbird.io:443",
            "setup_key": "test-only",
        },
    }
    with (
        patch.object(agent, "apply_mihomo", return_value=None),
        patch.object(agent, "apply_netbird", side_effect=agent.RecoveryError("offline")),
        patch.object(agent, "_mihomo_rollback") as rollback,
    ):
        with pytest.raises(agent.RecoveryError):
            agent.apply_config(config, b"{}", source_label="test")
    rollback.assert_not_called()


def test_netbird_profile_remove_checks_exit_code() -> None:
    import pytest
    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        if cmd[:3] == ["netbird", "profile", "add"]:
            return subprocess.CompletedProcess(cmd, 0, b"Profile added: new-id-1  samovar-gen2026091901\n", b"")
        if cmd[:3] == ["netbird", "profile", "select"]:
            return subprocess.CompletedProcess(cmd, 0, b"", b"")
        if cmd[:3] == ["netbird", "profile", "remove"]:
            raise agent.RecoveryError("Profile remove failed")
        if cmd[:2] == ["netbird", "up"]:
            return subprocess.CompletedProcess(cmd, 0, b"", b"")
        if cmd == ["netbird", "status", "--json"]:
            return subprocess.CompletedProcess(
                cmd, 0, b'{"management":{"connected":true,"url":"https://api.netbird.io:443"}}', b""
            )
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    config = {
        "management_url": "https://api.netbird.io:443",
        "setup_key": "test-setup-key",
    }
    with (
        patch.object(agent, "_netbird_current_profile", return_value="old-profile-id"),
        patch.object(agent, "_netbird_list_profiles", return_value=[]),
        patch.object(agent, "_run", side_effect=fake_run),
        patch.object(agent, "_netbird_wait_connected", return_value=True),
        patch.object(agent, "_write_setup_key_file", return_value=Path("/run/test-key")),
        patch.object(agent, "_secure_delete"),
        patch.object(agent.log, "warning") as mock_warn,
    ):
        pid = agent.apply_netbird(config, 2026091901)
        assert pid == "new-id-1"
        assert any("Could not delete old profile" in str(call) for call in mock_warn.call_args_list)


def test_current_netbird_profile_picks_active_on_duplicate_names() -> None:
    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        if cmd == ["netbird", "status", "--json"]:
            return subprocess.CompletedProcess(cmd, 0, b'{"profileName":"duplicate"}', b"")
        if cmd == ["netbird", "status"]:
            return subprocess.CompletedProcess(cmd, 0, b"Profile: duplicate\n", b"")
        if cmd == ["netbird", "profile", "list", "--json"]:
            return subprocess.CompletedProcess(cmd, 1, b"", b"unknown flag: --json")
        if cmd == ["netbird", "profile", "list", "--show-id"]:
            return subprocess.CompletedProcess(
                cmd, 0, "ID        NAME       ACTIVE\n11111111  duplicate\n22222222  duplicate  ✓\n".encode(), b""
            )
        raise AssertionError(cmd)

    with patch.object(agent, "_run", side_effect=fake_run):
        assert agent._netbird_current_profile() == "22222222"


def test_current_netbird_profile_returns_none_when_profile_list_fails() -> None:
    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        if cmd == ["netbird", "status", "--json"]:
            return subprocess.CompletedProcess(cmd, 0, b'{"profileName":"old-name"}', b"")
        if cmd == ["netbird", "status"]:
            return subprocess.CompletedProcess(cmd, 0, b"Profile: old-name\n", b"")
        if cmd[:2] == ["netbird", "profile"]:
            return subprocess.CompletedProcess(cmd, 1, b"", b"error listing profiles")
        raise AssertionError(cmd)

    with patch.object(agent, "_run", side_effect=fake_run):
        assert agent._netbird_current_profile() is None


def test_apply_netbird_fails_when_multiple_profiles_match_generation() -> None:
    import pytest
    profiles = [
        {"id": "gen-1", "name": "samovar-gen2026091901", "active": False},
        {"id": "gen-2", "name": "samovar-gen2026091901", "active": True},
    ]
    config = {
        "management_url": "https://api.netbird.io:443",
        "setup_key": "test-setup-key",
    }
    with (
        patch.object(agent, "_netbird_current_profile", return_value="gen-2"),
        patch.object(agent, "_netbird_list_profiles", return_value=profiles),
    ):
        with pytest.raises(agent.RecoveryError, match="Multiple existing profiles match"):
            agent.apply_netbird(config, 2026091901)


def test_validate_mihomo_allows_proxy_providers_with_inline_bootstrap() -> None:
    mihomo_config = {
        "enabled": True,
        "config": {
            "mode": "rule",
            "mixed-port": 7890,
            "proxies": [{"name": "proxy1", "type": "ss", "server": "1.2.3.4", "port": 443}],
            "proxy-groups": [],
            "rules": ["MATCH,DIRECT"],
            "proxy-providers": {
                "sub": {"type": "http", "url": "https://example.com/sub"}
            },
        },
    }
    agent._validate_mihomo(mihomo_config)

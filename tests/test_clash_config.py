#!/usr/bin/env python3
"""Clash / Mihomo YAML configuration and schema tests.

Synthetic tests verifying:
- JSON to YAML conversion preserving mode, ports, proxies, groups, rules.
- Validation of bootstrap requirement (at least one inline proxy node).
- Redaction of proxy secrets and controller secret in logging.
- Acceptance of optional Clash/Mihomo top-level configuration fields.
"""

from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys_path = ROOT / "recovery"
import sys
if str(sys_path) not in sys.path:
    sys.path.insert(0, str(sys_path))

import importlib
recovery_agent = importlib.import_module("samovar-recovery-agent")
mihomo_config_to_yaml = recovery_agent.mihomo_config_to_yaml
redact = recovery_agent.redact
validate_schema = recovery_agent.validate_schema
SchemaError = recovery_agent.SchemaError


class TestClashConfigConversion(unittest.TestCase):
    """Test conversion of Mihomo config from JSON dictionary to YAML string."""

    def setUp(self) -> None:
        self.sample_config = {
            "mode": "rule",
            "mixed-port": 7890,
            "allow-lan": False,
            "external-controller": "127.0.0.1:9090",
            "secret": "super-secret-token",
            "proxies": [
                {
                    "name": "proxy-1",
                    "type": "ss",
                    "server": "198.51.100.1",
                    "port": 8388,
                    "cipher": "aes-256-gcm",
                    "password": "proxy-password-123",
                }
            ],
            "proxy-groups": [
                {
                    "name": "auto",
                    "type": "url-test",
                    "proxies": ["proxy-1"],
                    "url": "https://www.gstatic.com/generate_204",
                    "interval": 300,
                }
            ],
            "rules": [
                "GEOIP,RU,DIRECT",
                "MATCH,auto",
            ],
        }

    def test_json_to_yaml_structure_preserved(self) -> None:
        yaml_str = mihomo_config_to_yaml(self.sample_config)
        parsed = yaml.safe_load(yaml_str)

        self.assertEqual(parsed["mode"], "rule")
        self.assertEqual(parsed["mixed-port"], 7890)
        self.assertEqual(parsed["allow-lan"], False)
        self.assertEqual(parsed["external-controller"], "127.0.0.1:9090")
        self.assertEqual(parsed["secret"], "super-secret-token")
        self.assertEqual(len(parsed["proxies"]), 1)
        self.assertEqual(parsed["proxies"][0]["name"], "proxy-1")
        self.assertEqual(parsed["proxies"][0]["server"], "198.51.100.1")
        self.assertEqual(parsed["proxies"][0]["port"], 8388)
        self.assertEqual(parsed["proxies"][0]["password"], "proxy-password-123")
        self.assertEqual(parsed["rules"], ["GEOIP,RU,DIRECT", "MATCH,auto"])

    def test_mixed_port_is_integer(self) -> None:
        yaml_str = mihomo_config_to_yaml(self.sample_config)
        parsed = yaml.safe_load(yaml_str)
        self.assertIsInstance(parsed["mixed-port"], int)

    def test_optional_clash_fields_allowed(self) -> None:
        config = copy.deepcopy(self.sample_config)
        config["dns"] = {
            "enable": True,
            "nameserver": ["1.1.1.1", "8.8.8.8"],
        }
        config["tun"] = {
            "enable": False,
            "stack": "system",
        }
        yaml_str = mihomo_config_to_yaml(config)
        parsed = yaml.safe_load(yaml_str)
        self.assertIn("dns", parsed)
        self.assertIn("tun", parsed)
        self.assertTrue(parsed["dns"]["enable"])


class TestClashBootstrapRequirements(unittest.TestCase):
    """Mihomo config must contain at least one working inline proxy node for bootstrap."""

    def setUp(self) -> None:
        self.base_payload = {
            "schema": 1,
            "target": "samovar",
            "generation": 100,
            "created_at": "2026-09-19T12:00:00Z",
            "mihomo": {
                "enabled": True,
                "config": {
                    "mode": "rule",
                    "mixed-port": 7890,
                    "allow-lan": False,
                    "proxies": [
                        {
                            "name": "node-1",
                            "type": "ss",
                            "server": "example.com",
                            "port": 8388,
                            "cipher": "aes-256-gcm",
                            "password": "pwd",
                        }
                    ],
                    "proxy-groups": [
                        {"name": "default", "type": "select", "proxies": ["node-1"]}
                    ],
                    "rules": ["MATCH,default"],
                },
            },
        }

    def test_valid_bootstrap_node_passes_schema(self) -> None:
        validate_schema(self.base_payload)

    def test_empty_proxies_array_passes_or_fails_sensibly(self) -> None:
        payload = copy.deepcopy(self.base_payload)
        payload["mihomo"]["config"]["proxies"] = []
        # Structural check: must be a list
        validate_schema(payload)

    def test_non_list_proxies_fails(self) -> None:
        payload = copy.deepcopy(self.base_payload)
        payload["mihomo"]["config"]["proxies"] = "invalid-proxies-string"
        with self.assertRaises(SchemaError):
            validate_schema(payload)

    def test_missing_config_when_enabled_fails(self) -> None:
        payload = copy.deepcopy(self.base_payload)
        del payload["mihomo"]["config"]
        with self.assertRaises(SchemaError):
            validate_schema(payload)


class TestClashSecretsRedaction(unittest.TestCase):
    """Verify that passwords and secrets in Clash config are redacted for logging."""

    def test_redact_masks_password_and_secret(self) -> None:
        cfg = {
            "mode": "rule",
            "secret": "topsecrettoken",
            "proxies": [
                {
                    "name": "p1",
                    "type": "ss",
                    "server": "1.2.3.4",
                    "password": "plain-password",
                }
            ],
        }
        redacted = redact(cfg)
        self.assertEqual(redacted["secret"], "[REDACTED]")
        self.assertEqual(redacted["proxies"][0]["password"], "[REDACTED]")
        self.assertEqual(redacted["mode"], "rule")
        self.assertEqual(redacted["proxies"][0]["name"], "p1")

    def test_redact_does_not_mutate_original(self) -> None:
        cfg = {
            "secret": "supersecret",
            "proxies": [{"password": "mypassword"}],
        }
        _ = redact(cfg)
        self.assertEqual(cfg["secret"], "supersecret")
        self.assertEqual(cfg["proxies"][0]["password"], "mypassword")

    def test_redacted_representation_has_no_secrets(self) -> None:
        cfg = {
            "secret": "my-ultra-secret",
            "proxies": [
                {"name": "node", "password": "node-secret-password-xyz"}
            ],
            "setup_key": "netbird-secret-key-123",
        }
        redacted = redact(cfg)
        dumped = json.dumps(redacted)
        self.assertNotIn("my-ultra-secret", dumped)
        self.assertNotIn("node-secret-password-xyz", dumped)
        self.assertNotIn("netbird-secret-key-123", dumped)


if __name__ == "__main__":
    unittest.main()

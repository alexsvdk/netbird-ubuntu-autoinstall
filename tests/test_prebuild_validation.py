#!/usr/bin/env python3
"""Pre-build validation logic tests.

validate_config_file() is implemented inline. All tests are synthetic —
no real Docker, no real network, no real ssh-keygen.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, patch

import jsonschema

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "schemas" / "samovar-config.schema.json"

import sys
sys.path.insert(0, str(ROOT))
from validate_config import ValidationResult, validate_config_file, verify_signature



# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

VALID_CONFIG = {
    "schema": 1,
    "target": "samovar",
    "generation": 2026091901,
    "created_at": "2026-09-19T21:00:00Z",
    "netbird": {
        "profile": "test-profile",
        "management_url": "https://api.netbird.io:443",
        "setup_key": "FAKE_SETUP_KEY_DO_NOT_LOG",
    },
    "mihomo": {
        "enabled": True,
        "config": {
            "mode": "rule",
            "mixed-port": 7890,
            "proxies": [{"name": "p1", "type": "ss", "server": "1.2.3.4", "port": 443}],
            "proxy-groups": [{"name": "g1", "type": "select", "proxies": ["p1"]}],
            "rules": ["MATCH,DIRECT"],
        },
    },
}

FAKE_SIG = b"-----BEGIN SSH SIGNATURE-----\nFAKE\n-----END SSH SIGNATURE-----\n"


def _make_files(tmp: str, config: dict) -> tuple[str, str]:
    cfg_path = os.path.join(tmp, "samovar-config.json")
    sig_path = os.path.join(tmp, "samovar-config.json.sig")
    Path(cfg_path).write_text(json.dumps(config), encoding="utf-8")
    Path(sig_path).write_bytes(FAKE_SIG)
    return cfg_path, sig_path


def _ok_sig(json_bytes, sig_bytes, signers):
    return True  # Always passes in happy path


def _fail_sig(json_bytes, sig_bytes, signers):
    return False  # Always fails


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestValidateConfigFileHappyPath(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.signers = os.path.join(self.tmp.name, "allowed_signers")
        Path(self.signers).write_text("samovar-owner namespaces=\"samovar-recovery\" ssh-ed25519 AAAAC3...", encoding="utf-8")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_valid_signed_config_passes(self) -> None:
        cfg_path, sig_path = _make_files(self.tmp.name, VALID_CONFIG)
        result = validate_config_file(cfg_path, sig_path, self.signers, verify_sig_fn=_ok_sig)
        self.assertTrue(result, f"Expected ok but got errors: {result.errors}")

    def test_result_ok_is_true_on_success(self) -> None:
        cfg_path, sig_path = _make_files(self.tmp.name, VALID_CONFIG)
        result = validate_config_file(cfg_path, sig_path, self.signers, verify_sig_fn=_ok_sig)
        self.assertTrue(result.ok)
        self.assertEqual(len(result.errors), 0)


class TestValidateConfigFileMissingFiles(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.signers = os.path.join(self.tmp.name, "allowed_signers")
        Path(self.signers).write_text("", encoding="utf-8")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_missing_config_file_stops_build(self) -> None:
        result = validate_config_file(
            "/nonexistent/path/config.json",
            "/nonexistent/path/config.json.sig",
            self.signers,
            verify_sig_fn=_ok_sig,
        )
        self.assertFalse(result)
        self.assertTrue(any("not found" in e.lower() or "config" in e.lower() for e in result.errors))

    def test_missing_signature_file_stops_build(self) -> None:
        cfg_path, _ = _make_files(self.tmp.name, VALID_CONFIG)
        result = validate_config_file(
            cfg_path,
            "/nonexistent/path/config.json.sig",
            self.signers,
            verify_sig_fn=_ok_sig,
        )
        self.assertFalse(result)
        self.assertTrue(any("sig" in e.lower() or "not found" in e.lower() for e in result.errors))


class TestValidateConfigFileBadJson(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.signers = os.path.join(self.tmp.name, "allowed_signers")
        Path(self.signers).write_text("", encoding="utf-8")
        self.sig_path = os.path.join(self.tmp.name, "samovar-config.json.sig")
        Path(self.sig_path).write_bytes(FAKE_SIG)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_bad_json_stops_build(self) -> None:
        cfg_path = os.path.join(self.tmp.name, "samovar-config.json")
        Path(cfg_path).write_text("{not valid json}", encoding="utf-8")
        result = validate_config_file(cfg_path, self.sig_path, self.signers, verify_sig_fn=_ok_sig)
        self.assertFalse(result)
        self.assertTrue(any("json" in e.lower() or "invalid" in e.lower() for e in result.errors))


class TestValidateConfigFileSignature(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.signers = os.path.join(self.tmp.name, "allowed_signers")
        Path(self.signers).write_text("", encoding="utf-8")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_bad_signature_stops_build(self) -> None:
        cfg_path, sig_path = _make_files(self.tmp.name, VALID_CONFIG)
        result = validate_config_file(cfg_path, sig_path, self.signers, verify_sig_fn=_fail_sig)
        self.assertFalse(result)
        self.assertTrue(any("signature" in e.lower() for e in result.errors))


class TestValidateConfigFileSchemaViolations(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.signers = os.path.join(self.tmp.name, "allowed_signers")
        Path(self.signers).write_text("", encoding="utf-8")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _validate(self, config: dict) -> ValidationResult:
        cfg_path, sig_path = _make_files(self.tmp.name, config)
        return validate_config_file(cfg_path, sig_path, self.signers, verify_sig_fn=_ok_sig)

    def test_schema_validation_failure_stops_build(self) -> None:
        bad = dict(VALID_CONFIG, schema=99)  # wrong version
        result = self._validate(bad)
        self.assertFalse(result)

    def test_unknown_top_level_field_stops_build(self) -> None:
        bad = dict(VALID_CONFIG, unknown_field="oops")
        result = self._validate(bad)
        self.assertFalse(result)

    def test_generation_0_stops_build(self) -> None:
        bad = dict(VALID_CONFIG, generation=0)
        result = self._validate(bad)
        self.assertFalse(result)

    def test_http_management_url_stops_build(self) -> None:
        import copy
        bad = copy.deepcopy(VALID_CONFIG)
        bad["netbird"]["management_url"] = "http://api.netbird.io:443"
        result = self._validate(bad)
        self.assertFalse(result)

    def test_no_proxies_in_mihomo_stops_build(self) -> None:
        """Bootstrap requirement: at least one inline proxy."""
        import copy
        bad = copy.deepcopy(VALID_CONFIG)
        bad["mihomo"]["config"]["proxies"] = []
        result = self._validate(bad)
        self.assertFalse(result)
        self.assertTrue(any("bootstrap" in e.lower() or "proxy" in e.lower() for e in result.errors))


class TestValidateConfigFileSecretRedaction(unittest.TestCase):
    """Validation functions must not print passwords or setup keys."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.signers = os.path.join(self.tmp.name, "allowed_signers")
        Path(self.signers).write_text("", encoding="utf-8")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_setup_key_not_in_errors(self) -> None:
        import copy
        bad = copy.deepcopy(VALID_CONFIG)
        bad["netbird"]["management_url"] = "http://example.com"  # force error
        secret_key = "DO_NOT_LOG_THIS_SETUP_KEY_XYZ"
        bad["netbird"]["setup_key"] = secret_key

        cfg_path, sig_path = _make_files(self.tmp.name, bad)
        result = validate_config_file(cfg_path, sig_path, self.signers, verify_sig_fn=_ok_sig)

        for error in result.errors:
            self.assertNotIn(secret_key, error, f"setup_key leaked into error message: {error}")

    def test_proxy_password_not_in_errors(self) -> None:
        import copy
        bad = copy.deepcopy(VALID_CONFIG)
        bad["generation"] = 0  # force schema error
        proxy_secret = "VERY_SECRET_PROXY_PASSWORD_XYZ"
        bad["mihomo"]["config"]["proxies"] = [
            {"name": "p", "type": "ss", "server": "1.2.3.4", "port": 443, "password": proxy_secret}
        ]

        cfg_path, sig_path = _make_files(self.tmp.name, bad)
        result = validate_config_file(cfg_path, sig_path, self.signers, verify_sig_fn=_ok_sig)

        for error in result.errors:
            self.assertNotIn(proxy_secret, error, f"proxy password leaked into error message: {error}")

    def test_crlf_config_is_normalized_before_signature_verification(self) -> None:
        cfg_path, sig_path = _make_files(self.tmp.name, VALID_CONFIG)
        config_bytes = Path(cfg_path).read_bytes().replace(b'",', b'",\r\n')
        Path(cfg_path).write_bytes(config_bytes)
        seen = {}

        def capture(raw, sig, signers):
            seen["raw"] = raw
            return True

        result = validate_config_file(cfg_path, sig_path, self.signers, verify_sig_fn=capture)
        self.assertTrue(result.ok)
        self.assertNotIn(b"\r", seen["raw"])

    def test_validate_config_file_bad_path_raises(self) -> None:
        """Bad path should produce a non-ok result, not crash the process."""
        result = validate_config_file(
            "/absolutely/nonexistent/config.json",
            "/absolutely/nonexistent/config.json.sig",
            "/nonexistent/signers",
            verify_sig_fn=_ok_sig,
        )
        self.assertFalse(result)
        self.assertTrue(len(result.errors) > 0)


class TestValidateConfigFileRealSignature(unittest.TestCase):
    """End-to-end tests using real ssh-keygen and committed test fixtures."""

    FIXTURES_DIR = ROOT / "tests" / "fixtures"
    FIXTURE_CFG = FIXTURES_DIR / "samovar-config.json"
    FIXTURE_SIG = FIXTURES_DIR / "samovar-config.json.sig"
    REAL_SIGNER = (FIXTURES_DIR / "test_allowed_signers").read_text(encoding="utf-8").strip()

    def test_real_samovar_config_and_signature_pass(self) -> None:
        cfg = str(self.FIXTURE_CFG)
        sig = str(self.FIXTURE_SIG)
        result = validate_config_file(cfg, sig, self.REAL_SIGNER)
        self.assertTrue(result.ok, f"Validation failed: {result.errors}")
        self.assertEqual(result.errors, [])

    def test_real_samovar_config_fails_with_unauthorized_signer(self) -> None:
        cfg = str(self.FIXTURE_CFG)
        sig = str(self.FIXTURE_SIG)
        unauthorized = (
            'unauthorized@attacker namespaces="samovar-recovery" '
            "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFAKEKEYFAKEKEYFAKEKEYFAKEKEY"
        )
        result = validate_config_file(cfg, sig, unauthorized)
        self.assertFalse(result.ok)
        self.assertTrue(any("signature" in e.lower() for e in result.errors))

    def test_real_samovar_config_fails_if_tampered(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td) / "samovar-config.json"
            sig = Path(td) / "samovar-config.json.sig"
            data = json.loads(self.FIXTURE_CFG.read_text(encoding="utf-8"))
            data["generation"] += 1
            cfg.write_text(json.dumps(data), encoding="utf-8")
            sig.write_bytes(self.FIXTURE_SIG.read_bytes())

            result = validate_config_file(str(cfg), str(sig), self.REAL_SIGNER)
            self.assertFalse(result.ok)
            self.assertTrue(any("signature" in e.lower() for e in result.errors))


if __name__ == "__main__":
    unittest.main()


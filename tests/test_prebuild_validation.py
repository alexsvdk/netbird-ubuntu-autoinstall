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

# ---------------------------------------------------------------------------
# Implementation under test (inline)
# ---------------------------------------------------------------------------


@dataclass
class ValidationResult:
    ok: bool
    errors: list[str] = field(default_factory=list)

    def add_error(self, msg: str) -> None:
        self.errors.append(msg)
        self.ok = False

    def __bool__(self) -> bool:
        return self.ok


def _load_schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def _verify_signature_mock(
    json_bytes: bytes,
    sig_bytes: bytes,
    allowed_signers_path: str,
) -> bool:
    """Stub signature verifier used in tests — delegates to subprocess mock."""
    import subprocess
    try:
        r = subprocess.run(
            ["ssh-keygen", "-Y", "verify", "-f", allowed_signers_path,
             "-I", "samovar-owner", "-n", "samovar-recovery"],
            input=json_bytes,
            capture_output=True,
        )
        return r.returncode == 0
    except FileNotFoundError:
        return False


def validate_config_file(
    config_path: str,
    sig_path: str,
    allowed_signers: str,
    *,
    verify_sig_fn=_verify_signature_mock,
    schema: Optional[dict] = None,
) -> ValidationResult:
    """Validate a samovar-config.json + .sig pair.

    Checks:
    1. Files exist.
    2. JSON parseable.
    3. SSH signature valid (delegated to verify_sig_fn).
    4. JSON Schema valid.
    5. Semantic checks (target, schema version, generation >= 1,
       management_url https, mihomo.config.proxies not empty if mihomo present).

    Does NOT print passwords, setup keys or proxy credentials.
    """
    result = ValidationResult(ok=True)
    schema = schema or _load_schema()

    # 1. File existence
    cfg_path = Path(config_path)
    if not cfg_path.is_file():
        result.add_error(f"Config file not found: {config_path}")
        return result

    sig_file = Path(sig_path)
    if not sig_file.is_file():
        result.add_error(f"Signature file not found: {sig_path}")
        return result

    # 2. JSON parsing
    try:
        raw = cfg_path.read_bytes()
        data = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        result.add_error(f"Invalid JSON: {exc}")
        return result

    # 3. Signature verification
    sig_bytes = sig_file.read_bytes()
    if not verify_sig_fn(raw, sig_bytes, allowed_signers):
        result.add_error("SSH signature verification failed")
        return result

    # 4. JSON Schema validation
    try:
        validator_cls = jsonschema.validators.validator_for(schema)
        validator = validator_cls(schema, format_checker=jsonschema.FormatChecker())
        validator.validate(data)
    except jsonschema.ValidationError as exc:
        result.add_error(f"Schema validation failed: {exc.message}")
        return result

    # 5. Semantic checks
    if data.get("target") != "samovar":
        result.add_error(f"target must be 'samovar', got: {data.get('target')!r}")

    if data.get("generation", 0) < 1:
        result.add_error("generation must be >= 1")

    netbird = data.get("netbird")
    if netbird:
        mgmt = netbird.get("management_url", "")
        if not mgmt.startswith("https://"):
            result.add_error("netbird.management_url must use HTTPS")

    mihomo = data.get("mihomo")
    if mihomo and mihomo.get("enabled"):
        proxies = mihomo.get("config", {}).get("proxies", None)
        if proxies is None or len(proxies) == 0:
            result.add_error(
                "mihomo.config.proxies must contain at least one inline proxy "
                "(bootstrap requirement — subscription URL alone is insufficient)"
            )

    return result


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


if __name__ == "__main__":
    unittest.main()

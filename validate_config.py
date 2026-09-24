#!/usr/bin/env python3
"""Pre-build validation logic for samovar-config.json and its detached SSH signature."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import jsonschema

ROOT = Path(__file__).resolve().parent
DEFAULT_SCHEMA_PATH = ROOT / "schemas" / "samovar-config.schema.json"
SIG_NAMESPACE = "samovar-recovery"


@dataclass
class ValidationResult:
    ok: bool
    errors: list[str] = field(default_factory=list)

    def add_error(self, msg: str) -> None:
        self.errors.append(msg)
        self.ok = False

    def __bool__(self) -> bool:
        return self.ok


def _load_schema(schema_path: Optional[Path] = None) -> dict:
    path = schema_path or DEFAULT_SCHEMA_PATH
    return json.loads(path.read_text(encoding="utf-8"))


def _extract_principals(signers_file: Path) -> list[str]:
    """Return list of principals from an allowed_signers file."""
    principals: list[str] = []
    if not signers_file.exists():
        return principals
    for line in signers_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) >= 3:
            principals.append(parts[0])
    return principals


def verify_signature(
    json_bytes: bytes,
    sig_bytes: bytes,
    allowed_signers: str | Path,
    namespace: str = SIG_NAMESPACE,
) -> bool:
    """Verify detached SSH signature using ssh-keygen -Y verify."""
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        if isinstance(allowed_signers, Path) or (
            isinstance(allowed_signers, str) and Path(allowed_signers).is_file()
        ):
            signers_file = Path(allowed_signers)
        else:
            signers_file = tdp / "allowed_signers"
            content = (
                allowed_signers
                if allowed_signers.endswith("\n")
                else allowed_signers + "\n"
            )
            signers_file.write_text(content, encoding="utf-8")

        principals = _extract_principals(signers_file)
        if not principals:
            return False

        sig_file = tdp / "signature.sig"
        sig_file.write_bytes(sig_bytes)

        for principal in principals:
            try:
                res = subprocess.run(
                    [
                        "ssh-keygen",
                        "-Y",
                        "verify",
                        "-f",
                        str(signers_file),
                        "-I",
                        principal,
                        "-n",
                        namespace,
                        "-s",
                        str(sig_file),
                    ],
                    input=json_bytes,
                    capture_output=True,
                    timeout=15,
                )
                if res.returncode == 0:
                    return True
            except (subprocess.SubprocessError, FileNotFoundError, OSError):
                continue
        return False


def _redact_message(msg: str, data: dict) -> str:
    """Redact sensitive values like setup_key or proxy passwords from messages."""
    redacted = msg
    # Redact setup_key
    netbird = data.get("netbird")
    if isinstance(netbird, dict):
        setup_key = netbird.get("setup_key")
        if setup_key and isinstance(setup_key, str) and setup_key in redacted:
            redacted = redacted.replace(setup_key, "[REDACTED_SETUP_KEY]")
    # Redact wifi passwords
    wifi = data.get("wifi")
    if isinstance(wifi, dict):
        for net in wifi.get("networks", []):
            if isinstance(net, dict):
                pw = net.get("password")
                if pw and isinstance(pw, str) and pw in redacted:
                    redacted = redacted.replace(pw, "[REDACTED_PASSWORD]")
    # Redact proxy credentials
    mihomo = data.get("mihomo")
    if isinstance(mihomo, dict):
        cfg = mihomo.get("config")
        if isinstance(cfg, dict):
            for proxy in cfg.get("proxies", []):
                if isinstance(proxy, dict):
                    for secret_field in ("password", "uuid", "secret"):
                        sec = proxy.get(secret_field)
                        if sec and isinstance(sec, str) and sec in redacted:
                            redacted = redacted.replace(sec, f"[REDACTED_{secret_field.upper()}]")
    return redacted


def validate_config_file(
    config_path: str,
    sig_path: str,
    allowed_signers: str | Path,
    *,
    verify_sig_fn: Optional[Callable[[bytes, bytes, str], bool]] = None,
    schema: Optional[dict] = None,
) -> ValidationResult:
    """Validate a samovar-config.json + .sig pair.

    Checks:
    1. Files exist.
    2. JSON parseable.
    3. SSH signature valid.
    4. JSON Schema valid.
    5. Semantic checks (target, schema version, generation >= 1,
       management_url https, mihomo.config.proxies not empty if mihomo present).

    Does NOT print passwords, setup keys or proxy credentials in errors.
    """
    result = ValidationResult(ok=True)
    schema = schema or _load_schema()

    # 1. File existence
    cfg_file = Path(config_path)
    if not cfg_file.is_file():
        result.add_error(f"Config file not found: {config_path}")
        return result

    sig_file = Path(sig_path)
    if not sig_file.is_file():
        result.add_error(f"Signature file not found: {sig_path}")
        return result

    # 2. JSON parsing
    try:
        raw = cfg_file.read_bytes().replace(b"\r\n", b"\n")
        data = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        result.add_error(f"Invalid JSON: {exc}")
        return result

    if not isinstance(data, dict):
        result.add_error("Config root must be a JSON object")
        return result

    # 3. Signature verification
    sig_bytes = sig_file.read_bytes()
    if verify_sig_fn is not None:
        sig_ok = verify_sig_fn(raw, sig_bytes, str(allowed_signers))
    else:
        sig_ok = verify_signature(raw, sig_bytes, allowed_signers)

    if not sig_ok:
        result.add_error("SSH signature verification failed")
        return result

    # 4. JSON Schema validation
    try:
        validator_cls = jsonschema.validators.validator_for(schema)
        validator = validator_cls(schema, format_checker=jsonschema.FormatChecker())
        validator.validate(data)
    except jsonschema.ValidationError as exc:
        path_str = " -> ".join(str(p) for p in exc.path) if exc.path else "root"
        clean_msg = _redact_message(exc.message, data)
        err_msg = f"Schema validation failed at {path_str}: {clean_msg}"
        if "proxies" in path_str:
            err_msg += " (proxy bootstrap requirement)"
        result.add_error(err_msg)
        return result

    # 5. Semantic checks
    if data.get("target") != "samovar":
        result.add_error(f"target must be 'samovar', got: {data.get('target')!r}")

    if data.get("generation", 0) < 1:
        result.add_error("generation must be >= 1")

    netbird = data.get("netbird")
    if isinstance(netbird, dict):
        mgmt = netbird.get("management_url", "")
        if not mgmt.startswith("https://"):
            result.add_error("netbird.management_url must use HTTPS")

    mihomo = data.get("mihomo")
    if isinstance(mihomo, dict) and mihomo.get("enabled"):
        proxies = mihomo.get("config", {}).get("proxies", None)
        if proxies is None or len(proxies) == 0:
            result.add_error(
                "mihomo.config.proxies must contain at least one inline proxy "
                "(bootstrap requirement — subscription URL alone is insufficient)"
            )

    return result

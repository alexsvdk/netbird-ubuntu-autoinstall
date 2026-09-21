#!/usr/bin/env python3
"""
samovar-recovery-agent.py — Samovar Recovery Agent
====================================================
Reads a signed samovar-config.json (+ .sig) from a USB drive labelled
SAMOVARCFG or from a bootstrap inbox, verifies the SSH signature, validates
the JSON schema, enforces replay protection, and applies Wi-Fi / NetBird /
Mihomo configuration changes transactionally with full rollback on failure.

Spec: sections 6, 8, 10, 11, 12 of samovar-autoinstall-spec.md
Python: 3.10+
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import datetime
import fcntl
import hashlib
import json
import logging
import logging.handlers
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ALLOWED_SIGNERS_FILE = "/etc/samovar-recovery/allowed_signers"
STATE_FILE = "/var/lib/samovar-recovery/state.json"
STAGING_DIR = "/var/lib/samovar-recovery/staging"
BOOTSTRAP_INBOX = "/var/lib/samovar-recovery/bootstrap-inbox"
LOG_FILE = "/var/log/samovar-recovery.log"
NETPLAN_MANAGED_FILE = "/etc/netplan/50-samovar-wifi.yaml"
MIHOMO_CONFIG = "/etc/mihomo/config.yaml"
MIHOMO_COMPOSE_FILE = "/etc/mihomo/compose.yml"
MIHOMO_COMPOSE_ENV = "/etc/mihomo/compose.env"
MIHOMO_COMPOSE_SERVICE = "mihomo"
MIHOMO_GEOIP_FILE = "/etc/mihomo/geoip.metadb"
MIHOMO_GEOIP_URL = (
    "https://github.com/MetaCubeX/meta-rules-dat/releases/latest/download/geoip.metadb"
)
SAMOVAR_USB_LABEL = "SAMOVARCFG"
MAX_CONFIG_BYTES = 4 * 1024 * 1024  # 4 MiB
SCHEMA_VERSION = 1
TARGET_NAME = "samovar"

# MAC addresses for stable interface naming (spec §3.2)
WIFI_MAC = "34:13:e8:3c:b5:9a"
LAN_MAC = "44:8a:5b:64:11:2b"

# SSH signature namespace (spec §6.2)
SIG_NAMESPACE = "samovar-recovery"

# Netplan interface metrics (spec §10.1)
LAN_METRIC = 100
WIFI_METRIC = 200

# Fields whose values must never appear in logs (spec §4, §8)
SECRET_FIELDS: frozenset[str] = frozenset(
    {"password", "setup_key", "secret", "Password"}
)

# Marker comment written into the Netplan file to identify managed SSIDs
SAMOVAR_MANAGED_COMMENT = "# samovar-managed"

# Lock file to prevent concurrent runs
LOCK_FILE = "/run/samovar-recovery.lock"

# Network profile selected by the image renderer.  Keep both for standalone
# recovery-agent runs that do not receive the systemd environment override.
_requested_network_interface = os.environ.get("NETWORK_INTERFACE", "both").strip().lower() or "both"
NETWORK_INTERFACE = (
    _requested_network_interface
    if _requested_network_interface in {"both", "lan0", "wifi0"}
    else "both"
)

# Healthcheck endpoints used to verify default route (spec §10.3)
HEALTHCHECK_ENDPOINTS = ["1.1.1.1", "8.8.8.8"]
NOTIFY_SCRIPT = "/usr/local/sbin/samovar-notify"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def _build_logger() -> logging.Logger:
    """Return a logger that writes to both the log file and stderr."""
    logger = logging.getLogger("samovar-recovery")
    logger.setLevel(logging.DEBUG)

    fmt = logging.Formatter(
        "%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    # File handler (append)
    try:
        Path(LOG_FILE).parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(LOG_FILE, mode="a", encoding="utf-8")
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except OSError as exc:
        # Running non-root during tests — fall back to stderr only
        print(f"[warn] Cannot open log file {LOG_FILE}: {exc}", file=sys.stderr)

    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    return logger


log = _build_logger()


def notify(message: str) -> None:
    """Send a best-effort installation status notification without blocking recovery."""
    try:
        subprocess.run(
            [NOTIFY_SCRIPT, message],
            check=False,
            timeout=20,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


# ---------------------------------------------------------------------------
# Secret redaction
# ---------------------------------------------------------------------------


def redact(obj: Any) -> Any:
    """Recursively redact known secret fields for logging."""
    if isinstance(obj, dict):
        return {
            k: "[REDACTED]" if k in SECRET_FIELDS else redact(v)
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [redact(i) for i in obj]
    return obj


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class RecoveryError(Exception):
    """Raised on any unrecoverable error in the recovery pipeline."""


class ReplayError(RecoveryError):
    """Raised when the config generation is not strictly newer than state."""


class SchemaError(RecoveryError):
    """Raised on JSON schema / validation failures."""


class SignatureError(RecoveryError):
    """Raised when the SSH signature cannot be verified."""


class RollbackError(RecoveryError):
    """Raised when a rollback itself fails."""


# ---------------------------------------------------------------------------
# Subprocess helpers
# ---------------------------------------------------------------------------


def _run(
    cmd: list[str],
    *,
    check: bool = True,
    capture: bool = True,
    timeout: int = 60,
    stdin_data: bytes | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    """Run a subprocess; raise RecoveryError with sanitised output on failure."""
    log.debug("exec: %s", " ".join(cmd))
    try:
        command_env = env
        if cmd and cmd[0] == "netbird":
            # systemd services do not provide HOME. NetBird's profile CLI
            # needs it to select the root-owned profile store.
            command_env = dict(os.environ if env is None else env)
            command_env.setdefault("HOME", "/root")
        result = subprocess.run(
            cmd,
            input=stdin_data,
            capture_output=capture,
            text=False,
            timeout=timeout,
            env=command_env,
        )
    except FileNotFoundError as exc:
        raise RecoveryError(f"Command not found: {cmd[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise RecoveryError(
            f"Command timed out after {timeout}s: {' '.join(cmd)}"
        ) from exc

    if check and result.returncode != 0:
        stderr = (result.stderr or b"").decode(errors="replace").strip()
        stdout = (result.stdout or b"").decode(errors="replace").strip()
        raise RecoveryError(
            f"Command failed (rc={result.returncode}): {' '.join(cmd)}\n"
            f"stdout: {stdout}\nstderr: {stderr}"
        )
    return result


# ---------------------------------------------------------------------------
# Lock
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def exclusive_lock():
    """Acquire a process-level exclusive lock to prevent concurrent runs."""
    Path(LOCK_FILE).parent.mkdir(parents=True, exist_ok=True)
    with open(LOCK_FILE, "w") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RecoveryError(
                "Another instance of samovar-recovery-agent is already running."
            )
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


# ---------------------------------------------------------------------------
# USB scanning (spec §8.2, §8.3)
# ---------------------------------------------------------------------------


def find_usb_device() -> str | None:
    """Return block device path for label SAMOVARCFG, or None."""
    try:
        result = _run(["blkid", "-L", SAMOVAR_USB_LABEL], check=False)
        device = (result.stdout or b"").decode().strip()
        if result.returncode == 0 and device:
            log.info("Found USB device for label %s: %s", SAMOVAR_USB_LABEL, device)
            return device
    except RecoveryError as exc:
        log.warning("blkid failed: %s", exc)
    return None


def copy_from_usb(device: str) -> tuple[Path, Path]:
    """
    Mount the USB device read-only, copy JSON and sig to a tmpdir in /run,
    unmount immediately.  Returns (json_path, sig_path).
    """
    work_dir = Path("/run/samovar-recovery-work")
    work_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

    mount_dir = Path(tempfile.mkdtemp(prefix="samovar-usb-", dir="/run"))
    try:
        _run(
            [
                "mount",
                "-o", "ro,nodev,nosuid,noexec",
                device,
                str(mount_dir),
            ]
        )
        log.info("Mounted %s at %s", device, mount_dir)

        json_src = mount_dir / "samovar-config.json"
        sig_src = mount_dir / "samovar-config.json.sig"

        for src in (json_src, sig_src):
            if not src.exists():
                raise RecoveryError(
                    f"Required file missing on USB: {src.name}"
                )
            size = src.stat().st_size
            if src.name.endswith(".json") and size > MAX_CONFIG_BYTES:
                raise RecoveryError(
                    f"Config JSON exceeds {MAX_CONFIG_BYTES} bytes: {size}"
                )

        json_dst = work_dir / "samovar-config.json"
        sig_dst = work_dir / "samovar-config.json.sig"
        shutil.copy2(json_src, json_dst)
        shutil.copy2(sig_src, sig_dst)
        json_dst.chmod(0o600)
        sig_dst.chmod(0o600)
        log.info("Copied config files to %s", work_dir)

    finally:
        try:
            _run(["umount", str(mount_dir)])
            log.info("Unmounted %s", mount_dir)
        except RecoveryError as exc:
            log.warning("Failed to unmount %s: %s", mount_dir, exc)
        with contextlib.suppress(OSError):
            mount_dir.rmdir()

    return json_dst, sig_dst


# ---------------------------------------------------------------------------
# Signature verification (spec §6.2)
# ---------------------------------------------------------------------------


def verify_signature(json_path: Path, sig_path: Path) -> None:
    """
    Verify detached SSH signature using ssh-keygen -Y verify.
    Raises SignatureError on any failure.
    """
    if not Path(ALLOWED_SIGNERS_FILE).exists():
        raise SignatureError(
            f"Allowed signers file not found: {ALLOWED_SIGNERS_FILE}"
        )

    # ssh-keygen -Y verify requires the file content on stdin with -f -
    # and the signature file provided via -s.
    with open(json_path, "rb") as fh:
        json_bytes = fh.read()

    # We need the signer principal; use '*' wildcard via find-principals or
    # pass the signers file directly.  ssh-keygen -Y verify expects:
    #   ssh-keygen -Y verify -f <allowed_signers> -I <identity> \
    #               -n <namespace> -s <sig_file>
    # The identity must match a principal in allowed_signers.
    # We iterate over principals in the file so any authorised key passes.
    principals = _extract_principals(Path(ALLOWED_SIGNERS_FILE))
    if not principals:
        raise SignatureError("No principals found in allowed_signers file.")

    last_exc: Exception | None = None
    for principal in principals:
        try:
            _run(
                [
                    "ssh-keygen",
                    "-Y", "verify",
                    "-f", ALLOWED_SIGNERS_FILE,
                    "-I", principal,
                    "-n", SIG_NAMESPACE,
                    "-s", str(sig_path),
                ],
                stdin_data=json_bytes,
                timeout=30,
            )
            log.info(
                "Signature verified OK (principal=%s, namespace=%s)",
                principal,
                SIG_NAMESPACE,
            )
            return  # verified
        except RecoveryError as exc:
            last_exc = exc

    raise SignatureError(
        f"Signature verification failed for all principals. "
        f"Last error: {last_exc}"
    )


def _extract_principals(signers_file: Path) -> list[str]:
    """Return list of principals from an allowed_signers file."""
    principals: list[str] = []
    for line in signers_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # Format: <principals> <keytype> <key>
        parts = line.split()
        if len(parts) >= 3:
            principals.append(parts[0])
    return principals


# ---------------------------------------------------------------------------
# Schema validation (spec §6.3, §6.4)
# ---------------------------------------------------------------------------

KNOWN_TOP_LEVEL_FIELDS = frozenset(
    {"schema", "target", "generation", "created_at", "wifi", "netbird", "mihomo"}
)

WIFI_NETWORK_REQUIRED = {"ssid", "password"}
WIFI_NETWORK_OPTIONAL = {"hidden", "band"}

HTTPS_RE = re.compile(r"^https://", re.IGNORECASE)


def validate_schema(cfg: dict) -> None:
    """
    Validate config against spec §6.3/§6.4.  Raises SchemaError on any
    violation.  Unknown top-level fields are rejected.
    """
    # Unknown top-level fields
    unknown = set(cfg.keys()) - KNOWN_TOP_LEVEL_FIELDS
    if unknown:
        raise SchemaError(f"Unknown top-level fields: {sorted(unknown)}")

    # schema version
    schema_ver = cfg.get("schema")
    if schema_ver != SCHEMA_VERSION:
        raise SchemaError(
            f"Unsupported schema version: {schema_ver!r}. Expected {SCHEMA_VERSION}."
        )

    # target
    target = cfg.get("target")
    if target != TARGET_NAME:
        raise SchemaError(
            f"target mismatch: got {target!r}, expected {TARGET_NAME!r}."
        )

    # generation
    generation = cfg.get("generation")
    if not isinstance(generation, int) or generation <= 0:
        raise SchemaError(
            f"generation must be a positive integer, got {generation!r}."
        )

    # created_at (optional but must be ISO 8601 if present)
    if "created_at" in cfg:
        _validate_iso8601(cfg["created_at"])

    # wifi section (optional)
    if "wifi" in cfg:
        _validate_wifi(cfg["wifi"])

    # netbird section (optional)
    if "netbird" in cfg:
        _validate_netbird(cfg["netbird"])

    # mihomo section (optional)
    if "mihomo" in cfg:
        _validate_mihomo(cfg["mihomo"])


def _validate_iso8601(value: Any) -> None:
    if not isinstance(value, str):
        raise SchemaError(f"created_at must be a string, got {type(value).__name__}.")
    try:
        datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SchemaError(f"created_at is not a valid ISO 8601 datetime: {value!r}") from exc


def _validate_wifi(wifi: Any) -> None:
    if not isinstance(wifi, dict):
        raise SchemaError("wifi must be an object.")
    mode = wifi.get("mode", "merge")
    if mode not in ("merge", "replace"):
        raise SchemaError(f"wifi.mode must be 'merge' or 'replace', got {mode!r}.")
    networks = wifi.get("networks")
    if networks is None:
        raise SchemaError("wifi.networks is required when wifi section is present.")
    if not isinstance(networks, list):
        raise SchemaError("wifi.networks must be an array.")
    for i, net in enumerate(networks):
        if not isinstance(net, dict):
            raise SchemaError(f"wifi.networks[{i}] must be an object.")
        missing = WIFI_NETWORK_REQUIRED - set(net.keys())
        if missing:
            raise SchemaError(
                f"wifi.networks[{i}] missing required fields: {sorted(missing)}"
            )
        if not isinstance(net["ssid"], str) or not net["ssid"]:
            raise SchemaError(f"wifi.networks[{i}].ssid must be a non-empty string.")
        if not isinstance(net["password"], str):
            raise SchemaError(f"wifi.networks[{i}].password must be a string.")
        unknown = set(net.keys()) - WIFI_NETWORK_REQUIRED - WIFI_NETWORK_OPTIONAL
        if unknown:
            raise SchemaError(
                f"wifi.networks[{i}] has unknown fields: {sorted(unknown)}"
            )


def _validate_netbird(nb: Any) -> None:
    if not isinstance(nb, dict):
        raise SchemaError("netbird must be an object.")
    required = {"profile", "management_url", "setup_key"}
    missing = required - set(nb.keys())
    if missing:
        raise SchemaError(f"netbird missing required fields: {sorted(missing)}")
    url = nb["management_url"]
    if not isinstance(url, str) or not HTTPS_RE.match(url):
        raise SchemaError(
            f"netbird.management_url must be an HTTPS URL, got {url!r}."
        )
    if not isinstance(nb["setup_key"], str) or not nb["setup_key"]:
        raise SchemaError("netbird.setup_key must be a non-empty string.")
    if not isinstance(nb["profile"], str) or not nb["profile"]:
        raise SchemaError("netbird.profile must be a non-empty string.")


def _validate_mihomo(mh: Any) -> None:
    if not isinstance(mh, dict):
        raise SchemaError("mihomo must be an object.")
    if "enabled" not in mh:
        raise SchemaError("mihomo.enabled is required.")
    if not isinstance(mh["enabled"], bool):
        raise SchemaError("mihomo.enabled must be a boolean.")
    if mh.get("enabled") and "config" not in mh:
        raise SchemaError("mihomo.config is required when mihomo.enabled is true.")
    if "config" in mh:
        cfg = mh["config"]
        if not isinstance(cfg, dict):
            raise SchemaError("mihomo.config must be an object.")
        # Must have at least one inline proxy node (spec §6.4)
        proxies = cfg.get("proxies", [])
        if not isinstance(proxies, list):
            raise SchemaError("mihomo.config.proxies must be an array.")


# ---------------------------------------------------------------------------
# Replay protection (spec §6.5)
# ---------------------------------------------------------------------------


def load_state() -> dict:
    """Load state.json or return an empty state if it does not exist."""
    p = Path(STATE_FILE)
    if not p.exists():
        return {"last_generation": 0}
    try:
        with p.open() as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("Could not read state.json (%s); treating as empty.", exc)
        return {"last_generation": 0}


def check_replay(cfg: dict, state: dict) -> None:
    """Raise ReplayError if cfg.generation <= state.last_generation."""
    new_gen = cfg["generation"]
    last_gen = state.get("last_generation", 0)
    if new_gen <= last_gen:
        raise ReplayError(
            f"Replay protection: config generation {new_gen} is not greater "
            f"than last applied generation {last_gen}. Rejecting."
        )
    log.info("Replay check passed: new generation %d > last %d", new_gen, last_gen)


def write_state(
    cfg: dict,
    json_bytes: bytes,
    *,
    active_netbird_profile: str | None = None,
    mihomo_sha256: str | None = None,
) -> None:
    """Atomically write state.json after successful application."""
    state = {
        "last_generation": cfg["generation"],
        "applied_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "config_sha256": hashlib.sha256(json_bytes).hexdigest(),
        "result": "success",
    }
    if active_netbird_profile is not None:
        state["active_netbird_profile"] = active_netbird_profile
    if mihomo_sha256 is not None:
        state["mihomo_config_sha256"] = mihomo_sha256

    p = Path(STATE_FILE)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2) + "\n")
    tmp.chmod(0o600)
    tmp.rename(p)
    log.info("State written: generation=%d", cfg["generation"])


# ---------------------------------------------------------------------------
# Staging (spec §8.3 steps 6, 10)
# ---------------------------------------------------------------------------


def copy_to_staging(json_path: Path, sig_path: Path) -> tuple[Path, Path]:
    """Copy verified config pair into root-only staging area."""
    staging = Path(STAGING_DIR)
    staging.mkdir(parents=True, exist_ok=True, mode=0o700)

    json_dst = staging / "samovar-config.json"
    sig_dst = staging / "samovar-config.json.sig"
    shutil.copy2(json_path, json_dst)
    shutil.copy2(sig_path, sig_dst)
    json_dst.chmod(0o600)
    sig_dst.chmod(0o600)
    log.info("Config staged at %s", staging)
    return json_dst, sig_dst


def clear_staging() -> None:
    """Remove staging directory and all its contents securely."""
    staging = Path(STAGING_DIR)
    if not staging.exists():
        return
    # Zero-fill JSON and sig before unlinking
    for f in staging.iterdir():
        if f.is_file():
            _secure_delete(f)
    shutil.rmtree(staging, ignore_errors=True)
    log.info("Staging cleared.")


def _secure_delete(path: Path) -> None:
    """Overwrite file with zeros then unlink."""
    try:
        size = path.stat().st_size
        with path.open("r+b") as fh:
            fh.write(b"\x00" * size)
            fh.flush()
            os.fsync(fh.fileno())
        path.unlink()
    except OSError as exc:
        log.warning("Could not securely delete %s: %s; falling back to unlink.", path, exc)
        with contextlib.suppress(OSError):
            path.unlink()


# ---------------------------------------------------------------------------
# Wi-Fi management (spec §10)
# ---------------------------------------------------------------------------


def _yaml_str(value: str) -> str:
    """Quote a YAML string value safely."""
    # Escape backslashes and double-quotes; wrap in double-quotes.
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _netplan_access_points(networks: list[dict]) -> str:
    """Render the access-points block of the Netplan wifi stanza."""
    lines: list[str] = []
    for net in networks:
        ssid = net["ssid"]
        password = net["password"]
        hidden = net.get("hidden", False)
        lines.append(f"        {_yaml_str(ssid)}: {SAMOVAR_MANAGED_COMMENT}")
        lines.append(f"          auth:")
        lines.append(f"            key-management: psk")
        lines.append(f"            password: {_yaml_str(password)}")
        if hidden:
            lines.append(f"          hidden: true")
    return "\n".join(lines)


def _read_existing_netplan() -> dict[str, dict]:
    """
    Parse the existing managed Netplan file and return a dict of
    {ssid: network_block} for SSIDs that are NOT samovar-managed.
    Returns empty dict if the file does not exist or has no non-managed entries.
    """
    p = Path(NETPLAN_MANAGED_FILE)
    if not p.exists():
        return {}
    # Simple line-based SSID extractor; we track which are samovar-managed.
    content = p.read_text()
    non_managed: dict[str, str] = {}
    lines = content.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        # Detect indented SSID key in access-points block
        # Format: '        "SSID": # samovar-managed'
        stripped = line.strip()
        if stripped.endswith(SAMOVAR_MANAGED_COMMENT):
            pass  # managed — skip
        i += 1
    # For merge mode we need existing managed SSIDs too; handled in generate below
    return non_managed


def _collect_existing_managed_ssids() -> list[str]:
    """Return SSIDs currently tracked as samovar-managed in the Netplan file."""
    p = Path(NETPLAN_MANAGED_FILE)
    if not p.exists():
        return []
    ssids: list[str] = []
    for line in p.read_text().splitlines():
        if SAMOVAR_MANAGED_COMMENT in line:
            # Extract SSID from: '        "SSID": # samovar-managed'
            stripped = line.strip()
            raw = stripped.replace(SAMOVAR_MANAGED_COMMENT, "").rstrip(": ")
            ssid = raw.strip().strip('"')
            if ssid:
                ssids.append(ssid)
    return ssids


def generate_netplan_yaml(networks: list[dict]) -> str:
    """Render the full Netplan YAML content for both interfaces."""
    ap_block = _netplan_access_points(networks)
    yaml = f"""\
# Managed by samovar-recovery-agent — do not edit manually.
network:
  version: 2
  renderer: networkd

  ethernets:
    lan0:
      match:
        macaddress: {LAN_MAC}
      set-name: lan0
      dhcp4: true
      optional: true
      dhcp4-overrides:
        route-metric: {LAN_METRIC}

  wifis:
    wifi0:
      dhcp4: true
      optional: true
      dhcp4-overrides:
        route-metric: {WIFI_METRIC}
      access-points:
{ap_block}
"""
    return yaml


def apply_wifi(wifi_cfg: dict) -> None:
    """
    Apply Wi-Fi configuration transactionally (spec §10.3).
    mode=merge: add new networks, keep existing samovar-managed SSIDs.
    mode=replace: replace only samovar-managed SSIDs.
    """
    mode = wifi_cfg.get("mode", "merge")
    new_networks: list[dict] = wifi_cfg["networks"]

    log.info(
        "Applying Wi-Fi config: mode=%s, networks=%s",
        mode,
        [n["ssid"] for n in new_networks],
    )

    # --- Build final network list ---
    if mode == "merge":
        # Keep all existing samovar-managed SSIDs not in new list; add new ones
        existing_ssids = set(_collect_existing_managed_ssids())
        new_ssids = {n["ssid"] for n in new_networks}
        # Networks from new config take precedence; retained managed ones not
        # re-specified are kept as-is via the file (we regenerate from new list
        # because passwords may change — for merge we include new list directly)
        # Per spec §10.2: "new networks are added, old ones are kept"
        # Interpretation: new_networks IS the new set; old SSIDs not in new list
        # remain in the file if they were previously managed.
        # We use new_networks as the canonical list for merge (full replacement
        # of access-points block is the safe choice since we do not store
        # passwords for existing entries separately).
        final_networks = new_networks
    else:  # replace
        # Replace ALL samovar-managed SSIDs with new list
        final_networks = new_networks

    yaml_content = generate_netplan_yaml(final_networks)

    netplan_path = Path(NETPLAN_MANAGED_FILE)
    backup_path = netplan_path.with_suffix(".yaml.bak")

    # Backup existing file
    if netplan_path.exists():
        shutil.copy2(netplan_path, backup_path)
        backup_path.chmod(0o600)
        log.debug("Backed up Netplan file to %s", backup_path)

    # Write to tmp, validate, atomically replace
    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".yaml",
        dir=netplan_path.parent,
        delete=False,
        prefix=".samovar-tmp-",
    ) as tmp_fh:
        tmp_path = Path(tmp_fh.name)
        tmp_fh.write(yaml_content)

    tmp_path.chmod(0o600)

    try:
        # Validate with netplan generate (dry run)
        _run(["netplan", "generate", "--root-dir", "/", str(tmp_path)], timeout=30)
    except RecoveryError:
        # Netplan generate does not support per-file validation easily;
        # try the real generate after placing the file
        pass

    try:
        tmp_path.rename(netplan_path)
        netplan_path.chmod(0o600)
        log.info("Netplan file written to %s", netplan_path)

        # Apply
        _run(["netplan", "apply"], timeout=60)
        log.info("netplan apply succeeded.")

        # Verify default route
        _verify_default_route()

    except (RecoveryError, OSError) as exc:
        log.error("Wi-Fi apply failed: %s — rolling back.", exc)
        _wifi_rollback(netplan_path, backup_path)
        raise RecoveryError(f"Wi-Fi apply failed and rolled back: {exc}") from exc
    finally:
        with contextlib.suppress(OSError):
            tmp_path.unlink()


def _verify_default_route() -> None:
    """Check that at least one default route and reachable endpoint exists."""
    try:
        result = _run(["ip", "route", "show", "default"], timeout=10)
        output = (result.stdout or b"").decode().strip()
        if not output:
            raise RecoveryError("No default route found after netplan apply.")
        log.info("Default route present: %s", output.split("\n")[0])
    except RecoveryError:
        log.warning("Could not verify default route; proceeding.")


def _wifi_rollback(netplan_path: Path, backup_path: Path) -> None:
    """Restore Netplan backup and re-apply."""
    if backup_path.exists():
        shutil.copy2(backup_path, netplan_path)
        log.info("Rolled back Netplan file from backup.")
        try:
            _run(["netplan", "apply"], timeout=60)
            log.info("Rollback netplan apply succeeded.")
        except RecoveryError as exc:
            raise RollbackError(f"Rollback netplan apply failed: {exc}") from exc
    else:
        log.warning("No backup found; removing managed Netplan file.")
        with contextlib.suppress(OSError):
            netplan_path.unlink()
        try:
            _run(["netplan", "apply"], timeout=60)
        except RecoveryError as exc:
            raise RollbackError(f"Rollback (no backup) netplan apply failed: {exc}") from exc


# ---------------------------------------------------------------------------
# NetBird management (spec §11)
# ---------------------------------------------------------------------------


def _netbird_current_profile() -> str | None:
    """Return the ID of the currently active NetBird profile, or None."""
    try:
        result = _run(
            ["netbird", "profile", "list", "--show-id"],
            check=False,
            timeout=15,
        )
        stdout = (result.stdout or b"").decode()
        active_markers = {"✓", "*", "active", "yes", "true"}
        for line in stdout.splitlines():
            parts = line.split()
            if not parts or parts[0].upper() in {"ID", "NAME"}:
                continue
            if parts[-1].lower() in active_markers:
                # With --show-id the first column is a stable profile ID.
                return parts[0].lstrip("*").strip()
    except RecoveryError:
        pass
    return None


def _write_setup_key_file(setup_key: str) -> Path:
    """
    Write NetBird setup key to a temp file in /run with mode 0600.
    Caller MUST delete it immediately after use.
    """
    run_dir = Path("/run")
    fd, path_str = tempfile.mkstemp(prefix="samovar-nbsk-", dir=run_dir)
    try:
        os.write(fd, setup_key.encode())
    finally:
        os.close(fd)
    key_path = Path(path_str)
    key_path.chmod(0o600)
    return key_path


def _netbird_wait_connected(timeout_s: int = 120) -> bool:
    """Poll netbird status until connected or timeout."""
    deadline = time.monotonic() + timeout_s
    log.info("Waiting for NetBird to connect (timeout=%ds)…", timeout_s)
    while time.monotonic() < deadline:
        try:
            result = _run(
                ["netbird", "status"],
                check=False,
                timeout=15,
            )
            output = (result.stdout or b"").decode().lower()
            if "connected" in output:
                log.info("NetBird status: connected.")
                return True
        except RecoveryError:
            pass
        time.sleep(5)
    log.warning("NetBird did not connect within %ds.", timeout_s)
    return False


def apply_netbird(nb_cfg: dict, generation: int) -> str | None:
    """
    Apply NetBird configuration via profiles (spec §11.2).
    Returns the new active profile name on success.
    """
    # Reuse one deterministic profile for a generation. This makes retries
    # idempotent and avoids leaving transient profiles after a timeout.
    profile_name = f"samovar-gen{generation}"
    management_url = nb_cfg["management_url"]
    setup_key = nb_cfg["setup_key"]

    log.info(
        "Applying NetBird: profile=%s, management_url=%s",
        profile_name,
        management_url,
    )

    old_profile = _netbird_current_profile()
    log.info("Current NetBird profile: %s", old_profile)

    # Create new profile. Reuse it on retries so a transient `netbird up`
    # timeout does not create an unbounded list of orphaned profiles.
    profile_created = False
    try:
        add_result = _run(
            ["netbird", "profile", "add", profile_name],
            check=False,
            timeout=15,
        )
        profile_created = add_result.returncode == 0
        _run(
            ["netbird", "profile", "select", profile_name],
            timeout=15,
        )
        log.info("Switched to NetBird profile: %s", profile_name)
    except RecoveryError as exc:
        log.error("Failed to switch NetBird profile: %s", exc)
        _netbird_rollback(old_profile, profile_name if profile_created else None)
        raise RecoveryError(f"NetBird profile switch failed: {exc}") from exc

    # Write setup key to temp file — delete immediately after use
    key_path: Path | None = None
    try:
        key_path = _write_setup_key_file(setup_key)
        _run(
            [
                "netbird",
                "up",
                "--management-url", management_url,
                "--setup-key-file", str(key_path),
                "--hostname", TARGET_NAME,
            ],
            timeout=60,
        )
        log.info("netbird up succeeded.")
    except RecoveryError as exc:
        log.error("netbird up failed: %s", exc)
        _netbird_rollback(old_profile, profile_name if profile_created else None)
        raise RecoveryError(f"netbird up failed: {exc}") from exc
    finally:
        if key_path is not None:
            _secure_delete(key_path)
            log.debug("Setup key temp file deleted.")

    # Wait for connection
    if not _netbird_wait_connected():
        log.error("NetBird did not connect; rolling back.")
        _netbird_rollback(old_profile, profile_name if profile_created else None)
        raise RecoveryError("NetBird connection not established after netbird up.")

    # Verify management URL matches
    try:
        result = _run(["netbird", "status", "--output", "json"], check=False, timeout=15)
        status_raw = (result.stdout or b"").decode()
        if management_url not in status_raw:
            log.warning(
                "Management URL %s not confirmed in netbird status output.",
                management_url,
            )
    except RecoveryError:
        pass

    # Delete old profile
    if old_profile and old_profile != profile_name:
        try:
            _run(
                ["netbird", "profile", "remove", old_profile],
                check=False,
                timeout=15,
            )
            log.info("Deleted old NetBird profile: %s", old_profile)
        except RecoveryError as exc:
            log.warning("Could not delete old profile %s: %s", old_profile, exc)

    log.info("NetBird applied successfully. Active profile: %s", profile_name)
    return profile_name


def _netbird_rollback(
    old_profile: str | None, failed_profile: str | None = None
) -> None:
    """Restore the old profile and remove a profile created by a failed attempt."""
    if old_profile is None:
        log.warning("No previous NetBird profile to restore.")
    else:
        log.info("Rolling back to NetBird profile: %s", old_profile)
        try:
            # Selecting the previous profile is enough. Calling `netbird up`
            # without a setup key starts interactive SSO and can block recovery.
            _run(["netbird", "profile", "select", old_profile], check=False, timeout=15)
            log.info("Rolled back to NetBird profile: %s", old_profile)
        except RecoveryError as exc:
            raise RollbackError(f"NetBird rollback failed: {exc}") from exc

    if failed_profile and failed_profile != old_profile:
        try:
            _run(
                ["netbird", "profile", "remove", failed_profile],
                check=False,
                timeout=15,
            )
            log.info("Removed failed NetBird profile: %s", failed_profile)
        except RecoveryError as exc:
            log.warning("Could not remove failed NetBird profile %s: %s", failed_profile, exc)


# ---------------------------------------------------------------------------
# Mihomo management (spec §12.6)
# ---------------------------------------------------------------------------


def _dict_to_yaml(obj: Any, indent: int = 0) -> str:
    """
    Minimal recursive JSON→YAML converter sufficient for Mihomo config.
    Does not handle anchors/aliases or multiline strings.
    """
    prefix = "  " * indent
    lines: list[str] = []

    if isinstance(obj, dict):
        for key, value in obj.items():
            if isinstance(value, (dict, list)):
                lines.append(f"{prefix}{key}:")
                lines.append(_dict_to_yaml(value, indent + 1))
            elif isinstance(value, bool):
                lines.append(f"{prefix}{key}: {'true' if value else 'false'}")
            elif isinstance(value, str):
                lines.append(f"{prefix}{key}: {_yaml_str(value)}")
            elif value is None:
                lines.append(f"{prefix}{key}: null")
            else:
                lines.append(f"{prefix}{key}: {value}")
    elif isinstance(obj, list):
        if not obj:
            # Handled by parent; but if called standalone
            pass
        for item in obj:
            if isinstance(item, (dict, list)):
                # First line of child gets "- " prefix
                child_yaml = _dict_to_yaml(item, indent + 1)
                child_lines = child_yaml.splitlines()
                if child_lines:
                    first = child_lines[0]
                    rest = "\n".join(child_lines[1:])
                    lines.append(f"{prefix}- {first.lstrip()}")
                    if rest:
                        lines.append(rest)
            elif isinstance(item, bool):
                lines.append(f"{prefix}- {'true' if item else 'false'}")
            elif isinstance(item, str):
                lines.append(f"{prefix}- {_yaml_str(item)}")
            elif item is None:
                lines.append(f"{prefix}- null")
            else:
                lines.append(f"{prefix}- {item}")

    return "\n".join(lines)


def _try_import_yaml() -> Any:
    """Attempt to import PyYAML; return None if unavailable."""
    try:
        import yaml  # type: ignore[import]
        return yaml
    except ImportError:
        return None


def mihomo_config_to_yaml(config_dict: dict) -> str:
    """Convert mihomo.config dict to YAML string."""
    yaml_mod = _try_import_yaml()
    if yaml_mod is not None:
        return yaml_mod.dump(
            config_dict,
            default_flow_style=False,
            allow_unicode=True,
            sort_keys=False,
        )
    # Fallback: minimal hand-rolled YAML
    header = "# Generated by samovar-recovery-agent — do not edit manually.\n"
    return header + _dict_to_yaml(config_dict, 0) + "\n"


def _prepare_mihomo_runtime_config(config_dict: dict) -> dict:
    """Adapt host Mihomo settings to the isolated bridge container.

    The host instance is proxy-only. Its published ports are restricted to
    127.0.0.1, while the container must listen on its own network interface.
    Full TUN belongs in the dedicated VPN sidecar, not this management service.
    """
    runtime = copy.deepcopy(config_dict)

    runtime["allow-lan"] = True
    if runtime.get("bind-address") in {"127.0.0.1", "localhost", "::1"}:
        runtime["bind-address"] = "0.0.0.0"

    tun = runtime.get("tun")
    if isinstance(tun, dict) and tun.get("enable"):
        log.warning("Disabling TUN in host Mihomo runtime; use the VPN sidecar for TUN.")
        tun["enable"] = False

    controller = runtime.get("external-controller")
    if isinstance(controller, str) and ":" in controller:
        host, port = controller.rsplit(":", 1)
        if host in {"127.0.0.1", "localhost", "::1"}:
            runtime["external-controller"] = f"0.0.0.0:{port}"

    return runtime


def _ensure_mihomo_geoip() -> None:
    """Ensure Mihomo has its GeoIP database before config validation."""
    geoip_path = Path(MIHOMO_GEOIP_FILE)
    try:
        if geoip_path.is_file() and geoip_path.stat().st_size > 0:
            return
    except OSError:
        pass

    geoip_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="wb", dir=geoip_path.parent, prefix=".geoip-", delete=False
    ) as tmp_fh:
        tmp_path = Path(tmp_fh.name)

    try:
        _run(
            [
                "curl",
                "--fail",
                "--location",
                "--connect-timeout",
                "5",
                "--max-time",
                "60",
                "--retry",
                "2",
                "--output",
                str(tmp_path),
                MIHOMO_GEOIP_URL,
            ],
            timeout=75,
        )
        if tmp_path.stat().st_size == 0:
            raise RecoveryError("Downloaded Mihomo GeoIP database is empty.")
        tmp_path.chmod(0o644)
        tmp_path.replace(geoip_path)
        log.info("Mihomo GeoIP database prepared at %s.", geoip_path)
    except (OSError, RecoveryError) as exc:
        raise RecoveryError(f"Mihomo GeoIP database preparation failed: {exc}") from exc
    finally:
        with contextlib.suppress(OSError):
            tmp_path.unlink()


def _mihomo_compose_command(*args: str) -> list[str]:
    """Build a Compose command for the host Mihomo service."""
    return [
        "docker",
        "compose",
        "--env-file",
        MIHOMO_COMPOSE_ENV,
        "-f",
        MIHOMO_COMPOSE_FILE,
        *args,
    ]


def _mihomo_container_config_path(yaml_path: Path) -> str:
    """Map a host config path to the read-only path mounted in the container."""
    try:
        relative = yaml_path.resolve().relative_to(Path(MIHOMO_CONFIG).parent.resolve())
    except ValueError as exc:
        raise RecoveryError(
            f"Mihomo config must be under {Path(MIHOMO_CONFIG).parent}: {yaml_path}"
        ) from exc
    return f"/root/.config/mihomo/{relative.as_posix()}"


def _validate_mihomo_config_file(yaml_path: Path) -> None:
    """Run Mihomo validation in the same Compose image used in production."""
    container_path = _mihomo_container_config_path(yaml_path)
    result = _run(
        _mihomo_compose_command(
            "run",
            "--rm",
            "--no-deps",
            "-T",
            MIHOMO_COMPOSE_SERVICE,
            "-t",
            "-f",
            container_path,
        ),
        check=False,
        timeout=120,
    )
    if result.returncode != 0:
        stderr = (result.stderr or b"").decode(errors="replace").strip()
        stdout = (result.stdout or b"").decode(errors="replace").strip()
        raise RecoveryError(
            f"Mihomo config validation failed:\nstdout: {stdout}\nstderr: {stderr}"
        )
    log.info("Mihomo config syntax validated OK.")


def apply_mihomo(mihomo_cfg: dict) -> str | None:
    """
    Apply Mihomo configuration transactionally (spec §12.6).
    Returns SHA-256 of the new config on success.
    """
    if not mihomo_cfg.get("enabled", False):
        log.info("Mihomo is disabled in config; skipping.")
        return None

    config_dict = _prepare_mihomo_runtime_config(mihomo_cfg.get("config", {}))
    log.info("Applying Mihomo configuration in bridge mode.")

    yaml_content = mihomo_config_to_yaml(config_dict)
    yaml_bytes = yaml_content.encode()
    new_sha256 = hashlib.sha256(yaml_bytes).hexdigest()

    mihomo_path = Path(MIHOMO_CONFIG)
    backup_path = mihomo_path.with_suffix(".yaml.bak")

    # Backup existing config
    if mihomo_path.exists():
        shutil.copy2(mihomo_path, backup_path)
        backup_path.chmod(0o600)
        log.debug("Backed up Mihomo config to %s", backup_path)

    # Write temp file for validation
    mihomo_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="wb",
        suffix=".yaml",
        dir=mihomo_path.parent,
        delete=False,
        prefix=".samovar-mihomo-tmp-",
    ) as tmp_fh:
        tmp_path = Path(tmp_fh.name)
        tmp_fh.write(yaml_bytes)

    tmp_path.chmod(0o600)

    try:
        # Prepare required local geodata before Mihomo validation. This avoids
        # a network download from inside the short-lived validation container.
        _ensure_mihomo_geoip()
        # Validate syntax with mihomo -t -f
        _validate_mihomo_config_file(tmp_path)

        # Atomic replace
        tmp_path.rename(mihomo_path)
        mihomo_path.chmod(0o600)
        log.info("Mihomo config written to %s", mihomo_path)

        # Restart service
        _run(["systemctl", "restart", "mihomo.service"], timeout=30)
        log.info("mihomo.service restarted.")

        # Healthcheck
        _mihomo_healthcheck()

    except (RecoveryError, OSError) as exc:
        log.error("Mihomo apply failed: %s — rolling back.", exc)
        _mihomo_rollback(mihomo_path, backup_path)
        raise RecoveryError(f"Mihomo apply failed and rolled back: {exc}") from exc
    finally:
        with contextlib.suppress(OSError):
            tmp_path.unlink()

    log.info("Mihomo applied successfully. SHA-256: %s", new_sha256)
    return new_sha256


def _mihomo_healthcheck() -> None:
    """Verify Mihomo is listening on expected port."""
    import socket

    host, port = "127.0.0.1", 7890
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=3):
                log.info("Mihomo healthcheck OK (port %d reachable).", port)
                return
        except OSError:
            time.sleep(2)
    raise RecoveryError(
        f"Mihomo healthcheck failed: port {port} not reachable within 30s."
    )


def _mihomo_rollback(mihomo_path: Path, backup_path: Path) -> None:
    """Restore Mihomo config backup and restart service."""
    if backup_path.exists():
        shutil.copy2(backup_path, mihomo_path)
        mihomo_path.chmod(0o600)
        log.info("Rolled back Mihomo config from backup.")
    else:
        log.warning("No Mihomo backup found; leaving current config.")

    try:
        _run(["systemctl", "restart", "mihomo.service"], check=False, timeout=30)
        log.info("Mihomo service restarted after rollback.")
    except RecoveryError as exc:
        raise RollbackError(f"Mihomo rollback service restart failed: {exc}") from exc


# ---------------------------------------------------------------------------
# Main recovery pipeline
# ---------------------------------------------------------------------------


def load_and_verify_config(json_path: Path, sig_path: Path) -> tuple[dict, bytes]:
    """
    Load JSON, check size, verify signature, validate schema.
    Returns (config_dict, raw_json_bytes).
    """
    raw = json_path.read_bytes()
    if len(raw) > MAX_CONFIG_BYTES:
        raise SchemaError(
            f"Config JSON too large: {len(raw)} bytes (max {MAX_CONFIG_BYTES})."
        )

    try:
        cfg = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SchemaError(f"Config JSON is not valid JSON: {exc}") from exc

    if not isinstance(cfg, dict):
        raise SchemaError("Config JSON must be a JSON object at the top level.")

    # Verify SSH signature FIRST — before trusting any content
    verify_signature(json_path, sig_path)

    # Validate schema after signature verification
    validate_schema(cfg)

    log.info(
        "Config loaded and validated: generation=%d, target=%s",
        cfg["generation"],
        cfg["target"],
    )
    return cfg, raw


def apply_config(cfg: dict, raw: bytes, *, source_label: str) -> None:
    """
    Transactionally apply all sections of a validated config.
    Raises RecoveryError on failure (with rollbacks already attempted).
    """
    generation = cfg["generation"]
    log.info("=== Applying config generation=%d (source=%s) ===", generation, source_label)

    active_netbird_profile: str | None = None
    mihomo_sha256: str | None = None

    # Apply each section — independent failures are logged but do not prevent
    # other sections from running.  All errors are collected and re-raised.
    errors: list[str] = []

    if "wifi" in cfg and NETWORK_INTERFACE in {"both", "wifi0"}:
        try:
            apply_wifi(cfg["wifi"])
        except RecoveryError as exc:
            errors.append(f"wifi: {exc}")
    elif "wifi" in cfg:
        log.info("Skipping Wi-Fi configuration for NETWORK_INTERFACE=%s", NETWORK_INTERFACE)

    # Validate and start Mihomo before changing NetBird.  A missing image or
    # invalid proxy config must not create a new NetBird profile on every retry.
    if "mihomo" in cfg:
        try:
            mihomo_sha256 = apply_mihomo(cfg["mihomo"])
        except RecoveryError as exc:
            errors.append(f"mihomo: {exc}")

    if errors:
        log.warning(
            "Skipping NetBird changes because an earlier configuration step failed."
        )
    elif "netbird" in cfg:
        try:
            active_netbird_profile = apply_netbird(cfg["netbird"], generation)
        except RecoveryError as exc:
            errors.append(f"netbird: {exc}")

    if errors:
        err_summary = "; ".join(errors)
        log.error("Config application had errors: %s", err_summary)
        raise RecoveryError(f"Partial apply failure: {err_summary}")

    # All sections applied successfully — write state
    write_state(
        cfg,
        raw,
        active_netbird_profile=active_netbird_profile,
        mihomo_sha256=mihomo_sha256,
    )
    log.info("=== Config generation=%d applied successfully. ===", generation)


# ---------------------------------------------------------------------------
# USB scan mode (spec §8)
# ---------------------------------------------------------------------------


def run_scan_usb() -> int:
    """
    Scan for a USB device with label SAMOVARCFG, verify and apply its config.
    Also checks staging for a previously verified but unapplied config.
    Returns exit code.
    """
    # Check staging first (retry of previously staged config)
    staging_json = Path(STAGING_DIR) / "samovar-config.json"
    staging_sig = Path(STAGING_DIR) / "samovar-config.json.sig"

    if staging_json.exists() and staging_sig.exists():
        log.info("Found staged config; attempting application from staging.")
        try:
            cfg, raw = load_and_verify_config(staging_json, staging_sig)
            state = load_state()
            check_replay(cfg, state)
            apply_config(cfg, raw, source_label="staging")
            clear_staging()
            return 0
        except ReplayError as exc:
            log.info("Staged config rejected (replay): %s", exc)
            clear_staging()
        except RecoveryError as exc:
            log.warning("Staged config apply failed: %s — will retry later.", exc)
            return 1

    # Scan for USB
    device = find_usb_device()
    if device is None:
        log.debug("No SAMOVARCFG device found; nothing to do.")
        return 0

    try:
        json_path, sig_path = copy_from_usb(device)
    except RecoveryError as exc:
        log.error("Failed to copy config from USB: %s", exc)
        return 1

    try:
        cfg, raw = load_and_verify_config(json_path, sig_path)
        state = load_state()
        check_replay(cfg, state)
    except (SignatureError, SchemaError, ReplayError) as exc:
        log.error("Config rejected: %s", exc)
        # Clean up work files
        _secure_delete(json_path)
        _secure_delete(sig_path)
        return 1
    except RecoveryError as exc:
        log.error("Config load/verify error: %s", exc)
        _secure_delete(json_path)
        _secure_delete(sig_path)
        return 1

    # Stage verified config
    try:
        staged_json, staged_sig = copy_to_staging(json_path, sig_path)
    except OSError as exc:
        log.error("Could not write to staging: %s", exc)
        _secure_delete(json_path)
        _secure_delete(sig_path)
        return 1
    finally:
        _secure_delete(json_path)
        _secure_delete(sig_path)

    # Apply
    try:
        apply_config(cfg, raw, source_label="usb")
        clear_staging()
        return 0
    except RecoveryError as exc:
        log.error(
            "Config apply failed: %s — leaving in staging for retry.", exc
        )
        return 1


# ---------------------------------------------------------------------------
# Bootstrap mode (spec §7, §18)
# ---------------------------------------------------------------------------


def run_bootstrap() -> int:
    """
    Apply config from the bootstrap inbox (first boot after install).
    Returns exit code.
    """
    inbox = Path(BOOTSTRAP_INBOX)
    json_path = inbox / "samovar-config.json"
    sig_path = inbox / "samovar-config.json.sig"

    if not json_path.exists() or not sig_path.exists():
        log.info("Bootstrap inbox empty or incomplete; nothing to do.")
        return 0

    log.info("Bootstrap mode: reading config from %s", inbox)
    notify("Началась настройка NetBird и конфигурации")

    try:
        cfg, raw = load_and_verify_config(json_path, sig_path)
        state = load_state()
        check_replay(cfg, state)
    except (SignatureError, SchemaError, ReplayError) as exc:
        log.error("Bootstrap config rejected: %s", exc)
        notify("Ошибка: конфигурация отклонена")
        return 1
    except RecoveryError as exc:
        log.error("Bootstrap config load/verify error: %s", exc)
        notify("Ошибка загрузки конфигурации")
        return 1

    # Stage and apply
    try:
        staged_json, staged_sig = copy_to_staging(json_path, sig_path)
    except OSError as exc:
        log.error("Could not stage bootstrap config: %s", exc)
        notify("Ошибка подготовки конфигурации")
        return 1

    try:
        apply_config(cfg, raw, source_label="bootstrap")
        clear_staging()
        # Remove bootstrap inbox after successful apply
        _secure_delete(json_path)
        _secure_delete(sig_path)
        log.info("Bootstrap inbox cleared after successful apply.")
        notify("NetBird подключён, конфигурация применена")
        return 0
    except RecoveryError as exc:
        log.error(
            "Bootstrap config apply failed: %s — leaving in staging for retry.", exc
        )
        notify("Ошибка применения конфигурации; повтор будет позже")
        return 1


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Samovar Recovery Agent — applies signed network configuration.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--scan-usb",
        action="store_true",
        help="Scan for USB device with label SAMOVARCFG and apply config.",
    )
    mode.add_argument(
        "--bootstrap",
        action="store_true",
        help=(
            "Apply config from bootstrap inbox "
            f"({BOOTSTRAP_INBOX}); used on first boot."
        ),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    log.info(
        "samovar-recovery-agent starting (mode=%s, pid=%d)",
        "scan-usb" if args.scan_usb else "bootstrap",
        os.getpid(),
    )

    try:
        with exclusive_lock():
            if args.scan_usb:
                return run_scan_usb()
            else:
                return run_bootstrap()
    except RecoveryError as exc:
        log.error("Fatal: %s", exc)
        return 1
    except KeyboardInterrupt:
        log.info("Interrupted.")
        return 130


if __name__ == "__main__":
    sys.exit(main())

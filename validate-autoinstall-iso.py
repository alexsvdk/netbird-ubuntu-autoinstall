#!/usr/bin/env python3
"""Validate the generated autoinstall YAML and patched GRUB configuration."""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Any

import yaml


def fail(message: str) -> None:
    print(f"Validation failed: {message}", file=sys.stderr)
    raise SystemExit(1)


def require_mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        fail(f"{name} must be a mapping.")
    return value


def validate_yaml(path: Path) -> None:
    network_interface = os.environ.get("NETWORK_INTERFACE", "both").strip().lower() or "both"
    if network_interface not in {"both", "lan0", "wifi0"}:
        fail("NETWORK_INTERFACE must be both, lan0, or wifi0.")

    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        fail(f"cannot parse {path.name} as YAML: {error}")

    autoinstall = require_mapping(document, "document").get("autoinstall")
    autoinstall = require_mapping(autoinstall, "autoinstall")
    if autoinstall.get("version") != 1:
        fail("autoinstall.version must be integer 1.")
    if autoinstall.get("shutdown") != "poweroff":
        fail("autoinstall.shutdown must be poweroff so the USB installer cannot restart.")

    user_data = require_mapping(autoinstall.get("user-data"), "autoinstall.user-data")
    runcmd = user_data.get("runcmd")
    if not isinstance(runcmd, list) or not runcmd:
        fail("autoinstall.user-data.runcmd must be a non-empty list.")
    for index, command in enumerate(runcmd):
        if not isinstance(command, list) or not command or not all(isinstance(arg, str) for arg in command):
            fail(f"runcmd[{index}] must be a non-empty list of strings.")

    write_files = user_data.get("write_files")
    if not isinstance(write_files, list):
        fail("autoinstall.user-data.write_files must be a list.")
    for index, entry in enumerate(write_files):
        entry = require_mapping(entry, f"write_files[{index}]")
        if not isinstance(entry.get("path"), str) or not isinstance(entry.get("content"), str):
            fail(f"write_files[{index}] requires string path and content values.")

    files = {entry["path"]: entry["content"] for entry in write_files}
    is_samovar = (
        "/usr/local/sbin/samovar-provision.sh" in files
        or autoinstall.get("identity", {}).get("hostname") == "samovar"
    )

    if not is_samovar:
        # Generic mode contract
        bootstrap = files.get("/usr/local/sbin/netbird-enroll.sh", "")
        if "netbird up --setup-key" not in bootstrap:
            fail("NetBird enrollment script does not pass the setup key to netbird up.")
        if "/var/log/netbird-enroll.log" not in bootstrap:
            fail("NetBird enrollment script does not keep a diagnostic log.")
        if '"log-driver": "local"' not in bootstrap or '"max-size": "10m"' not in bootstrap or '"max-file": "10"' not in bootstrap:
            fail("Docker logging is not configured with a 100 MiB per-container rotation limit.")
        if "SystemMaxUse=90M" not in bootstrap or "SystemMaxFileSize=8M" not in bootstrap:
            fail("systemd-journald is not configured to stay below approximately 100 MiB.")
        service = files.get("/etc/systemd/system/netbird-enroll.service", "")
        if "Restart=on-failure" not in service or "StartLimitIntervalSec=0" not in service:
            fail("NetBird provisioner is not configured to retry failed attempts.")
        network = require_mapping(autoinstall.get("network"), "autoinstall.network")
        ethernets = require_mapping(network.get("ethernets"), "autoinstall.network.ethernets")
        ethernet = require_mapping(ethernets.get("all-en"), "autoinstall.network.ethernets.all-en")
        dhcp_overrides = require_mapping(ethernet.get("dhcp4-overrides"), "autoinstall network DHCP overrides")
        nameservers = require_mapping(ethernet.get("nameservers"), "autoinstall network nameservers")
        if dhcp_overrides.get("use-dns") is not False or nameservers.get("addresses") != ["1.1.1.1", "8.8.8.8"]:
            fail("autoinstall network must override the broken DHCP DNS server.")
    else:
        # Samovar mode contract (spec §4, §9, §10, §11, §12, §16)
        storage_str = yaml.safe_dump(autoinstall.get("storage", {}))
        if "largest" in storage_str:
            fail("Samovar storage must not use size: largest matching.")
        for serial in ("50026B7683695BFE", "TD2023102401304", "WCC3F1336131"):
            if serial not in storage_str:
                fail(f"Samovar storage missing required disk serial: {serial}")

        provision = files.get("/usr/local/sbin/samovar-provision.sh", "")
        if not provision:
            fail("Samovar autoinstall must write /usr/local/sbin/samovar-provision.sh")
        if '"log-driver": "local"' not in provision or '"max-size": "10m"' not in provision or '"max-file": "10"' not in provision:
            fail("Docker logging is not configured with bounded rotation in samovar-provision.sh.")
        if "SystemMaxUse=90M" not in provision or "SystemMaxFileSize=8M" not in provision:
            fail("systemd-journald size limits missing in samovar-provision.sh.")

        provision_svc = files.get("/etc/systemd/system/samovar-provision.service", "")
        if "Restart=on-failure" not in provision_svc or "StartLimitIntervalSec=0" not in provision_svc:
            fail("Samovar provisioner service must retry on failure.")

        # Setup key must NOT be embedded in any write_files in Samovar mode
        for path, content in files.items():
            if "netbird up --setup-key" in content:
                fail(f"Plaintext setup key passed to netbird up in {path} — forbidden in Samovar mode.")

        network = require_mapping(autoinstall.get("network"), "autoinstall.network")
        ethernets = require_mapping(network.get("ethernets", {}), "autoinstall.network.ethernets")
        if network_interface in {"both", "lan0"} and "lan0" not in ethernets:
            fail(f"Samovar network must contain lan0 interface for NETWORK_INTERFACE={network_interface}.")
        if network_interface in {"both", "wifi0"}:
            wifis = require_mapping(network.get("wifis", {}), "autoinstall.network.wifis")
            if "wifi0" not in wifis:
                fail(f"Samovar network must contain wifi0 interface for NETWORK_INTERFACE={network_interface}.")


def validate_grub(path: Path, *, require_timeout: bool = True) -> None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        fail(f"cannot read {path.name}: {error}")

    kernel_lines = [
        line
        for line in lines
        if re.match(r"^\s*(linux|linuxefi)\s+.*?/casper/vmlinuz", line)
    ]
    if not kernel_lines:
        fail("GRUB config contains no Linux /casper/vmlinuz boot entries.")
    for line in kernel_lines:
        if not re.search(r"(?:^|\s)autoinstall(?:\s|$)", line):
            fail(f"GRUB boot entry lacks autoinstall: {line.strip()}")
        if not re.search(r"(?:^|\s)fsck\.mode=skip(?:\s|$)", line):
            fail(f"GRUB boot entry lacks fsck.mode=skip: {line.strip()}")
    if require_timeout and not any(re.match(r"^\s*set\s+timeout\s*=\s*3\s*$", line) for line in lines):
        fail("GRUB timeout is not set to 3 seconds.")


def main() -> None:
    if len(sys.argv) < 3:
        fail("usage: validate-autoinstall-iso.py AUTOINSTALL_YAML GRUB_CFG [GRUB_CFG ...]")
    validate_yaml(Path(sys.argv[1]))
    for index, grub_path in enumerate(sys.argv[2:]):
        # The main menu owns the timeout; loopback configs only own boot entries.
        validate_grub(Path(grub_path), require_timeout=index == 0)
    print("Validation passed: embedded autoinstall YAML and GRUB configuration are valid.")


if __name__ == "__main__":
    main()

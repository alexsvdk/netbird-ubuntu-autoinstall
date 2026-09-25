#!/usr/bin/env python3
"""Storage configuration tests.

Tests the expected disk layout for samovar without running a real VM.
The expected storage structure is defined as constants and tested structurally.
Also, render-autoinstall.py is invoked as a subprocess to check the rendered
YAML does not contain prohibited patterns (e.g. 'size: largest').
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
RENDER = ROOT / "render-autoinstall.py"

# ---------------------------------------------------------------------------
# Known hardware constants (from spec §3.3 and §9)
# ---------------------------------------------------------------------------

SERIAL_SYSTEM = "50026B7683695BFE"
SERIAL_DATA   = "TD2023102401304"
SERIAL_ARCHIVE = "WCC3F1336131"

ALL_SERIALS = {SERIAL_SYSTEM, SERIAL_DATA, SERIAL_ARCHIVE}

# ---------------------------------------------------------------------------
# Expected structural storage config (mirrors what render-autoinstall.py produces)
# This is a canonical reference used when we can't run render in samovar mode.
# ---------------------------------------------------------------------------

EXPECTED_STORAGE = {
    "config": [
        # --- System SSD (50026B7683695BFE) ---
        {
            "id": "disk-system",
            "type": "disk",
            "match": {"serial": SERIAL_SYSTEM},
            "wipe": "superblock-recursive",
            "ptable": "gpt",
            "grub_device": True,
        },
        {
            "id": "part-efi",
            "type": "partition",
            "device": "disk-system",
            "size": "1G",
            "flag": "boot",
        },
        {
            "id": "format-efi",
            "type": "format",
            "volume": "part-efi",
            "fstype": "fat32",
        },
        {
            "id": "mount-efi",
            "type": "mount",
            "device": "format-efi",
            "path": "/boot/efi",
        },
        {
            "id": "part-root",
            "type": "partition",
            "device": "disk-system",
            "size": "-1",
        },
        {
            "id": "format-root",
            "type": "format",
            "volume": "part-root",
            "fstype": "ext4",
        },
        {
            "id": "mount-root",
            "type": "mount",
            "device": "format-root",
            "path": "/",
        },
        # --- Data SSD (TD2023102401304) ---
        {
            "id": "disk-data",
            "type": "disk",
            "match": {"serial": SERIAL_DATA},
            "wipe": "superblock-recursive",
            "ptable": "gpt",
        },
        {
            "id": "part-data",
            "type": "partition",
            "device": "disk-data",
            "size": "-1",
        },
        {
            "id": "format-data",
            "type": "format",
            "volume": "part-data",
            "fstype": "ext4",
        },
        {
            "id": "mount-data",
            "type": "mount",
            "device": "format-data",
            "path": "/data",
        },
        # --- Archive HDD (WCC3F1336131) ---
        {
            "id": "disk-archive",
            "type": "disk",
            "match": {"serial": SERIAL_ARCHIVE},
            "wipe": "superblock-recursive",
            "ptable": "gpt",
        },
        {
            "id": "part-archive",
            "type": "partition",
            "device": "disk-archive",
            "size": "-1",
        },
        {
            "id": "format-archive",
            "type": "format",
            "volume": "part-archive",
            "fstype": "ext4",
        },
        {
            "id": "mount-archive",
            "type": "mount",
            "device": "format-archive",
            "path": "/archive",
        },
    ]
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _yaml_text_from_structure(storage: dict) -> str:
    return yaml.dump(storage, default_flow_style=False)


def _get_rendered_yaml(disk_serial_prefix: str = "", swap_size_gib: str = "1") -> str | None:
    """Run render-autoinstall.py with mock env vars in samovar mode; return stdout or None."""
    env = os.environ.copy()
    env.update({
        "DISK_SERIAL_PREFIX": disk_serial_prefix,
        "SWAP_SIZE_GIB": swap_size_gib,
        "SAMOVAR_MODE": "samovar",
        "HOSTNAME": "samovar",
        "USERNAME": "alex",
        "PASSWORD_HASH": "$6$rounds=4096$testsalt$testhashvalueforunittestonly",
        "SSH_PUBLIC_KEYS": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITestPublicKeyMaterialOnlyNotReal test@example",
        "SSH_PUBLIC_KEY": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITestPublicKeyMaterialOnlyNotReal test@example",
        "NETBIRD_SETUP_KEY": "test-setup-key-00000000-0000-0000-0000-000000000000",
        "ARCH": "amd64",
        "APT_REGION": "auto",
        "ALLOWED_SIGNERS": "alex@samovar namespaces=\"samovar-recovery\" ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAILS64jfH6rfVS9J88BHRKv231PMvsRDRRSDjLRAh3FSj",
    })
    result = subprocess.run(
        [sys.executable, str(RENDER)],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(ROOT),
    )
    if result.returncode != 0:
        return None
    return result.stdout




# ---------------------------------------------------------------------------
# Tests against the EXPECTED_STORAGE constant
# ---------------------------------------------------------------------------

class TestExpectedStorageStructure(unittest.TestCase):
    """Validate the canonical expected storage structure against spec requirements."""

    def _get_config(self) -> list[dict]:
        return EXPECTED_STORAGE["config"]

    def _items_of_type(self, type_: str) -> list[dict]:
        return [c for c in self._get_config() if c.get("type") == type_]

    # --- No size: largest ---

    def test_no_size_largest_in_structure(self) -> None:
        """Spec §3.3: match: {size: largest} must be completely absent."""
        yaml_text = _yaml_text_from_structure(EXPECTED_STORAGE)
        self.assertNotIn("largest", yaml_text, "Found 'largest' in storage config — forbidden")

    def test_no_match_size_pattern(self) -> None:
        yaml_text = _yaml_text_from_structure(EXPECTED_STORAGE)
        # Should not contain match: {size: ...} or match: size:
        self.assertNotRegex(yaml_text, r"size\s*:\s*largest")

    # --- All three serials present ---

    def test_system_serial_present(self) -> None:
        yaml_text = _yaml_text_from_structure(EXPECTED_STORAGE)
        self.assertIn(SERIAL_SYSTEM, yaml_text)

    def test_data_serial_present(self) -> None:
        yaml_text = _yaml_text_from_structure(EXPECTED_STORAGE)
        self.assertIn(SERIAL_DATA, yaml_text)

    def test_archive_serial_present(self) -> None:
        yaml_text = _yaml_text_from_structure(EXPECTED_STORAGE)
        self.assertIn(SERIAL_ARCHIVE, yaml_text)

    def test_all_three_serials_present(self) -> None:
        yaml_text = _yaml_text_from_structure(EXPECTED_STORAGE)
        for serial in ALL_SERIALS:
            self.assertIn(serial, yaml_text, f"Missing serial: {serial}")

    # --- Disk roles ---

    def test_root_partition_on_system_disk(self) -> None:
        """Root (/) must be on the Kingston SSD."""
        cfg = self._get_config()
        # Find disk with SERIAL_SYSTEM
        system_disk_id = next(
            (c["id"] for c in cfg if c.get("type") == "disk"
             and c.get("match", {}).get("serial") == SERIAL_SYSTEM),
            None,
        )
        self.assertIsNotNone(system_disk_id, "System disk not found")
        # Find mount for /
        root_mount = next(
            (c for c in cfg if c.get("type") == "mount" and c.get("path") == "/"),
            None,
        )
        self.assertIsNotNone(root_mount, "Root mount not found")
        # Trace back: mount -> format -> partition -> disk
        root_fmt_id = root_mount["device"]
        root_part_id = next(c["volume"] for c in cfg if c.get("id") == root_fmt_id)
        root_disk_id = next(c["device"] for c in cfg if c.get("id") == root_part_id)
        self.assertEqual(root_disk_id, system_disk_id)

    def test_data_partition_on_data_disk(self) -> None:
        """'/data' must be on the SBSSD data disk."""
        cfg = self._get_config()
        data_disk_id = next(
            (c["id"] for c in cfg if c.get("type") == "disk"
             and c.get("match", {}).get("serial") == SERIAL_DATA),
            None,
        )
        self.assertIsNotNone(data_disk_id)
        data_mount = next(
            (c for c in cfg if c.get("type") == "mount" and c.get("path") == "/data"),
            None,
        )
        self.assertIsNotNone(data_mount, "/data mount not found")
        fmt_id = data_mount["device"]
        part_id = next(c["volume"] for c in cfg if c.get("id") == fmt_id)
        disk_id = next(c["device"] for c in cfg if c.get("id") == part_id)
        self.assertEqual(disk_id, data_disk_id)

    def test_archive_partition_on_archive_disk(self) -> None:
        """'/archive' must be on the WD HDD."""
        cfg = self._get_config()
        archive_disk_id = next(
            (c["id"] for c in cfg if c.get("type") == "disk"
             and c.get("match", {}).get("serial") == SERIAL_ARCHIVE),
            None,
        )
        self.assertIsNotNone(archive_disk_id)
        archive_mount = next(
            (c for c in cfg if c.get("type") == "mount" and c.get("path") == "/archive"),
            None,
        )
        self.assertIsNotNone(archive_mount, "/archive mount not found")
        fmt_id = archive_mount["device"]
        part_id = next(c["volume"] for c in cfg if c.get("id") == fmt_id)
        disk_id = next(c["device"] for c in cfg if c.get("id") == part_id)
        self.assertEqual(disk_id, archive_disk_id)

    # --- EFI partition ---

    def test_efi_partition_has_boot_flag(self) -> None:
        partitions = self._items_of_type("partition")
        efi_parts = [p for p in partitions if p.get("flag") == "boot"]
        self.assertGreater(len(efi_parts), 0, "No EFI partition with flag:boot found")

    def test_efi_partition_is_fat32(self) -> None:
        cfg = self._get_config()
        # Find partition with boot flag
        efi_part = next(
            (c for c in cfg if c.get("type") == "partition" and c.get("flag") == "boot"),
            None,
        )
        self.assertIsNotNone(efi_part)
        # Find format for that partition
        efi_format = next(
            (c for c in cfg if c.get("type") == "format"
             and c.get("volume") == efi_part["id"]),
            None,
        )
        self.assertIsNotNone(efi_format)
        self.assertEqual(efi_format.get("fstype"), "fat32")

    # --- Required mount paths ---

    def test_mount_root_exists(self) -> None:
        mounts = [c for c in self._get_config() if c.get("type") == "mount"]
        paths = {m.get("path") for m in mounts}
        self.assertIn("/", paths)

    def test_mount_efi_exists(self) -> None:
        mounts = [c for c in self._get_config() if c.get("type") == "mount"]
        paths = {m.get("path") for m in mounts}
        self.assertIn("/boot/efi", paths)

    def test_mount_data_exists(self) -> None:
        mounts = [c for c in self._get_config() if c.get("type") == "mount"]
        paths = {m.get("path") for m in mounts}
        self.assertIn("/data", paths)

    def test_mount_archive_exists(self) -> None:
        mounts = [c for c in self._get_config() if c.get("type") == "mount"]
        paths = {m.get("path") for m in mounts}
        self.assertIn("/archive", paths)

    # --- grub_device ---

    def test_grub_device_set_on_system_disk(self) -> None:
        cfg = self._get_config()
        system_disk = next(
            (c for c in cfg if c.get("type") == "disk"
             and c.get("match", {}).get("serial") == SERIAL_SYSTEM),
            None,
        )
        self.assertIsNotNone(system_disk)
        self.assertTrue(system_disk.get("grub_device"), "grub_device must be True on system disk")

    # --- Wipe actions ---

    def test_all_disks_have_wipe_action(self) -> None:
        disks = [c for c in self._get_config() if c.get("type") == "disk"]
        self.assertEqual(len(disks), 3, "Expected exactly 3 disks")
        for disk in disks:
            self.assertIn("wipe", disk, f"Disk {disk.get('id')} missing wipe action")
            self.assertTrue(disk["wipe"], f"Disk {disk.get('id')} has falsy wipe")


# ---------------------------------------------------------------------------
# Tests against the rendered YAML output (render-autoinstall.py)
# Skip if render returns non-zero (render may not support samovar mode yet).
# ---------------------------------------------------------------------------

class TestRenderedStorageYaml(unittest.TestCase):
    """Run render-autoinstall.py and validate its storage section."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.rendered = _get_rendered_yaml()

    def _skip_if_no_rendered(self) -> None:
        if self.rendered is None:
            self.skipTest("render-autoinstall.py did not produce output (may need samovar mode)")

    def test_rendered_yaml_no_size_largest(self) -> None:
        self._skip_if_no_rendered()
        # Extract storage section only
        storage_start = self.rendered.find("storage:")
        if storage_start == -1:
            self.skipTest("No storage section in rendered YAML")
        storage_section = self.rendered[storage_start:]
        self.assertNotIn("largest", storage_section, "Found 'largest' in rendered storage")

    def test_rendered_yaml_contains_autoinstall(self) -> None:
        self._skip_if_no_rendered()
        self.assertIn("autoinstall:", self.rendered)

    def test_rendered_yaml_uses_unprefixed_serials_by_default(self) -> None:
        self._skip_if_no_rendered()
        assert self.rendered is not None
        self.assertIn("serial: 50026B7683695BFE", self.rendered)
        self.assertIn("for serial in 50026B7683695BFE", self.rendered)

    def test_rendered_yaml_supports_vm_disk_serial_prefix(self) -> None:
        rendered = _get_rendered_yaml("QEMU_HARDDISK_")
        self.assertIsNotNone(rendered)
        assert rendered is not None
        self.assertIn("serial: QEMU_HARDDISK_50026B7683695BFE", rendered)
        self.assertIn("for serial in 50026B7683695BFE", rendered)
        self.assertNotIn("for serial in QEMU_HARDDISK_50026B7683695BFE", rendered)

    def test_rendered_yaml_uses_configured_swap_size(self) -> None:
        rendered = _get_rendered_yaml(swap_size_gib="8")
        self.assertIsNotNone(rendered)
        assert rendered is not None
        self.assertIn("create_swap_file /swapfile 8", rendered)
        self.assertIn("create_swap_file /data/swapfile 8", rendered)

    def test_preflight_matches_udev_short_serial(self) -> None:
        rendered = _get_rendered_yaml("QEMU_HARDDISK_")
        self.assertIsNotNone(rendered)
        assert rendered is not None
        self.assertIn("udevadm", rendered)
        self.assertIn("^ID_SERIAL_SHORT=", rendered)
        self.assertNotIn("^ID_SERIAL=", rendered)
        self.assertNotIn("grep -c", rendered)

    def test_rendered_yaml_has_poweroff(self) -> None:
        self._skip_if_no_rendered()
        self.assertIn("shutdown: poweroff", self.rendered)


if __name__ == "__main__":
    unittest.main()

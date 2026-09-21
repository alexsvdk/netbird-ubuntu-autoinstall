#!/usr/bin/env python3
"""Synthetic ISO validation tests.

Tests validate-autoinstall-iso.py on both Generic and Samovar configurations,
as well as GRUB kernel command line parameter and timeout validation.
"""

from __future__ import annotations

import copy
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
VALIDATE = ROOT / "validate-autoinstall-iso.py"
RENDER = ROOT / "render-autoinstall.py"
PATCH_GRUB = ROOT / "patch-grub.py"

SAMPLE_VALID_GRUB = """\
set timeout=3

menuentry 'Try or Install Ubuntu Server' {
\tset gfxpayload=keep
\tlinux\t/casper/vmlinuz autoinstall fsck.mode=skip ---
\tinitrd\t/casper/initrd
}
"""


class TestGrubValidation(unittest.TestCase):
    """Test GRUB boot configuration validation in validate-autoinstall-iso.py."""

    def test_valid_grub_passes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            grub_path = Path(td) / "grub.cfg"
            grub_path.write_text(SAMPLE_VALID_GRUB, encoding="utf-8")
            # We also need a dummy valid autoinstall.yaml for the CLI
            yaml_path = Path(td) / "autoinstall.yaml"
            yaml_path.write_text(self._generic_yaml(), encoding="utf-8")

            res = subprocess.run(
                [sys.executable, str(VALIDATE), str(yaml_path), str(grub_path)],
                capture_output=True,
                text=True,
            )
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertIn("Validation passed", res.stdout)

    def test_missing_autoinstall_flag_fails(self) -> None:
        bad_grub = SAMPLE_VALID_GRUB.replace("autoinstall ", "")
        with tempfile.TemporaryDirectory() as td:
            grub_path = Path(td) / "grub.cfg"
            grub_path.write_text(bad_grub, encoding="utf-8")
            yaml_path = Path(td) / "autoinstall.yaml"
            yaml_path.write_text(self._generic_yaml(), encoding="utf-8")

            res = subprocess.run(
                [sys.executable, str(VALIDATE), str(yaml_path), str(grub_path)],
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(res.returncode, 0)
            self.assertIn("autoinstall", res.stderr)

    def test_missing_fsck_skip_fails(self) -> None:
        bad_grub = SAMPLE_VALID_GRUB.replace("fsck.mode=skip ", "")
        with tempfile.TemporaryDirectory() as td:
            grub_path = Path(td) / "grub.cfg"
            grub_path.write_text(bad_grub, encoding="utf-8")
            yaml_path = Path(td) / "autoinstall.yaml"
            yaml_path.write_text(self._generic_yaml(), encoding="utf-8")

            res = subprocess.run(
                [sys.executable, str(VALIDATE), str(yaml_path), str(grub_path)],
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(res.returncode, 0)
            self.assertIn("fsck.mode=skip", res.stderr)

    def test_wrong_timeout_fails(self) -> None:
        bad_grub = SAMPLE_VALID_GRUB.replace("set timeout=3", "set timeout=30")
        with tempfile.TemporaryDirectory() as td:
            grub_path = Path(td) / "grub.cfg"
            grub_path.write_text(bad_grub, encoding="utf-8")
            yaml_path = Path(td) / "autoinstall.yaml"
            yaml_path.write_text(self._generic_yaml(), encoding="utf-8")

            res = subprocess.run(
                [sys.executable, str(VALIDATE), str(yaml_path), str(grub_path)],
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(res.returncode, 0)
            self.assertIn("timeout", res.stderr)

    def test_main_and_loopback_grub_configs_pass(self) -> None:
        loopback = SAMPLE_VALID_GRUB.replace("set timeout=3\n\n", "")
        with tempfile.TemporaryDirectory() as td:
            yaml_path = Path(td) / "autoinstall.yaml"
            yaml_path.write_text(self._generic_yaml(), encoding="utf-8")
            main_path = Path(td) / "grub.cfg"
            main_path.write_text(SAMPLE_VALID_GRUB, encoding="utf-8")
            loopback_path = Path(td) / "loopback.cfg"
            loopback_path.write_text(loopback, encoding="utf-8")
            res = subprocess.run(
                [sys.executable, str(VALIDATE), str(yaml_path), str(main_path), str(loopback_path)],
                capture_output=True,
                text=True,
            )
            self.assertEqual(res.returncode, 0, res.stderr)

    def _generic_yaml(self) -> str:
        env = os.environ.copy()
        env.update(
            {
                "HOSTNAME": "test-generic",
                "USERNAME": "server",
                "PASSWORD_HASH": "$6$rounds=4096$salt$hash",
                "SSH_PUBLIC_KEY": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIValidKey test@example",
                "NETBIRD_SETUP_KEY": "setup-key-12345",
                "ARCH": "amd64",
                "APT_REGION": "auto",
                "SAMOVAR_MODE": "generic",
            }
        )
        res = subprocess.run(
            [sys.executable, str(RENDER)],
            capture_output=True,
            text=True,
            env=env,
            cwd=str(ROOT),
        )
        return res.stdout


class TestSamovarIsoValidation(unittest.TestCase):
    """Test validation of rendered Samovar autoinstall configuration."""

    @classmethod
    def setUpClass(cls) -> None:
        env = os.environ.copy()
        env.update(
            {
                "SAMOVAR_MODE": "samovar",
                "HOSTNAME": "samovar",
                "USERNAME": "alex",
                "PASSWORD_HASH": "$6$rounds=4096$salt$hash",
                "SSH_PUBLIC_KEYS": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIValidKey alex@samovar",
                "SSH_PUBLIC_KEY": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIValidKey alex@samovar",
                "ARCH": "amd64",
                "APT_REGION": "auto",
                "ALLOWED_SIGNERS": "alex@samovar namespaces=\"samovar-recovery\" ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIValidKey",
            }
        )
        res = subprocess.run(
            [sys.executable, str(RENDER)],
            capture_output=True,
            text=True,
            env=env,
            cwd=str(ROOT),
        )
        if res.returncode != 0:
            raise RuntimeError(f"render-autoinstall failed: {res.stderr}")
        cls.samovar_yaml = res.stdout

    def test_rendered_samovar_passes_validation(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            yaml_path = Path(td) / "autoinstall.yaml"
            yaml_path.write_text(self.samovar_yaml, encoding="utf-8")
            grub_path = Path(td) / "grub.cfg"
            grub_path.write_text(SAMPLE_VALID_GRUB, encoding="utf-8")

            res = subprocess.run(
                [sys.executable, str(VALIDATE), str(yaml_path), str(grub_path)],
                capture_output=True,
                text=True,
            )
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertIn("Validation passed", res.stdout)

    def _render_samovar_with_interface(self, interface: str) -> tuple[subprocess.CompletedProcess[str], dict]:
        env = os.environ.copy()
        env.update(
            {
                "SAMOVAR_MODE": "samovar",
                "NETWORK_INTERFACE": interface,
                "HOSTNAME": "samovar",
                "USERNAME": "alex",
                "PASSWORD_HASH": "$6$rounds=4096$salt$hash",
                "SSH_PUBLIC_KEYS": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIValidKey alex@samovar",
                "ARCH": "amd64",
                "APT_REGION": "auto",
            }
        )
        res = subprocess.run(
            [sys.executable, str(RENDER)],
            capture_output=True,
            text=True,
            env=env,
            cwd=str(ROOT),
        )
        return res, yaml.safe_load(res.stdout) if res.returncode == 0 else {}

    def test_lan0_profile_omits_wifi_configuration(self) -> None:
        res, document = self._render_samovar_with_interface("lan0")
        self.assertEqual(res.returncode, 0, res.stderr)
        network = document["autoinstall"]["network"]
        self.assertIn("lan0", network["ethernets"])
        self.assertNotIn("wifis", network)
        files = document["autoinstall"]["user-data"]["write_files"]
        self.assertNotIn("/etc/systemd/network/10-wifi0.link", {item["path"] for item in files})
        provision = next(item["content"] for item in files if item["path"] == "/usr/local/sbin/samovar-provision.sh")
        self.assertIn("ufw allow in on lan0", provision)
        self.assertNotIn("ufw allow in on wifi0", provision)

    def test_invalid_network_interface_rejected(self) -> None:
        res, _ = self._render_samovar_with_interface("ens4")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("NETWORK_INTERFACE", res.stderr)

    def test_size_largest_in_samovar_storage_rejected(self) -> None:
        doc = yaml.safe_load(self.samovar_yaml)
        doc["autoinstall"]["storage"]["layout"] = {"name": "direct", "match": {"size": "largest"}}
        with tempfile.TemporaryDirectory() as td:
            yaml_path = Path(td) / "autoinstall.yaml"
            yaml_path.write_text(yaml.dump(doc), encoding="utf-8")
            grub_path = Path(td) / "grub.cfg"
            grub_path.write_text(SAMPLE_VALID_GRUB, encoding="utf-8")

            res = subprocess.run(
                [sys.executable, str(VALIDATE), str(yaml_path), str(grub_path)],
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(res.returncode, 0)
            self.assertIn("largest", res.stderr)

    def test_missing_disk_serial_rejected(self) -> None:
        # Remove SBSSD data serial
        bad_yaml = self.samovar_yaml.replace("TD2023102401304", "WRONG_SERIAL_123")
        with tempfile.TemporaryDirectory() as td:
            yaml_path = Path(td) / "autoinstall.yaml"
            yaml_path.write_text(bad_yaml, encoding="utf-8")
            grub_path = Path(td) / "grub.cfg"
            grub_path.write_text(SAMPLE_VALID_GRUB, encoding="utf-8")

            res = subprocess.run(
                [sys.executable, str(VALIDATE), str(yaml_path), str(grub_path)],
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(res.returncode, 0)
            self.assertIn("TD2023102401304", res.stderr)

    def test_setup_key_exposure_in_samovar_rejected(self) -> None:
        bad_yaml = self.samovar_yaml + "\n# netbird up --setup-key secret-in-clear\n"
        doc = yaml.safe_load(self.samovar_yaml)
        doc["autoinstall"]["user-data"]["write_files"].append({
            "path": "/etc/insecure.sh",
            "owner": "root:root",
            "permissions": "0700",
            "content": "netbird up --setup-key leakedkey123\n",
        })
        with tempfile.TemporaryDirectory() as td:
            yaml_path = Path(td) / "autoinstall.yaml"
            yaml_path.write_text(yaml.dump(doc), encoding="utf-8")
            grub_path = Path(td) / "grub.cfg"
            grub_path.write_text(SAMPLE_VALID_GRUB, encoding="utf-8")

            res = subprocess.run(
                [sys.executable, str(VALIDATE), str(yaml_path), str(grub_path)],
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(res.returncode, 0)
            self.assertIn("Plaintext setup key", res.stderr)

    def test_missing_lan0_interface_rejected(self) -> None:
        doc = yaml.safe_load(self.samovar_yaml)
        del doc["autoinstall"]["network"]["ethernets"]["lan0"]
        with tempfile.TemporaryDirectory() as td:
            yaml_path = Path(td) / "autoinstall.yaml"
            yaml_path.write_text(yaml.dump(doc), encoding="utf-8")
            grub_path = Path(td) / "grub.cfg"
            grub_path.write_text(SAMPLE_VALID_GRUB, encoding="utf-8")

            res = subprocess.run(
                [sys.executable, str(VALIDATE), str(yaml_path), str(grub_path)],
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(res.returncode, 0)
            self.assertIn("lan0", res.stderr)

    def test_missing_wifi0_interface_rejected(self) -> None:
        doc = yaml.safe_load(self.samovar_yaml)
        del doc["autoinstall"]["network"]["wifis"]["wifi0"]
        with tempfile.TemporaryDirectory() as td:
            yaml_path = Path(td) / "autoinstall.yaml"
            yaml_path.write_text(yaml.dump(doc), encoding="utf-8")
            grub_path = Path(td) / "grub.cfg"
            grub_path.write_text(SAMPLE_VALID_GRUB, encoding="utf-8")

            res = subprocess.run(
                [sys.executable, str(VALIDATE), str(yaml_path), str(grub_path)],
                capture_output=True,
                text=True,
                cwd=str(ROOT),
            )
            self.assertNotEqual(res.returncode, 0)
            self.assertIn("wifi0", res.stderr)


class TestSshKeyValidation(unittest.TestCase):
    """Test validation of various OpenSSH public key types (ssh-, ecdsa-, sk-)."""

    def _base_env(self, mode: str = "samovar") -> dict[str, str]:
        env = os.environ.copy()
        env.update(
            {
                "SAMOVAR_MODE": mode,
                "HOSTNAME": "samovar" if mode == "samovar" else "test-generic",
                "USERNAME": "alex" if mode == "samovar" else "server",
                "PASSWORD_HASH": "$6$rounds=4096$salt$hash",
                "ARCH": "amd64",
                "APT_REGION": "auto",
            }
        )
        if mode == "generic":
            env["NETBIRD_SETUP_KEY"] = "setup-key-test-12345"
        return env

    def test_ecdsa_key_accepted_samovar_mode(self) -> None:
        env = self._base_env("samovar")
        ecdsa_key = "ecdsa-sha2-nistp256 AAAAE2VjZHNhLXNoYTItnistp256AAAAIb... user@host"
        env["SSH_PUBLIC_KEYS"] = ecdsa_key
        res = subprocess.run(
            [sys.executable, str(RENDER)],
            capture_output=True,
            text=True,
            env=env,
            cwd=str(ROOT),
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        doc = yaml.safe_load(res.stdout)
        self.assertIn(ecdsa_key, doc["autoinstall"]["ssh"]["authorized-keys"])

    def test_ecdsa_key_accepted_generic_mode(self) -> None:
        env = self._base_env("generic")
        ecdsa_key = "ecdsa-sha2-nistp256 AAAAE2VjZHNhLXNoYTItnistp256AAAAIb... user@host"
        env["SSH_PUBLIC_KEY"] = ecdsa_key
        res = subprocess.run(
            [sys.executable, str(RENDER)],
            capture_output=True,
            text=True,
            env=env,
            cwd=str(ROOT),
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        doc = yaml.safe_load(res.stdout)
        self.assertIn(ecdsa_key, doc["autoinstall"]["ssh"]["authorized-keys"])

    def test_multiple_keys_including_ecdsa_and_sk(self) -> None:
        env = self._base_env("samovar")
        k1 = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIValidKey alex@samovar"
        k2 = "ecdsa-sha2-nistp384 AAAAE2VjZHNhLXNoYTItnistp384AAAA... alex@work"
        k3 = "sk-ssh-ed25519@openssh.com AAAAGnNrLXNzaC1lZDI1NTE5... alex@fido"
        env["SSH_PUBLIC_KEYS"] = f"{k1}\n{k2},{k3}"
        res = subprocess.run(
            [sys.executable, str(RENDER)],
            capture_output=True,
            text=True,
            env=env,
            cwd=str(ROOT),
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        doc = yaml.safe_load(res.stdout)
        keys = doc["autoinstall"]["ssh"]["authorized-keys"]
        self.assertEqual(len(keys), 3)
        self.assertIn(k1, keys)
        self.assertIn(k2, keys)
        self.assertIn(k3, keys)

    def test_invalid_ssh_key_rejected_samovar_mode(self) -> None:
        env = self._base_env("samovar")
        env["SSH_PUBLIC_KEYS"] = "invalid-key-type AAAA... user@host"
        res = subprocess.run(
            [sys.executable, str(RENDER)],
            capture_output=True,
            text=True,
            env=env,
            cwd=str(ROOT),
        )
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("valid OpenSSH key type", res.stderr)

    def test_invalid_ssh_key_rejected_generic_mode(self) -> None:
        env = self._base_env("generic")
        env["SSH_PUBLIC_KEY"] = "invalid-key-type AAAA... user@host"
        res = subprocess.run(
            [sys.executable, str(RENDER)],
            capture_output=True,
            text=True,
            env=env,
            cwd=str(ROOT),
        )
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("valid OpenSSH key type", res.stderr)


if __name__ == "__main__":
    unittest.main()

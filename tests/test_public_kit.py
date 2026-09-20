#!/usr/bin/env python3
"""Drive the shipped builder scripts on the real entry points (no reimplementation)."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATCH_GRUB = ROOT / "patch-grub.py"
RENDER = ROOT / "render-autoinstall.py"
VALIDATE = ROOT / "validate-autoinstall-iso.py"
BUILD_SH = ROOT / "build-autoinstall-iso.sh"

SAMPLE_GRUB = """\
set timeout=30

menuentry 'Try or Install Ubuntu Server' {
	set gfxpayload=keep
	linux	/casper/vmlinuz  ---
	initrd	/casper/initrd
}
"""


class PublicKitTests(unittest.TestCase):
    def test_required_public_files_exist(self) -> None:
        for rel in (
            "LICENSE",
            "README.md",
            "SECURITY.md",
            "CONTRIBUTING.md",
            ".env.example",
            ".gitignore",
            "build-autoinstall-iso.sh",
            "build-autoinstall-iso.ps1",
            "build-autoinstall-iso.bat",
            "render-autoinstall.py",
            "patch-grub.py",
            "validate-autoinstall-iso.py",
        ):
            path = ROOT / rel
            self.assertTrue(path.is_file(), f"missing public file: {rel}")

    def test_env_example_secret_fields_empty(self) -> None:
        text = (ROOT / ".env.example").read_text(encoding="utf-8")
        for key in ("PASSWORD", "SSH_PUBLIC_KEY", "NETBIRD_SETUP_KEY"):
            matched = False
            for line in text.splitlines():
                if line.startswith(f"{key}="):
                    matched = True
                    self.assertEqual(
                        line.split("=", 1)[1],
                        "",
                        f"{key} in .env.example must be empty",
                    )
            self.assertTrue(matched, f"{key}= missing from .env.example")

    def test_gitignore_covers_secrets_and_artifacts(self) -> None:
        ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
        for pattern in (".env", "*.iso", "autoinstall.yaml", "__pycache__"):
            self.assertIn(pattern, ignore, f".gitignore must mention {pattern}")

    def test_patch_grub_real_script(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "grub.cfg"
            dst = Path(tmp) / "grub-patched.cfg"
            src.write_text(SAMPLE_GRUB, encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(PATCH_GRUB), str(src), str(dst)],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
            patched = dst.read_text(encoding="utf-8")
            self.assertIn("autoinstall", patched)
            self.assertIn("fsck.mode=skip", patched)
            self.assertRegex(patched, r"(?m)^\s*set\s+timeout\s*=\s*3\s*$")

    def test_render_and_validate_real_scripts(self) -> None:
        env = os.environ.copy()
        env.update(
            {
                "HOSTNAME": "test-server",
                "USERNAME": "server",
                # openssl-style hash placeholder; not a real password
                "PASSWORD_HASH": "$6$rounds=4096$testsalt$testhashvalueforunittestonly",
                "SSH_PUBLIC_KEY": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITestPublicKeyMaterialOnlyNotReal test@example",
                "NETBIRD_SETUP_KEY": "test-setup-key-not-real-00000000-0000-0000-0000-000000000000",
                "ARCH": "amd64",
                "APT_REGION": "auto",
                "SAMOVAR_MODE": "generic",
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            yaml_path = tmp_path / "autoinstall.yaml"
            grub_src = tmp_path / "grub.cfg"
            grub_dst = tmp_path / "grub-patched.cfg"
            grub_src.write_text(SAMPLE_GRUB, encoding="utf-8")

            render = subprocess.run(
                [sys.executable, str(RENDER)],
                check=False,
                capture_output=True,
                text=True,
                env=env,
                cwd=str(ROOT),
            )
            self.assertEqual(render.returncode, 0, render.stderr or render.stdout)
            self.assertIn("autoinstall:", render.stdout)
            self.assertIn("shutdown: poweroff", render.stdout)
            self.assertIn("netbird up --setup-key", render.stdout)
            yaml_path.write_text(render.stdout, encoding="utf-8")

            patch = subprocess.run(
                [sys.executable, str(PATCH_GRUB), str(grub_src), str(grub_dst)],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(patch.returncode, 0, patch.stderr or patch.stdout)

            validate = subprocess.run(
                [sys.executable, str(VALIDATE), str(yaml_path), str(grub_dst)],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(validate.returncode, 0, validate.stderr or validate.stdout)
            self.assertIn("Validation passed", validate.stdout)

    def test_build_script_bash_syntax(self) -> None:
        result = subprocess.run(
            ["bash", "-n", str(BUILD_SH)],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_build_script_powershell_syntax(self) -> None:
        ps1 = ROOT / "build-autoinstall-iso.ps1"
        self.assertTrue(ps1.is_file())
        bat = ROOT / "build-autoinstall-iso.bat"
        self.assertTrue(bat.is_file())

        ps_cmd = None
        for cmd in ("pwsh", "powershell"):
            try:
                subprocess.run([cmd, "-v"], check=True, capture_output=True)
                ps_cmd = cmd
                break
            except (FileNotFoundError, subprocess.CalledProcessError):
                continue

        if ps_cmd:
            check_script = (
                f"$errs = @(); [System.Management.Automation.Language.Parser]::ParseFile('{ps1}', [ref]$null, [ref]$errs); "
                "if ($errs.Count -gt 0) { exit 1 } else { exit 0 }"
            )
            result = subprocess.run(
                [ps_cmd, "-Command", check_script],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)


if __name__ == "__main__":
    unittest.main()

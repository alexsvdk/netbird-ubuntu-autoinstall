#!/usr/bin/env python3
"""Drive the shipped builder scripts on the real entry points (no reimplementation)."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

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

    def test_samovar_provision_installs_netbird_before_bootstrap(self) -> None:
        env = os.environ.copy()
        env.update(
            {
                "HOSTNAME": "samovar",
                "USERNAME": "server",
                "PASSWORD_HASH": "$6$rounds=4096$testsalt$testhashvalueforunittestonly",
                "SSH_PUBLIC_KEYS": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITestPublicKeyMaterialOnlyNotReal test@example",
                "SAMOVAR_MODE": "samovar",
                "SAMOVAR_CONFIG_FILE": "/does-not-exist/samovar-config.json",
                "ARCH": "amd64",
                "APT_REGION": "auto",
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run(
                [sys.executable, str(RENDER)],
                check=False,
                capture_output=True,
                text=True,
                env=env,
                cwd=tmp,
            )

        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        document = yaml.safe_load(result.stdout)
        early_commands = document["autoinstall"]["early-commands"]
        preflight_script = next(
            command[2] for command in early_commands if "PREFLIGHT_EOF" in command[2]
        )
        self.assertIn("exit 0", preflight_script)
        self.assertIn("/run/samovar-preflight.log", early_commands[2][2])
        files = document["autoinstall"]["user-data"]["write_files"]
        provision = next(
            entry["content"]
            for entry in files
            if entry["path"] == "/usr/local/sbin/samovar-provision.sh"
        )
        self.assertIn("https://pkgs.netbird.io/install.sh | sh", provision)
        self.assertIn("command -v netbird", provision)
        self.assertIn("install -d -m 0755 /etc/mihomo /var/lib/mihomo", provision)
        self.assertIn("meta-rules-dat/releases/latest/download/geoip.metadb", provision)
        self.assertNotIn("/usr/local/bin/mihomo", provision)
        self.assertIn("systemctl enable mihomo.service", provision)
        self.assertLess(
            provision.index("installing NetBird"),
            provision.index("systemctl enable --now docker.service"),
        )

        files_by_path = {entry["path"]: entry["content"] for entry in files}
        provision_service = files_by_path["/etc/systemd/system/samovar-provision.service"]
        self.assertIn("Type=oneshot", provision_service)
        self.assertIn("RemainAfterExit=yes", provision_service)
        mihomo_service = files_by_path["/etc/systemd/system/mihomo.service"]
        self.assertIn("ExecStart=/usr/bin/docker compose", mihomo_service)
        self.assertNotIn("ExecStart=/usr/local/bin/mihomo", mihomo_service)
        mihomo_compose = files_by_path["/etc/mihomo/compose.yml"]
        self.assertIn("metacubex/mihomo:latest", mihomo_compose)
        self.assertIn('entrypoint: ["/mihomo"]', mihomo_compose)
        self.assertNotIn("network_mode: host", mihomo_compose)
        self.assertIn('"127.0.0.1:7890:7890"', mihomo_compose)
        self.assertIn('"127.0.0.1:9090:9090"', mihomo_compose)
        self.assertIn("name: samovar-mihomo", mihomo_compose)
        self.assertIn("cap_drop:", mihomo_compose)
        self.assertEqual(
            files_by_path["/etc/mihomo/compose.env"],
            "MIHOMO_IMAGE=metacubex/mihomo:latest\n",
        )
        self.assertIn(
            ["systemctl", "enable", "--now", "samovar-provision.service"],
            document["autoinstall"]["user-data"]["runcmd"],
        )
        self.assertIn(
            ["systemctl", "enable", "--now", "samovar-recovery-bootstrap.service"],
            document["autoinstall"]["user-data"]["runcmd"],
        )
        self.assertIn("Environment=HOME=/root", files_by_path["/etc/systemd/system/samovar-recovery-bootstrap.service"])
        recovery_scan = files_by_path["/etc/systemd/system/samovar-recovery-scan.service"]
        self.assertIn("ExecStart=/usr/local/sbin/samovar-recovery-agent.py --scan-usb", recovery_scan)
        self.assertIn("ExecCondition=/usr/bin/test ! -e /var/lib/samovar-recovery/bootstrap-inbox/samovar-config.json", recovery_scan)
        self.assertNotIn(" --scan\n", recovery_scan)

    def test_notify_topic_default_and_custom_values_reach_yaml(self) -> None:
        base_env = os.environ.copy()
        base_env.update(
            {
                "HOSTNAME": "test-server",
                "USERNAME": "server",
                "PASSWORD_HASH": "$6$rounds=4096$testsalt$testhashvalueforunittestonly",
                "SSH_PUBLIC_KEY": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITestPublicKeyMaterialOnlyNotReal test@example",
                "NETBIRD_SETUP_KEY": "test-setup-key",
                "ARCH": "amd64",
                "APT_REGION": "auto",
                "SAMOVAR_MODE": "generic",
            }
        )
        base_env.pop("NOTIFY_TOPIC", None)

        for topic in ("samovar_test", "custom_install_topic"):
            env = base_env.copy()
            if topic != "samovar_test":
                env["NOTIFY_TOPIC"] = topic
            result = subprocess.run(
                [sys.executable, str(RENDER)],
                check=False,
                capture_output=True,
                text=True,
                env=env,
                cwd=str(ROOT),
            )
            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
            document = yaml.safe_load(result.stdout)["autoinstall"]
            files = document["user-data"]["write_files"]
            notifier = next(entry["content"] for entry in files if entry["path"] == "/usr/local/sbin/samovar-notify")
            self.assertIn(f"https://ntfy.sh/{topic}", notifier)
            self.assertIn("--proxy http://127.0.0.1:7890", notifier)
            self.assertIn("Установщик запущен", document["early-commands"][0][2])
            if len(document["early-commands"]) > 2:
                preflight_script = next(
                    command[2]
                    for command in document["early-commands"]
                    if "PREFLIGHT_EOF" in command[2]
                )
                self.assertIn("exit 0", preflight_script)
                self.assertIn("/run/samovar-preflight.log", document["early-commands"][2][2])

    def test_mihomo_image_default_and_custom_values_reach_yaml(self) -> None:
        base_env = os.environ.copy()
        base_env.update(
            {
                "HOSTNAME": "test-server",
                "USERNAME": "server",
                "PASSWORD_HASH": "$6$rounds=4096$testsalt$testhashvalueforunittestonly",
                "SSH_PUBLIC_KEY": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITestPublicKeyMaterialOnlyNotReal test@example",
                "ARCH": "amd64",
                "APT_REGION": "auto",
                "SAMOVAR_MODE": "samovar",
                "SAMOVAR_CONFIG_FILE": "/does-not-exist/samovar-config.json",
            }
        )
        for image in ("metacubex/mihomo:latest", "registry.example/mihomo:v2"):
            env = base_env.copy()
            if image == "metacubex/mihomo:latest":
                env.pop("MIHOMO_IMAGE", None)
            else:
                env["MIHOMO_IMAGE"] = image
            result = subprocess.run(
                [sys.executable, str(RENDER)],
                check=False,
                capture_output=True,
                text=True,
                env=env,
                cwd=str(ROOT),
            )
            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
            document = yaml.safe_load(result.stdout)["autoinstall"]
            files = {entry["path"]: entry["content"] for entry in document["user-data"]["write_files"]}
            self.assertEqual(files["/etc/mihomo/compose.env"], f"MIHOMO_IMAGE={image}\n")

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

    def test_mihomo_image_is_forwarded_by_build_entry_points(self) -> None:
        for rel in ("build-autoinstall-iso.sh", "build-autoinstall-iso.ps1", "build-autoinstall-iso.bat"):
            self.assertIn("MIHOMO_IMAGE", (ROOT / rel).read_text(encoding="utf-8"))

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

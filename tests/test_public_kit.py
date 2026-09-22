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
        self.assertIn("tee /run/samovar-preflight.log", preflight_script)
        self.assertEqual(early_commands[2][2], "chmod +x /run/samovar-preflight.sh && bash /run/samovar-preflight.sh")
        files = document["autoinstall"]["user-data"]["write_files"]
        provision = next(
            entry["content"]
            for entry in files
            if entry["path"] == "/usr/local/sbin/samovar-provision.sh"
        )
        self.assertIn("netbird libnvidia-container1", provision)
        self.assertIn("command -v netbird", provision)
        self.assertIn("nvidia-ctk runtime configure --runtime=docker", provision)
        self.assertIn("nvtop", provision)
        self.assertIn("mountpoint -q /data", provision)
        self.assertIn("install -d -m 0755 /etc/mihomo /var/lib/mihomo", provision)
        self.assertIn("/var/lib/samovar-offline-artifacts/geoip.metadb", provision)
        self.assertIn("meta-rules-dat/releases/latest/download/geoip.metadb", provision)
        self.assertNotIn("/usr/local/bin/mihomo", provision)
        self.assertIn("systemctl enable mihomo.service", provision)
        self.assertLess(
            provision.index("installing NetBird"),
            provision.index("systemctl enable --now docker.service"),
        )

        files_by_path = {entry["path"]: entry["content"] for entry in files}
        mounts_conf = files_by_path["/etc/systemd/system/docker.service.d/mounts.conf"]
        self.assertIn("RequiresMountsFor=/data", mounts_conf)
        self.assertIn("After=data.mount", mounts_conf)
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
                self.assertIn("tee /run/samovar-preflight.log", preflight_script)
                self.assertEqual(document["early-commands"][2][2], "chmod +x /run/samovar-preflight.sh && bash /run/samovar-preflight.sh")

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
            rendered = yaml.safe_load(render.stdout)["autoinstall"]
            files = {entry["path"]: entry["content"] for entry in rendered["user-data"]["write_files"]}
            self.assertIn("netbird up --setup-key", files["/usr/local/sbin/netbird-enroll.sh"])
            self.assertIn("/cdrom/samovar-offline-apt/", rendered["late-commands"][0])
            self.assertIn("offline_apt_install()", files["/usr/local/sbin/netbird-enroll.sh"])
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

    def test_offline_bundle_is_present_and_forwarded_by_build_entry_points(self) -> None:
        helper = ROOT / "offline" / "build-apt-bundle.sh"
        artifact_helper = ROOT / "offline" / "build-oci-artifacts.sh"
        self.assertTrue(helper.is_file())
        self.assertTrue(artifact_helper.is_file())
        syntax = subprocess.run(["bash", "-n", str(helper)], capture_output=True, text=True)
        self.assertEqual(syntax.returncode, 0, syntax.stderr)
        artifact_syntax = subprocess.run(["bash", "-n", str(artifact_helper)], capture_output=True, text=True)
        self.assertEqual(artifact_syntax.returncode, 0, artifact_syntax.stderr)
        seeds = (ROOT / "offline" / "packages.seeds.json").read_text(encoding="utf-8")
        self.assertIn('"codename": "resolute"', seeds)
        self.assertIn("linux-firmware", seeds)
        self.assertIn("wireless-regdb", seeds)
        self.assertIn("nvidia-container-toolkit", seeds)
        lock = (ROOT / "offline" / "packages.lock.json").read_text(encoding="utf-8")
        self.assertIn('"state": "ungenerated"', lock)
        image_lock = (ROOT / "offline" / "images.lock.json").read_text(encoding="utf-8")
        self.assertIn('"state": "ungenerated"', image_lock)
        for rel in ("build-autoinstall-iso.sh", "build-autoinstall-iso.ps1", "build-autoinstall-iso.bat"):
            text = (ROOT / rel).read_text(encoding="utf-8")
            self.assertIn("OFFLINE_BUNDLE", text, rel)
            self.assertIn("OFFLINE_ARTIFACT", text, rel)
        shell = (ROOT / "build-autoinstall-iso.sh").read_text(encoding="utf-8")
        self.assertIn("-e OFFLINE_BUNDLE_CACHE=\"$OFFLINE_BUNDLE_CACHE\"", shell)
        self.assertIn("-e OFFLINE_BUNDLE_REFRESH=\"$OFFLINE_BUNDLE_REFRESH\"", shell)
        self.assertIn("/samovar-offline-artifacts/mihomo-image.tar", shell)

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

    def test_ubuntu_iso_sha256_is_forwarded_by_build_entry_points(self) -> None:
        for rel in (
            "build-autoinstall-iso.sh",
            "build-autoinstall-iso.ps1",
            "build-autoinstall-iso.bat",
            ".env.example",
        ):
            self.assertIn("UBUNTU_ISO_SHA256", (ROOT / rel).read_text(encoding="utf-8"))

    def test_ubuntu_iso_sha256_default_and_custom_values_reach_yaml(self) -> None:
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
        custom_sha = "cc8a95cde20f6ced61a322420de00f10cc3c90ced545daa46cb9c1a117f1d927"
        for val in ("", custom_sha):
            env = base_env.copy()
            if not val:
                env.pop("UBUNTU_ISO_SHA256", None)
            else:
                env["UBUNTU_ISO_SHA256"] = val
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
            self.assertIn("/etc/samovar-build.env", files)
            self.assertEqual(files["/etc/samovar-build.env"], f"UBUNTU_ISO_SHA256={val}\n")

        # Invalid sha must be rejected
        env = base_env.copy()
        env["UBUNTU_ISO_SHA256"] = "invalid_short_hash"
        result = subprocess.run(
            [sys.executable, str(RENDER)],
            check=False,
            capture_output=True,
            text=True,
            env=env,
            cwd=str(ROOT),
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("UBUNTU_ISO_SHA256 must be a 64-character hex", result.stderr)

    def test_samovar_mode_rejects_arm64(self) -> None:
        env = os.environ.copy()
        env.update(
            {
                "HOSTNAME": "samovar",
                "USERNAME": "alex",
                "PASSWORD_HASH": "$6$rounds=4096$testsalt$testhashvalueforunittestonly",
                "SSH_PUBLIC_KEYS": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITestPublicKeyMaterialOnlyNotReal test@example",
                "ARCH": "arm64",
                "SAMOVAR_MODE": "samovar",
                "SAMOVAR_CONFIG_FILE": "/does-not-exist/samovar-config.json",
            }
        )
        result = subprocess.run(
            [sys.executable, str(RENDER)],
            check=False,
            capture_output=True,
            text=True,
            env=env,
            cwd=str(ROOT),
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Samovar mode requires ARCH=amd64", result.stderr)

    def test_vpn_compose_contains_kill_switch(self) -> None:
        vpn_compose = (ROOT / "provisioning" / "compose-templates" / "vpn.compose.yml").read_text(encoding="utf-8")
        self.assertIn("NET_ADMIN", vpn_compose)
        self.assertIn("iptables -P OUTPUT DROP", vpn_compose)
        self.assertIn("iptables -A OUTPUT -o tun+ -j ACCEPT", vpn_compose)
        self.assertIn("iptables -A OUTPUT -o lo -j ACCEPT", vpn_compose)
        self.assertIn("127.0.0.11", vpn_compose)
        # Fail-closed check when iptables is missing
        self.assertIn("command -v iptables", vpn_compose)
        self.assertIn("Error: iptables is required", vpn_compose)
        # Dynamic upstream proxy resolution from config.yaml
        self.assertIn("server:", vpn_compose)
        self.assertIn("ip route show default", vpn_compose)

    def test_source_iso_verification_fail_closed(self) -> None:
        sh_text = (ROOT / "build-autoinstall-iso.sh").read_text(encoding="utf-8")
        self.assertIn("Error: could not determine expected SHA-256 for $ISO_NAME", sh_text)
        self.assertIn("exit 1", sh_text)

        ps1_text = (ROOT / "build-autoinstall-iso.ps1").read_text(encoding="utf-8")
        self.assertIn("could not determine expected SHA-256 for $IsoName", ps1_text)
        self.assertIn("exit 1", ps1_text)

    def test_offline_refresh_never_configures_local_apt(self) -> None:
        sh_text = (ROOT / "build-autoinstall-iso.sh").read_text(encoding="utf-8")
        self.assertIn("samovar-offline.list", sh_text)
        self.assertIn("file:/work/${OFFLINE_BUNDLE_CACHE}/repository", sh_text)

        ps1_text = (ROOT / "build-autoinstall-iso.ps1").read_text(encoding="utf-8")
        self.assertIn("samovar-offline.list", ps1_text)
        self.assertIn("file:/work/${OFFLINE_BUNDLE_CACHE}/repository", ps1_text)

    def test_powershell_build_includes_geoip(self) -> None:
        ps1_text = (ROOT / "build-autoinstall-iso.ps1").read_text(encoding="utf-8")
        self.assertIn("geoip.metadb", ps1_text)
        self.assertIn("geoip.metadb.sha256", ps1_text)
        self.assertIn('"geoip"', ps1_text)
        self.assertIn("/samovar-offline-artifacts/geoip.metadb", ps1_text)

    def test_packages_seeds_includes_required_build_tools(self) -> None:
        seeds = (ROOT / "offline" / "packages.seeds.json").read_text(encoding="utf-8")
        self.assertIn('"xorriso"', seeds)
        self.assertIn('"python3-jsonschema"', seeds)


if __name__ == "__main__":
    unittest.main()

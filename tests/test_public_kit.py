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
        helper_text = helper.read_text(encoding="utf-8")
        self.assertIn("prune_download_cache", helper_text)
        self.assertLess(helper_text.rindex("verify_locked_repository"), helper_text.rindex("prune_download_cache"))
        artifact_text = artifact_helper.read_text(encoding="utf-8")
        self.assertIn("previous_image_id", artifact_text)
        self.assertIn('docker image rm "$previous_image_id"', artifact_text)

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
        # Fail-closed check when iptables/ip6tables is missing
        self.assertIn("command -v iptables", vpn_compose)
        self.assertIn("Error: iptables is required", vpn_compose)
        self.assertIn("command -v ip6tables", vpn_compose)
        self.assertIn("Error: ip6tables is required", vpn_compose)
        # Shell runs with set -eu for fail-closed firewall execution
        self.assertIn("set -eu", vpn_compose)
        # Sidecar explicitly forbids proxy-providers for kill switch integrity
        self.assertIn("proxy-providers are not supported in VPN sidecar TUN mode", vpn_compose)
        # Healthcheck verifies TUN interface / controller and rejects simple pidof
        self.assertIn("ip link show tun0", vpn_compose)
        self.assertNotIn("pidof mihomo", vpn_compose)
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

    def test_offline_builders_match_target_release(self) -> None:
        import json
        seeds = json.loads((ROOT / "offline" / "packages.seeds.json").read_text(encoding="utf-8"))
        release = seeds["target"]["release"]
        shell = (ROOT / "build-autoinstall-iso.sh").read_text(encoding="utf-8")
        powershell = (ROOT / "build-autoinstall-iso.ps1").read_text(encoding="utf-8")
        self.assertIn(f"ubuntu:{release}", shell)
        self.assertIn(f"ubuntu:{release}", powershell)

    def test_never_mode_does_not_run_apt_get(self) -> None:
        sh = (ROOT / "build-autoinstall-iso.sh").read_text(encoding="utf-8")
        wrapper = sh.split('if [ "$OFFLINE_BUNDLE_REFRESH" != "never" ]; then', 1)[1]
        never_branch = wrapper.split("else", 1)[1].split("fi", 1)[0]
        self.assertNotIn("apt-get", never_branch)

    def test_vpn_compose_escapes_shell_variables_for_compose(self) -> None:
        vpn_compose = (ROOT / "provisioning" / "compose-templates" / "vpn.compose.yml").read_text(encoding="utf-8")
        command_block = vpn_compose.split("command:", 1)[1].split("healthcheck:", 1)[0]
        self.assertIn('if [ -f "$$CONFIG" ]; then', command_block)
        self.assertIn('GW="$$(ip route show default', command_block)
        self.assertIn('if [ -n "$$GW" ]; then', command_block)
        self.assertIn('iptables -A OUTPUT -d "$$GW" -j ACCEPT', command_block)
        self.assertIn('for srv in $$(awk', command_block)
        self.assertIn('line = $$0', command_block)
        self.assertIn('case "$$srv" in', command_block)
        self.assertIn('ips="$$(nslookup "$$srv"', command_block)
        self.assertIn('for ip in $$ips; do', command_block)
        self.assertIn('iptables -A OUTPUT -d "$$ip" -j ACCEPT', command_block)
        self.assertIn('iptables -A OUTPUT -d "$$srv" -j ACCEPT', command_block)
        import re
        unescaped = re.findall(r'(?<!\$)\$(CONFIG|GW|srv|ips|ip)\b', command_block)
        self.assertEqual(unescaped, [])

        import shutil
        if shutil.which("docker") is None:
            self.skipTest("Docker Compose is unavailable")

        result = subprocess.run(
            ["docker", "compose", "-f", str(ROOT / "provisioning" / "compose-templates" / "vpn.compose.yml"), "config"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("variable is not set", result.stderr)

    def test_vpn_compose_proxy_providers_detection_pattern(self) -> None:
        explicit_cases = [
            ("proxy-providers:", True),
            ("  proxy-providers: {}", True),
            ("mihomo: {proxy-providers: {remote: {type: http}}}", True),
            ('{"proxy-providers": {}}', True),
            ("{\"proxy-providers\": {}}", True),
            ('  "proxy-providers"  :', True),
            ("  'proxy-providers'  :", True),
            ("proxy_providers:", True),
            ("  'proxy_providers':", True),
            ('  "proxy providers" :', True),
            ("proxies:", False),
            ("  - name: my-proxy-providers-server\n    server: 1.2.3.4", False),
            ("proxy-providers-allowed: false", False),
        ]
        pattern = r"""['"]?proxy[-_ ]*providers['"]?[[:space:]]*:"""
        for text, should_match in explicit_cases:
            res = subprocess.run(["grep", "-q", "-i", "-E", pattern], input=text, text=True)
            matched = (res.returncode == 0)
            self.assertEqual(matched, should_match, f"Explicit pattern failed for {text}")

        obfuscation_cases = [
            ('"proxy\\u002dproviders":', True),
            ('"proxy\\U0000002dproviders":', True),
            ('"proxy\\x2dproviders":', True),
            ('"proxy\\055providers":', True),
            ("key: &pp proxy-providers\n*pp:", True),
            ("!tag proxy-providers", True),
            ("? proxy-providers", True),
            ("<<: {proxy-providers: {}}", True),
            ("mixed-port: 7890\nproxies:\n  - name: node1\n    server: 1.2.3.4", False),
        ]
        obfuscation_pattern = r"""(\\[uUx0-7]|&[a-zA-Z0-9_-]+|\*[a-zA-Z0-9_-]+|![a-zA-Z0-9_-]+|^[[:space:]]*\?|^[[:space:]]*<<)"""
        for text, should_match in obfuscation_cases:
            res = subprocess.run(["grep", "-q", "-E", obfuscation_pattern], input=text, text=True)
            matched = (res.returncode == 0)
            self.assertEqual(matched, should_match, f"Obfuscation pattern failed for {text}")

    def test_render_vpn_config_from_json(self) -> None:
        import importlib.util
        import json
        import tempfile

        spec = importlib.util.spec_from_file_location(
            "render_vpn_config", ROOT / "provisioning" / "render-vpn-config.py"
        )
        self.assertIsNotNone(spec and spec.loader)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        with tempfile.TemporaryDirectory() as td:
            tpath = Path(td)
            in_file = tpath / "vpn.json"
            out_file = tpath / "vpn.yaml"

            # 1. Valid configuration with UTF-8 non-ASCII name
            valid_cfg = {
                "mode": "rule",
                "proxies": [
                    {
                        "name": "Швеция-01",
                        "type": "ss",
                        "server": "198.51.100.1",
                        "port": 8388,
                        "cipher": "aes-128-gcm",
                        "password": "secret",
                    }
                ],
            }
            in_file.write_text(json.dumps(valid_cfg), encoding="utf-8")
            mod.render_vpn_config(in_file, out_file)
            rendered = out_file.read_text(encoding="utf-8")
            self.assertIn("# Generated by Samovar render-vpn-config.py", rendered)
            self.assertIn('"device": "tun0"', rendered)
            self.assertIn('"server": "198.51.100.1"', rendered)
            self.assertIn("Швеция-01", rendered)
            self.assertNotIn("\\u0428", rendered)

            # 2. Reject explicit proxy-providers
            bad_cfg = {
                "proxy-providers": {"evil": {"type": "http"}},
                "proxies": [{"name": "p", "type": "ss", "server": "1.1.1.1"}],
            }
            in_file.write_text(json.dumps(bad_cfg), encoding="utf-8")
            with self.assertRaises(SystemExit) as ctx:
                mod.render_vpn_config(in_file, out_file)
            self.assertIn("proxy-providers are not supported", str(ctx.exception))

            # 3. Reject unicode-escaped proxy-providers in JSON
            evil_json = '{"proxy\\u002dproviders": {"evil": {}}, "proxies": [{"name": "p", "type": "ss", "server": "1.1.1.1"}]}'
            in_file.write_text(evil_json, encoding="utf-8")
            with self.assertRaises(SystemExit) as ctx:
                mod.render_vpn_config(in_file, out_file)
            self.assertIn("proxy-providers are not supported", str(ctx.exception))

            # 4. Reject empty proxies
            empty_cfg = {"mode": "rule", "proxies": []}
            in_file.write_text(json.dumps(empty_cfg), encoding="utf-8")
            with self.assertRaises(SystemExit) as ctx:
                mod.render_vpn_config(in_file, out_file)
            self.assertIn("non-empty 'proxies' list", str(ctx.exception))

    def test_offline_bundle_schema_3_and_metadata_verification(self) -> None:
        import hashlib
        import json
        import tempfile

        lock = json.loads((ROOT / "offline" / "packages.lock.json").read_text(encoding="utf-8"))
        self.assertEqual(lock.get("schema"), 3)

        build_sh = (ROOT / "offline" / "build-apt-bundle.sh").read_text(encoding="utf-8")
        self.assertIn('if lock.get("schema") != 3 or lock.get("state") != "locked":', build_sh)
        self.assertIn('"schema": 3,', build_sh)

        ps1 = (ROOT / "build-autoinstall-iso.ps1").read_text(encoding="utf-8")
        self.assertIn('$lockJson.schema -ne 3 -or $lockJson.state -ne "locked"', ps1)
        self.assertIn("offline lock metadata is missing or empty", ps1)

        for meta_path in (
            "dists/samovar/InRelease",
            "dists/samovar/Release",
            "samovar-offline-archive-keyring.gpg",
            "Packages.gz",
        ):
            self.assertIn(meta_path, build_sh)
            self.assertIn(meta_path, ps1)

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cache = tmp / "cache"
            repo = cache / "repository"
            dists = repo / "dists/samovar/main/binary-amd64"
            dists.mkdir(parents=True)

            in_rel = repo / "dists/samovar/InRelease"
            in_rel.write_bytes(b"InRelease")
            rel = repo / "dists/samovar/Release"
            rel.write_bytes(b"Release")
            pkg = dists / "Packages"
            pkg.write_bytes(b"Packages")
            pkg_gz = dists / "Packages.gz"
            pkg_gz.write_bytes(b"Packages.gz")
            key = repo / "samovar-offline-archive-keyring.gpg"
            key.write_bytes(b"Keyring")

            deb = repo / "pool/test.deb"
            deb.parent.mkdir(parents=True)
            deb.write_bytes(b"deb")
            deb_hash = hashlib.sha256(b"deb").hexdigest()

            meta = {
                "dists/samovar/InRelease": hashlib.sha256(in_rel.read_bytes()).hexdigest(),
                "dists/samovar/Release": hashlib.sha256(rel.read_bytes()).hexdigest(),
                "dists/samovar/main/binary-amd64/Packages": hashlib.sha256(pkg.read_bytes()).hexdigest(),
                "dists/samovar/main/binary-amd64/Packages.gz": hashlib.sha256(pkg_gz.read_bytes()).hexdigest(),
                "samovar-offline-archive-keyring.gpg": hashlib.sha256(key.read_bytes()).hexdigest(),
            }

            seeds = tmp / "seeds.json"
            seeds.write_text(json.dumps({
                "schema": 2,
                "target": {"release": "26.04", "codename": "resolute", "architecture": "amd64"}
            }))
            lock_file = tmp / "packages.lock.json"

            def run_verify(lock_dict: dict) -> tuple[int, str, str]:
                lock_file.write_text(json.dumps(lock_dict))
                p = subprocess.run(
                    ["bash", str(ROOT / "offline" / "build-apt-bundle.sh"), str(seeds), str(lock_file), str(cache), "never"],
                    capture_output=True,
                    text=True,
                )
                return p.returncode, p.stdout, p.stderr

            # Schema 2 rejected
            rc, _, err = run_verify({"schema": 2, "state": "locked", "packages": [{"path": "pool/test.deb", "sha256": deb_hash}], "metadata": meta})
            self.assertNotEqual(rc, 0)
            self.assertIn("offline lock is not generated", err)

            # Missing metadata rejected
            rc, _, err = run_verify({"schema": 3, "state": "locked", "packages": [{"path": "pool/test.deb", "sha256": deb_hash}]})
            self.assertNotEqual(rc, 0)
            self.assertIn("metadata is missing or empty", err)

            # Missing key rejected
            meta_missing = dict(meta)
            del meta_missing["dists/samovar/Release"]
            rc, _, err = run_verify({"schema": 3, "state": "locked", "packages": [{"path": "pool/test.deb", "sha256": deb_hash}], "metadata": meta_missing})
            self.assertNotEqual(rc, 0)
            self.assertIn("missing", err)

            # Extra key rejected
            meta_extra = dict(meta)
            meta_extra["extra/path"] = "bad"
            rc, _, err = run_verify({"schema": 3, "state": "locked", "packages": [{"path": "pool/test.deb", "sha256": deb_hash}], "metadata": meta_extra})
            self.assertNotEqual(rc, 0)
            self.assertIn("unexpected", err)

            # Corrupted hash rejected
            meta_bad = dict(meta)
            meta_bad["dists/samovar/InRelease"] = "wronghash"
            rc, _, err = run_verify({"schema": 3, "state": "locked", "packages": [{"path": "pool/test.deb", "sha256": deb_hash}], "metadata": meta_bad})
            self.assertNotEqual(rc, 0)
            self.assertIn("metadata SHA-256 mismatch", err)

            # Valid schema 3 succeeds
            rc, out, _ = run_verify({"schema": 3, "state": "locked", "packages": [{"path": "pool/test.deb", "sha256": deb_hash}], "metadata": meta})
            self.assertEqual(rc, 0)
            self.assertIn("Using verified offline APT bundle", out)


if __name__ == "__main__":
    unittest.main()

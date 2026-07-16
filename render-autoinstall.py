#!/usr/bin/env python3
"""Render a validated Ubuntu autoinstall configuration from environment variables."""

import os
import re
import shlex
import sys
from urllib.parse import urlparse

import yaml


def fail(message: str) -> None:
    print(f"Error: {message}", file=sys.stderr)
    raise SystemExit(1)


def required(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        fail(f"{name} is required.")
    return value


hostname = required("HOSTNAME")
username = required("USERNAME")
password_hash = required("PASSWORD_HASH")
ssh_public_key = required("SSH_PUBLIC_KEY")
netbird_setup_key = required("NETBIRD_SETUP_KEY")

apt_region = os.environ.get("APT_REGION", "auto").strip().lower() or "auto"
apt_mirror = os.environ.get("APT_MIRROR", "").strip().rstrip("/")
apt_security_mirror = os.environ.get("APT_SECURITY_MIRROR", "").strip().rstrip("/")
apt_fallback = os.environ.get("APT_FALLBACK", "offline-install").strip() or "offline-install"

arch = os.environ.get("ARCH", "amd64").strip().lower() or "amd64"
arch = {"x86_64": "amd64", "x64": "amd64", "aarch64": "arm64", "arm": "arm64"}.get(arch, arch)
if arch not in {"amd64", "arm64"}:
    fail(f"unsupported ARCH {arch!r}. Use amd64 or arm64.")
if not re.fullmatch(r"[a-z_][a-z0-9_-]*", username):
    fail("USERNAME must contain lowercase letters, digits, underscores, or hyphens.")
if apt_fallback not in {"abort", "offline-install", "continue-anyway"}:
    fail("APT_FALLBACK must be abort, offline-install, or continue-anyway.")
if not re.fullmatch(r"auto|default|archive|none|custom|[a-z]{2}", apt_region):
    fail("APT_REGION must be auto, default, custom, or a two-letter country code.")
if apt_region == "custom" and not apt_mirror:
    fail("APT_REGION=custom requires APT_MIRROR.")
for name, uri in (("APT_MIRROR", apt_mirror), ("APT_SECURITY_MIRROR", apt_security_mirror)):
    if uri and not re.match(r"^https?://", uri, re.IGNORECASE):
        fail(f"{name} must be an http(s) URL.")

if arch == "amd64":
    apt_arches, default_archive = ["amd64", "i386"], "http://archive.ubuntu.com/ubuntu"
else:
    apt_arches, default_archive = ["arm64"], "http://ports.ubuntu.com/ubuntu-ports"
default_archive_host = urlparse(default_archive).hostname
if not default_archive_host:
    fail("cannot determine the default APT mirror host.")


def mirror(uri: str) -> dict[str, object]:
    return {"uri": uri, "arches": apt_arches}


def build_apt() -> dict[str, object]:
    primary: list[object] = []
    geoip = False
    if apt_mirror:
        primary.append(mirror(apt_mirror))
    if apt_region == "auto":
        geoip = True
        primary.append("country-mirror")
    elif re.fullmatch(r"[a-z]{2}", apt_region) and arch == "amd64":
        country_uri = f"http://{apt_region}.archive.ubuntu.com/ubuntu"
        if apt_mirror != country_uri:
            primary.append(mirror(country_uri))
    if not any(isinstance(item, dict) and item["uri"] == default_archive for item in primary):
        primary.append(mirror(default_archive))

    apt: dict[str, object] = {
        "preserve_sources_list": False,
        "geoip": geoip,
        "fallback": apt_fallback,
        "mirror-selection": {"primary": primary},
    }
    if apt_security_mirror:
        apt["security"] = [mirror(apt_security_mirror)]
    return apt


bootstrap = f"""#!/usr/bin/env bash
set -Eeuo pipefail

# Keep a durable, non-secret diagnostic trail.  This unit runs only on the
# installed system (cloud-init's first boot), never inside the live installer.
exec >>/var/log/netbird-enroll.log 2>&1
echo "$(date -Is) netbird-enroll: started"

systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target || true

mkdir -p /etc/systemd/logind.conf.d
cat > /etc/systemd/logind.conf.d/99-server.conf <<'EOF'
[Login]
HandleLidSwitch=ignore
HandleLidSwitchExternalPower=ignore
HandleLidSwitchDocked=ignore
EOF

echo "$(date -Is) netbird-enroll: network diagnostics"
ip route || true
getent ahosts {default_archive_host} || true

# Make one bounded package-installation attempt.  A non-zero exit is retried by
# systemd, rather than hiding a permanent DNS, mirror, or package error inside
# an endless shell loop.
apt-get -o Acquire::Retries=5 -o DPkg::Lock::Timeout=120 update
DEBIAN_FRONTEND=noninteractive apt-get -o Acquire::Retries=5 -o DPkg::Lock::Timeout=120 install -y \\
    curl ca-certificates docker.io docker-compose-v2 ufw

install -d -m 0755 /etc/docker /etc/systemd/journald.conf.d
cat > /etc/docker/daemon.json <<'EOF'
{{
  "log-driver": "local",
  "log-opts": {{
    "max-size": "10m",
    "max-file": "10"
  }}
}}
EOF

# 90 MiB plus one active 8 MiB journal file stays below roughly 100 MiB.
cat > /etc/systemd/journald.conf.d/99-size-limit.conf <<'EOF'
[Journal]
Storage=persistent
SystemMaxUse=90M
SystemMaxFileSize=8M
SystemMaxFiles=12
RuntimeMaxUse=90M
RuntimeMaxFileSize=8M
RuntimeMaxFiles=12
EOF
systemctl restart systemd-journald || true
journalctl --rotate || true
journalctl --vacuum-size=90M || true

usermod -aG docker {shlex.quote(username)}
systemctl enable --now docker.service
ufw default deny incoming
ufw default allow outgoing
ufw allow OpenSSH
ufw allow in on wt0
ufw allow out on wt0
ufw --force enable

NETBIRD_HOSTNAME={shlex.quote(hostname)}
echo "$(date -Is) netbird-enroll: installing NetBird"
curl -fsSL https://pkgs.netbird.io/install.sh | sh
command -v netbird >/dev/null

echo "$(date -Is) netbird-enroll: registering $NETBIRD_HOSTNAME"
netbird up --setup-key {shlex.quote(netbird_setup_key)} --hostname "$NETBIRD_HOSTNAME"
netbird status
echo "$(date -Is) netbird-enroll: enrollment completed as $NETBIRD_HOSTNAME"
logger -t netbird-enroll "NetBird enrollment completed as $NETBIRD_HOSTNAME"
install -D -m 0644 /dev/null /var/lib/netbird-enroll.done
"""

service = """[Unit]
Description=Provision Docker, firewall, and NetBird
Wants=network-online.target
After=network-online.target
StartLimitIntervalSec=0
ConditionPathExists=/usr/local/sbin/netbird-enroll.sh
ConditionPathExists=!/var/lib/netbird-enroll.done

[Service]
Type=simple
ExecStart=/usr/local/sbin/netbird-enroll.sh
Restart=on-failure
RestartSec=60
TimeoutStartSec=infinity

[Install]
WantedBy=multi-user.target
"""

config: dict[str, object] = {
    "autoinstall": {
        "version": 1,
        "locale": "en_US.UTF-8",
        "keyboard": {"layout": "us"},
        "storage": {"layout": {"name": "direct", "match": {"size": "largest"}}},
        "identity": {"hostname": hostname, "username": username, "password": password_hash},
        "ssh": {"install-server": True, "allow-pw": False, "authorized-keys": [ssh_public_key]},
        "apt": build_apt(),
        # DHCP on the target VM network returns unroutable documentation-range
        # addresses for Canonical and NetBird package hosts. Preserve DHCP
        # addressing while using public resolvers for provisioning.
        "network": {
            "version": 2,
            "ethernets": {
                "all-en": {
                    "match": {"name": "en*"},
                    "dhcp4": True,
                    "dhcp4-overrides": {"use-dns": False},
                    "nameservers": {"addresses": ["1.1.1.1", "8.8.8.8"]},
                }
            },
        },
        "updates": "security",
        # A reboot with the USB stick still first in the UEFI boot order starts
        # the live installer again.  Power off instead, so removing the stick
        # is an explicit and safe post-install step.
        "shutdown": "poweroff",
        "user-data": {
            "write_files": [
                {"path": f"/etc/sudoers.d/99-{username}-nopasswd", "owner": "root:root", "permissions": "0440", "content": f"{username} ALL=(ALL) NOPASSWD:ALL\n"},
                {"path": "/usr/local/sbin/netbird-enroll.sh", "owner": "root:root", "permissions": "0700", "content": bootstrap},
                {"path": "/etc/systemd/system/netbird-enroll.service", "owner": "root:root", "permissions": "0644", "content": service},
                {"path": "/usr/local/bin/compose", "owner": "root:root", "permissions": "0755", "content": "#!/bin/sh\nexec docker compose \"$@\"\n"},
                {"path": "/etc/profile.d/99-compose-alias.sh", "owner": "root:root", "permissions": "0644", "content": "alias compose='docker compose'\n"},
            ],
            "runcmd": [
                ["systemctl", "daemon-reload"],
                ["systemctl", "enable", "--now", "netbird-enroll.service"],
            ],
        },
    }
}


def validate_autoinstall(document: dict[str, object]) -> None:
    """Check the structural contract used by Subiquity and cloud-init."""
    autoinstall = document.get("autoinstall")
    if not isinstance(autoinstall, dict) or autoinstall.get("version") != 1:
        fail("autoinstall.version must be the integer 1.")

    for key in ("locale", "updates", "shutdown"):
        if not isinstance(autoinstall.get(key), str):
            fail(f"autoinstall.{key} must be a string.")
    if autoinstall["shutdown"] != "poweroff":
        fail("autoinstall.shutdown must be poweroff to prevent booting the USB installer again.")
    packages = autoinstall.get("packages", [])
    if not isinstance(packages, list) or not all(isinstance(package, str) for package in packages):
        fail("autoinstall.packages must be a list of strings when present.")

    user_data = autoinstall.get("user-data")
    if not isinstance(user_data, dict):
        fail("autoinstall.user-data must be a mapping.")
    for command in user_data.get("runcmd", []):
        if not isinstance(command, list) or not command or not all(isinstance(arg, str) for arg in command):
            fail("autoinstall.user-data.runcmd must contain non-empty string lists.")
    for entry in user_data.get("write_files", []):
        if not isinstance(entry, dict) or not all(isinstance(entry.get(key), str) for key in ("path", "content")):
            fail("autoinstall.user-data.write_files entries require string path and content values.")

    files = {entry["path"]: entry["content"] for entry in user_data.get("write_files", [])}
    bootstrap_content = files.get("/usr/local/sbin/netbird-enroll.sh", "")
    if '"log-driver": "local"' not in bootstrap_content or '"max-file": "10"' not in bootstrap_content:
        fail("Docker logging is not configured with bounded rotation.")
    if "SystemMaxUse=90M" not in bootstrap_content or "SystemMaxFileSize=8M" not in bootstrap_content:
        fail("systemd-journald is not configured with the required size limit.")
    service_content = files.get("/etc/systemd/system/netbird-enroll.service", "")
    if "Restart=on-failure" not in service_content or "StartLimitIntervalSec=0" not in service_content:
        fail("NetBird provisioner must retry failed first-boot attempts.")


validate_autoinstall(config)
rendered = yaml.safe_dump(config, allow_unicode=True, default_flow_style=False, sort_keys=False)
parsed = yaml.safe_load(rendered)
runcmd = parsed["autoinstall"]["user-data"]["runcmd"]
if parsed != config or not all(isinstance(arg, str) for command in runcmd for arg in command):
    fail("internal YAML validation failed.")
sys.stdout.write(rendered)

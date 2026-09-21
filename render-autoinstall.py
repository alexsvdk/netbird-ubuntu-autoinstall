#!/usr/bin/env python3
"""Render a validated Ubuntu autoinstall configuration from environment variables.

Dual-mode operation:
  SAMOVAR_MODE=samovar  — Samovar-specific config (serial disks, preflight,
                          recovery-agent embedding, Netplan wi-fi/lan0/wifi0).
  SAMOVAR_MODE=generic  — Original generic behaviour (default; backward-compatible).

When SAMOVAR_MODE is not set, the script auto-detects samovar mode if
samovar-config.json is present in the working directory.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sys
from pathlib import Path
from urllib.parse import urlparse

import yaml

ROOT = Path(__file__).resolve().parent

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def fail(message: str) -> None:
    print(f"Error: {message}", file=sys.stderr)
    raise SystemExit(1)


def required(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        fail(f"{name} is required.")
    return value


# ---------------------------------------------------------------------------
# Mode detection
# ---------------------------------------------------------------------------

_mode_env = os.environ.get("SAMOVAR_MODE", "").strip().lower()
if _mode_env == "samovar":
    SAMOVAR_MODE = True
elif _mode_env in ("generic", ""):
    # Auto-detect: samovar mode when samovar-config.json present
    SAMOVAR_MODE = Path("samovar-config.json").exists() and _mode_env != "generic"
else:
    fail(f"Unknown SAMOVAR_MODE={_mode_env!r}. Use 'samovar' or 'generic'.")

NETWORK_INTERFACE = os.environ.get("NETWORK_INTERFACE", "both").strip().lower() or "both"
if SAMOVAR_MODE and NETWORK_INTERFACE not in {"both", "lan0", "wifi0"}:
    fail("NETWORK_INTERFACE must be both, lan0, or wifi0.")

NOTIFY_TOPIC = os.environ.get("NOTIFY_TOPIC", "samovar_test").strip() or "samovar_test"
if not re.fullmatch(r"[A-Za-z0-9._-]{1,100}", NOTIFY_TOPIC):
    fail("NOTIFY_TOPIC must contain only letters, digits, dots, underscores, or hyphens.")
NOTIFY_URL = f"https://ntfy.sh/{NOTIFY_TOPIC}"

MIHOMO_IMAGE = os.environ.get("MIHOMO_IMAGE", "metacubex/mihomo:latest").strip()
if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/@:-]*", MIHOMO_IMAGE):
    fail("MIHOMO_IMAGE must be a valid Docker image reference without whitespace.")

# ---------------------------------------------------------------------------
# Common required inputs (both modes)
# ---------------------------------------------------------------------------

hostname = required("HOSTNAME")
username = required("USERNAME")

if not re.fullmatch(r"[a-z_][a-z0-9_-]*", username):
    fail("USERNAME must contain lowercase letters, digits, underscores, or hyphens.")

# ---------------------------------------------------------------------------
# Mode-branched inputs
# ---------------------------------------------------------------------------

VALID_SSH_KEY_PREFIXES = ("ssh-", "ecdsa-", "sk-")

if not SAMOVAR_MODE:
    # ── Generic mode ────────────────────────────────────────────────────────
    password_hash = required("PASSWORD_HASH")
    ssh_public_key = required("SSH_PUBLIC_KEY")
    if not ssh_public_key.startswith(VALID_SSH_KEY_PREFIXES):
        fail(
            "SSH key does not start with a valid OpenSSH key type "
            f"(ssh-, ecdsa-, sk-): {ssh_public_key[:40]!r}…"
        )
    netbird_setup_key = required("NETBIRD_SETUP_KEY")
    sudo_nopasswd: bool = True  # always true in generic mode (original behaviour)
    ssh_public_keys: list[str] = [ssh_public_key]
else:
    # ── Samovar mode ─────────────────────────────────────────────────────────
    password_hash = required("PASSWORD_HASH")

    # Multi-key SSH
    _keys_raw = os.environ.get("SSH_PUBLIC_KEYS", os.environ.get("SSH_PUBLIC_KEY", ""))
    if not _keys_raw:
        fail("SSH_PUBLIC_KEYS (or SSH_PUBLIC_KEY) is required in samovar mode.")
    # Split on newlines or commas
    _key_list = [k.strip() for k in re.split(r"[\n,]+", _keys_raw) if k.strip()]
    # Validate and deduplicate
    seen: dict[str, None] = {}
    for _k in _key_list:
        if not _k.startswith(VALID_SSH_KEY_PREFIXES):
            fail(
                "SSH key does not start with a valid OpenSSH key type "
                f"(ssh-, ecdsa-, sk-): {_k[:40]!r}…"
            )
        seen[_k] = None
    ssh_public_keys = list(seen)
    if not ssh_public_keys:
        fail("SSH_PUBLIC_KEYS must contain at least one valid key.")

    sudo_nopasswd = os.environ.get("SUDO_NOPASSWD", "true").strip().lower() != "false"

    # Load samovar-config.json if present
    _cfg_env = os.environ.get("SAMOVAR_CONFIG_FILE", "samovar-config.json")
    _cfg_path = Path(_cfg_env)
    if not _cfg_path.is_absolute() and not _cfg_path.exists():
        _cfg_path = Path(__file__).resolve().parent / _cfg_env
    samovar_config: dict = {}
    if _cfg_path.exists():
        with _cfg_path.open(encoding="utf-8") as _f:
            samovar_config = json.load(_f)

    allowed_signers = os.environ.get("ALLOWED_SIGNERS", "")

    # Generic compat: NETBIRD_SETUP_KEY not needed in samovar mode (comes via
    # samovar-config.json / recovery-agent at first boot).
    netbird_setup_key = ""  # not embedded in samovar mode

# ---------------------------------------------------------------------------
# APT / mirror config (both modes)
# ---------------------------------------------------------------------------

apt_region = os.environ.get("APT_REGION", "auto").strip().lower() or "auto"
apt_mirror = os.environ.get("APT_MIRROR", "").strip().rstrip("/")
apt_security_mirror = os.environ.get("APT_SECURITY_MIRROR", "").strip().rstrip("/")
apt_fallback = os.environ.get("APT_FALLBACK", "offline-install").strip() or "offline-install"

arch = os.environ.get("ARCH", "amd64").strip().lower() or "amd64"
arch = {"x86_64": "amd64", "x64": "amd64", "aarch64": "arm64", "arm": "arm64"}.get(arch, arch)
if arch not in {"amd64", "arm64"}:
    fail(f"unsupported ARCH {arch!r}. Use amd64 or arm64.")
if apt_fallback not in {"abort", "offline-install", "continue-anyway"}:
    fail("APT_FALLBACK must be abort, offline-install, or continue-anyway.")
if not re.fullmatch(r"auto|default|archive|none|custom|[a-z]{2}", apt_region):
    fail("APT_REGION must be auto, default, custom, or a two-letter country code.")
if apt_region == "custom" and not apt_mirror:
    fail("APT_REGION=custom requires APT_MIRROR.")
for _name, _uri in (("APT_MIRROR", apt_mirror), ("APT_SECURITY_MIRROR", apt_security_mirror)):
    if _uri and not re.match(r"^https?://", _uri, re.IGNORECASE):
        fail(f"{_name} must be an http(s) URL.")

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


# ---------------------------------------------------------------------------
# Notifications (best-effort; never block installation)
# ---------------------------------------------------------------------------

def _notify_live_command(message: str) -> str:
    url = shlex.quote(NOTIFY_URL)
    body = shlex.quote(message)
    return (
        f"url={url}; body={body}; "
        "if command -v curl >/dev/null 2>&1; then "
        "curl --fail --silent --show-error --connect-timeout 5 --max-time 15 "
        ' -H "Title: Samovar installer" --data-raw "$body" "$url" >/dev/null 2>&1 || true; '
        "elif command -v wget >/dev/null 2>&1; then "
        "wget --quiet --timeout=15 --header='Title: Samovar installer' "
        ' --post-data="$body" -O - "$url" >/dev/null 2>&1 || true; '
        "fi; exit 0"
    )


_notify_script = f"""#!/usr/bin/env bash
set -u
URL={shlex.quote(NOTIFY_URL)}
MESSAGE="${{1:-}}"
if command -v curl >/dev/null 2>&1; then
    curl --fail --silent --show-error --connect-timeout 5 --max-time 15 \
        -H "Title: Samovar installer" --data-raw "$MESSAGE" "$URL" >/dev/null 2>&1 || true
elif command -v wget >/dev/null 2>&1; then
    wget --quiet --timeout=15 --header="Title: Samovar installer" \
        --post-data="$MESSAGE" -O - "$URL" >/dev/null 2>&1 || true
fi
exit 0
"""


# ---------------------------------------------------------------------------
# Generic bootstrap script (kept for backward compatibility)
# ---------------------------------------------------------------------------

bootstrap = f"""#!/usr/bin/env bash
set -Eeuo pipefail

# Keep a durable, non-secret diagnostic trail.  This unit runs only on the
# installed system (cloud-init's first boot), never inside the live installer.
exec >>/var/log/netbird-enroll.log 2>&1
echo "$(date -Is) netbird-enroll: started"
notify() {{ /usr/local/sbin/samovar-notify "$1" || true; }}
trap 'rc=$?; notify "Ошибка provisioning (код $rc)"' ERR

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
notify "Сеть доступна, базовые пакеты установлены"

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
notify "Началась установка NetBird"
echo "$(date -Is) netbird-enroll: installing NetBird"
curl -fsSL https://pkgs.netbird.io/install.sh | sh
command -v netbird >/dev/null
notify "NetBird установлен"

echo "$(date -Is) netbird-enroll: registering $NETBIRD_HOSTNAME"
netbird up --setup-key {shlex.quote(netbird_setup_key)} --hostname "$NETBIRD_HOSTNAME"
netbird status
echo "$(date -Is) netbird-enroll: enrollment completed as $NETBIRD_HOSTNAME"
logger -t netbird-enroll "NetBird enrollment completed as $NETBIRD_HOSTNAME"
notify "NetBird подключён, provisioning завершён"
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

# ---------------------------------------------------------------------------
# Samovar disk serials
# ---------------------------------------------------------------------------

# Some virtual controllers expose a disk serial with a model prefix, for
# example QEMU_HARDDISK_50026B7683695BFE.  Keep the physical target default
# unchanged and allow VM builds to opt into that prefix explicitly.
DISK_SERIAL_PREFIX = os.environ.get("DISK_SERIAL_PREFIX", "")
if not re.fullmatch(r"[A-Za-z0-9_.-]*", DISK_SERIAL_PREFIX):
    fail("DISK_SERIAL_PREFIX may contain only letters, digits, '_', '-', and '.'.")

SWAP_SIZE_GIB = os.environ.get("SWAP_SIZE_GIB", "1").strip()
if not re.fullmatch(r"[1-9][0-9]*", SWAP_SIZE_GIB):
    fail("SWAP_SIZE_GIB must be a positive integer number of GiB.")

SYSTEM_SSD_SERIAL = f"{DISK_SERIAL_PREFIX}50026B7683695BFE"  # Kingston 240GB - root
DATA_SSD_SERIAL   = f"{DISK_SERIAL_PREFIX}TD2023102401304"    # SBSSD 240GB   - /data
HDD_SERIAL        = f"{DISK_SERIAL_PREFIX}WCC3F1336131"       # WD 1TB        - /archive

# ---------------------------------------------------------------------------
# Samovar storage config
# ---------------------------------------------------------------------------

def build_samovar_storage() -> dict[str, object]:
    """Return a curtin storage config using exact serial-number matching.

    No size-based matching is used. Each disk is identified by its unique
    serial number so that the wrong disk cannot be accidentally wiped.
    """
    storage_config: list[dict[str, object]] = [
        # ── Disks (identified by serial number) ──────────────────────────
        {
            "id": "disk-system",
            "type": "disk",
            "serial": SYSTEM_SSD_SERIAL,
            "ptable": "gpt",
            "wipe": "superblock",
            "preserve": False,
            "grub_device": True,
        },
        {
            "id": "disk-data",
            "type": "disk",
            "serial": DATA_SSD_SERIAL,
            "ptable": "gpt",
            "wipe": "superblock",
            "preserve": False,
        },
        {
            "id": "disk-archive",
            "type": "disk",
            "serial": HDD_SERIAL,
            "ptable": "gpt",
            "wipe": "superblock",
            "preserve": False,
        },
        # ── System SSD partitions ─────────────────────────────────────────
        {
            "id": "part-efi",
            "type": "partition",
            "device": "disk-system",
            "size": "1G",
            "flag": "boot",
            "grub_device": True,
        },
        {
            "id": "part-root",
            "type": "partition",
            "device": "disk-system",
            "size": -1,   # remainder of disk
        },
        # ── Data SSD partition ────────────────────────────────────────────
        {
            "id": "part-data",
            "type": "partition",
            "device": "disk-data",
            "size": -1,
        },
        # ── HDD partition ─────────────────────────────────────────────────
        {
            "id": "part-archive",
            "type": "partition",
            "device": "disk-archive",
            "size": -1,
        },
        # ── Formats ───────────────────────────────────────────────────────
        {"id": "fmt-efi",     "type": "format", "volume": "part-efi",     "fstype": "fat32", "label": "EFI"},
        {"id": "fmt-root",    "type": "format", "volume": "part-root",    "fstype": "ext4",  "label": "root"},
        {"id": "fmt-data",    "type": "format", "volume": "part-data",    "fstype": "ext4",  "label": "data"},
        {"id": "fmt-archive", "type": "format", "volume": "part-archive", "fstype": "ext4",  "label": "archive"},
        # ── Mounts ────────────────────────────────────────────────────────
        {"id": "mount-efi",     "type": "mount", "device": "fmt-efi",     "path": "/boot/efi"},
        {"id": "mount-root",    "type": "mount", "device": "fmt-root",    "path": "/"},
        {"id": "mount-data",    "type": "mount", "device": "fmt-data",    "path": "/data"},
        {"id": "mount-archive", "type": "mount", "device": "fmt-archive", "path": "/archive"},
    ]
    return {"config": storage_config}


# ---------------------------------------------------------------------------
# Samovar preflight (early-commands)
# ---------------------------------------------------------------------------

# Written to /run/samovar-preflight.sh then executed.  Written with a heredoc
# to avoid any quoting issues when embedded in autoinstall YAML.
_preflight_script = f"""\
#!/bin/bash
set -e
echo 'Samovar preflight: checking hardware...'
# Check UEFI
[ -d /sys/firmware/efi ] || {{ echo 'ERROR: Not in UEFI mode'; exit 1; }}
# Check arch
uname -m | grep -q x86_64 || {{ echo 'ERROR: Not x86_64'; exit 1; }}
# Check full udev serials — each must appear exactly once.
# lsblk SERIAL may expose only ID_SERIAL_SHORT on QEMU disks.
for serial in {SYSTEM_SSD_SERIAL} {DATA_SSD_SERIAL} {HDD_SERIAL}; do
  count=0
  while read -r dev; do
    actual=$(udevadm info -q property -n "$dev" 2>/dev/null | sed -n 's/^ID_SERIAL=//p')
    if [ "$actual" = "$serial" ]; then
      count=$((count + 1))
    fi
  done < <(lsblk -dn -o NAME,TYPE 2>/dev/null | awk '$2 == "disk" {{print "/dev/" $1}}')
  [ "$count" -eq 1 ] || {{ echo "ERROR: Serial $serial found $count times (expected 1)"; exit 1; }}
done
echo 'Samovar preflight: all checks passed'
"""

samovar_early_commands: list[list[str]] = [
    ["sh", "-c", _notify_live_command("Установщик запущен")],
    ["sh", "-c", f"cat > /run/samovar-preflight.sh << 'PREFLIGHT_EOF'\n{_preflight_script}PREFLIGHT_EOF"],
    ["sh", "-c", "chmod +x /run/samovar-preflight.sh && bash /run/samovar-preflight.sh"],
    ["sh", "-c", _notify_live_command("Проверка оборудования пройдена")],
]


# ---------------------------------------------------------------------------
# Samovar network (Netplan)
# ---------------------------------------------------------------------------

def build_samovar_network() -> dict[str, object]:
    """Generate Netplan for the selected Samovar network interface(s)."""
    network: dict[str, object] = {"version": 2}

    if NETWORK_INTERFACE in {"both", "lan0"}:
        network["ethernets"] = {
            "lan0": {
                "match": {"macaddress": "44:8a:5b:64:11:2b"},
                "set-name": "lan0",
                "dhcp4": True,
                "dhcp4-overrides": {"route-metric": 10},
                "optional": True,
            }
        }

    if NETWORK_INTERFACE not in {"both", "wifi0"}:
        return network

    # Add Wi-Fi access-points from samovar-config.json if available
    wifi_networks: list[dict] = []
    if SAMOVAR_MODE and samovar_config:
        wifi_section = samovar_config.get("wifi", {})
        wifi_networks = wifi_section.get("networks", [])

    access_points: dict[str, object] = {}
    if wifi_networks:
        for net in wifi_networks:
            ssid = net.get("ssid", "")
            if not ssid:
                continue
            ap_entry: dict[str, object] = {}
            if net.get("password"):
                ap_entry["password"] = net["password"]
            if net.get("hidden"):
                ap_entry["hidden"] = True
            access_points[ssid] = ap_entry

    if not access_points:
        access_points = {"samovar-fallback": {"password": "samovar-fallback"}}

    # In Netplan with networkd backend, wifis must NOT use match: or set-name:
    # (only allowed by interface name). Interface renaming by MAC is handled
    # at the systemd/udev level via /etc/systemd/network/10-wifi0.link.
    network["wifis"] = {
        "wifi0": {
            "dhcp4": True,
            "dhcp4-overrides": {"route-metric": 20},
            "optional": True,
            "access-points": access_points,
        }
    }

    return network


# ---------------------------------------------------------------------------
# Samovar provisioning bootstrap (samovar-provision.sh)
# ---------------------------------------------------------------------------

_samovar_firewall_interfaces = [
    interface
    for interface in ("wifi0", "lan0")
    if NETWORK_INTERFACE in {"both", interface}
]
_samovar_firewall_rules = "\n".join(
    f"ufw allow in on {interface} to any port 22"
    for interface in _samovar_firewall_interfaces
)

_samovar_provision_script = f"""\
#!/usr/bin/env bash
# samovar-provision.sh — first-boot provisioning for samovar.
# Invoked by samovar-provision.service on first boot.
# NetBird enrollment is handled separately by samovar-recovery-bootstrap.service.
set -Eeuo pipefail

LOGFILE=/var/log/samovar-provision.log
exec >>"$LOGFILE" 2>&1
echo "$(date -Is) samovar-provision: started"
notify() {{ /usr/local/sbin/samovar-notify "$1" || true; }}
trap 'rc=$?; notify "Ошибка provisioning (код $rc)"' ERR
notify "Началась настройка установленной системы"

# ── Prevent repeated runs ────────────────────────────────────────────────────
DONE_FILE=/var/lib/samovar-provision.done
if [ -f "$DONE_FILE" ]; then
  echo "$(date -Is) samovar-provision: already completed, exiting"
  exit 0
fi

# ── Mask sleep/suspend ───────────────────────────────────────────────────────
systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target || true

mkdir -p /etc/systemd/logind.conf.d
cat > /etc/systemd/logind.conf.d/99-server.conf <<'EOF'
[Login]
HandleLidSwitch=ignore
HandleLidSwitchExternalPower=ignore
HandleLidSwitchDocked=ignore
EOF

# ── fstrim timer ─────────────────────────────────────────────────────────────
systemctl enable fstrim.timer || true

# ── Swap files ({SWAP_SIZE_GIB} GiB on each SSD) ───────────────────────────────────────────
create_swap_file() {{
  local swapfile="$1"
  local size_gib="$2"

  if swapon --show=NAME --noheadings | grep -Fxq "${{swapfile}}"; then
    echo "$(date -Is) samovar-provision: swap ${{swapfile}} already active"
  elif [ -f "${{swapfile}}" ]; then
    chmod 600 "${{swapfile}}"
    mkswap "${{swapfile}}"
    swapon "${{swapfile}}"
  else
    fallocate -l "${{size_gib}}G" "${{swapfile}}" \
      || dd if=/dev/zero of="${{swapfile}}" bs=1G count="${{size_gib}}" status=progress
    chmod 600 "${{swapfile}}"
    mkswap "${{swapfile}}"
    swapon "${{swapfile}}"
  fi

  if ! grep -Fqx "${{swapfile}} none swap sw 0 0" /etc/fstab; then
    echo "${{swapfile}} none swap sw 0 0" >> /etc/fstab
  fi
}}
create_swap_file /swapfile {SWAP_SIZE_GIB}
create_swap_file /data/swapfile {SWAP_SIZE_GIB}

# ── journald limits ──────────────────────────────────────────────────────────
install -d -m 0755 /etc/systemd/journald.conf.d
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

# ── Package installation ──────────────────────────────────────────────────────
apt-get -o Acquire::Retries=5 -o DPkg::Lock::Timeout=120 update
DEBIAN_FRONTEND=noninteractive apt-get -o Acquire::Retries=5 -o DPkg::Lock::Timeout=120 install -y \\
    curl ca-certificates docker.io docker-compose-v2 ufw \\
    smartmontools btop tmux git jq
notify "Базовые пакеты установлены"

# ── Mihomo Compose runtime ────────────────────────────────────────────────────
# The image is selected in /etc/mihomo/compose.env and can be updated without
# rebuilding the ISO or downloading a host binary.
install -d -m 0755 /etc/mihomo /var/lib/mihomo
systemctl daemon-reload
systemctl enable mihomo.service

# ── NetBird client ────────────────────────────────────────────────────────────
# The recovery bootstrap invokes netbird, so install it before that service is
# allowed to run. A bounded download prevents a dead network from blocking the
# whole first-boot provisioning attempt forever; systemd retries failed attempts.
notify "Началась установка NetBird"
echo "$(date -Is) samovar-provision: installing NetBird"
curl --fail --silent --show-error --location \\
    --connect-timeout 15 --max-time 120 \\
    https://pkgs.netbird.io/install.sh | sh
command -v netbird
notify "NetBird установлен"

# ── Docker ───────────────────────────────────────────────────────────────────
install -d -m 0755 /etc/docker
cat > /etc/docker/daemon.json <<'EOF'
{{
  "log-driver": "local",
  "log-opts": {{
    "max-size": "10m",
    "max-file": "10"
  }},
  "data-root": "/data/docker"
}}
EOF

# ── /data and /archive subdirectories ────────────────────────────────────────
install -d -m 0755 /data/docker /data/models /data/cache /data/tmp
install -d -m 0755 /archive/incoming /archive/output /archive/backups

usermod -aG docker {shlex.quote(username)}
systemctl enable --now docker.service

# ── Firewall ──────────────────────────────────────────────────────────────────
ufw default deny incoming
ufw default allow outgoing
ufw allow in on wt0 to any port 22
{_samovar_firewall_rules}
ufw allow in on wt0
ufw allow out on wt0
ufw --force enable

# ── compose wrapper ───────────────────────────────────────────────────────────
install -m 0755 /dev/null /usr/local/bin/compose
printf '#!/bin/sh\\nexec docker compose "$@"\\n' > /usr/local/bin/compose

echo "$(date -Is) samovar-provision: completed"
logger -t samovar-provision "Samovar provisioning completed"
notify "Базовая настройка системы завершена"
install -D -m 0644 /dev/null "$DONE_FILE"
"""

_samovar_provision_service = """\
[Unit]
Description=Samovar first-boot provisioning
Wants=network-online.target
After=network-online.target data.mount archive.mount
StartLimitIntervalSec=0
ConditionPathExists=/usr/local/sbin/samovar-provision.sh
ConditionPathExists=!/var/lib/samovar-provision.done

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/sbin/samovar-provision.sh
Restart=on-failure
RestartSec=60
TimeoutStartSec=infinity

[Install]
WantedBy=multi-user.target
"""

# ── Recovery-agent service units ──────────────────────────────────────────────

_samovar_recovery_service = f"""\
[Unit]
Description=Samovar Recovery Agent
After=network-online.target
Wants=network-online.target

[Service]
Environment=NETWORK_INTERFACE={NETWORK_INTERFACE}
Environment=HOME=/root
Type=simple
ExecStart=/usr/local/sbin/samovar-recovery-agent.py --run
Restart=on-failure
RestartSec=30
TimeoutStartSec=120

[Install]
WantedBy=multi-user.target
"""

_samovar_recovery_timer = """\
[Unit]
Description=Samovar Recovery Agent periodic scan

[Timer]
OnBootSec=30s
OnUnitActiveSec=5min
Unit=samovar-recovery-scan.service

[Install]
WantedBy=timers.target
"""

_samovar_recovery_scan_service = f"""\
[Unit]
Description=Samovar Recovery Agent scan
After=network-online.target

[Service]
Environment=NETWORK_INTERFACE={NETWORK_INTERFACE}
Environment=HOME=/root
Type=oneshot
ExecStart=/usr/local/sbin/samovar-recovery-agent.py --scan-usb
TimeoutStartSec=120
"""

_samovar_recovery_udev_rules = """\
# Trigger Samovar recovery agent when SAMOVARCFG USB is inserted
ACTION=="add", SUBSYSTEM=="block", ENV{ID_FS_LABEL}=="SAMOVARCFG", \\
    TAG+="systemd", ENV{SYSTEMD_WANTS}="samovar-recovery-scan.service"
"""

_samovar_recovery_bootstrap_service = f"""\
[Unit]
Description=Samovar Recovery Bootstrap (first-boot NetBird enrollment)
After=network-online.target samovar-provision.service
Wants=network-online.target
ConditionPathExists=/var/lib/samovar-recovery/bootstrap-inbox/samovar-config.json
ConditionPathExists=!/var/lib/samovar-recovery/state.json
StartLimitIntervalSec=0

[Service]
Environment=NETWORK_INTERFACE={NETWORK_INTERFACE}
Environment=HOME=/root
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/sbin/samovar-recovery-agent.py --bootstrap
Restart=on-failure
RestartSec=60
TimeoutStartSec=infinity

[Install]
WantedBy=multi-user.target
"""

# ── Recovery-agent Python script (stub — real file embedded from recovery/) ──

_recovery_agent_stub = """\
#!/usr/bin/env python3
# samovar-recovery-agent.py — installed to /usr/local/sbin/ on the target.
# Full implementation lives in recovery/samovar-recovery-agent.py in the repo.
# This stub is replaced by the builder when recovery/samovar-recovery-agent.py exists.
import sys
print("samovar-recovery-agent: not yet fully implemented", file=sys.stderr)
sys.exit(1)
"""


def _load_recovery_agent() -> str:
    """Load recovery agent from repo if present, otherwise use stub."""
    candidate = ROOT / "recovery/samovar-recovery-agent.py"
    if candidate.exists():
        return candidate.read_text(encoding="utf-8")
    return _recovery_agent_stub


def _load_mihomo_service() -> str:
    """Load the host Mihomo unit shipped with the provisioning bundle."""
    candidate = ROOT / "provisioning/mihomo.service"
    if candidate.exists():
        return candidate.read_text(encoding="utf-8")
    return ""


def _load_mihomo_compose() -> str:
    """Load the host Mihomo Compose definition."""
    candidate = ROOT / "provisioning/mihomo.compose.yml"
    if candidate.exists():
        return candidate.read_text(encoding="utf-8")
    return ""


def _load_mihomo_env() -> str:
    """Render the user-editable Mihomo image selection."""
    return f"MIHOMO_IMAGE={MIHOMO_IMAGE}\n"


def _load_samovar_config_bytes() -> str:
    """Return samovar-config.json content for embedding, or empty string."""
    cfg_path = Path(os.environ.get("SAMOVAR_CONFIG_FILE", "samovar-config.json"))
    if cfg_path.exists():
        return cfg_path.read_text(encoding="utf-8")
    return ""


def _load_samovar_config_sig() -> str:
    """Return samovar-config.json.sig content for embedding, or empty string."""
    sig_path = Path(os.environ.get("SAMOVAR_CONFIG_FILE", "samovar-config.json") + ".sig")
    if sig_path.exists():
        return sig_path.read_text(encoding="utf-8")
    return ""


# ---------------------------------------------------------------------------
# Build the config document
# ---------------------------------------------------------------------------

def build_write_files_generic() -> list[dict[str, str]]:
    return [
        {
            "path": "/usr/local/sbin/samovar-notify",
            "owner": "root:root",
            "permissions": "0700",
            "content": _notify_script,
        },
        {
            "path": f"/etc/sudoers.d/99-{username}-nopasswd",
            "owner": "root:root",
            "permissions": "0440",
            "content": f"{username} ALL=(ALL) NOPASSWD:ALL\n",
        },
        {
            "path": "/usr/local/sbin/netbird-enroll.sh",
            "owner": "root:root",
            "permissions": "0700",
            "content": bootstrap,
        },
        {
            "path": "/etc/systemd/system/netbird-enroll.service",
            "owner": "root:root",
            "permissions": "0644",
            "content": service,
        },
        {
            "path": "/usr/local/bin/compose",
            "owner": "root:root",
            "permissions": "0755",
            "content": "#!/bin/sh\nexec docker compose \"$@\"\n",
        },
        {
            "path": "/etc/profile.d/99-compose-alias.sh",
            "owner": "root:root",
            "permissions": "0644",
            "content": "alias compose='docker compose'\n",
        },
    ]


def build_write_files_samovar() -> list[dict[str, str]]:
    files: list[dict[str, str]] = []

    files.append({
        "path": "/usr/local/sbin/samovar-notify",
        "owner": "root:root",
        "permissions": "0700",
        "content": _notify_script,
    })

    # ── sudoers ──────────────────────────────────────────────────────────────
    if sudo_nopasswd:
        files.append({
            "path": f"/etc/sudoers.d/99-{username}-nopasswd",
            "owner": "root:root",
            "permissions": "0440",
            "content": f"{username} ALL=(ALL) NOPASSWD:ALL\n",
        })

    # ── Provisioning script ──────────────────────────────────────────────────
    files.append({
        "path": "/usr/local/sbin/samovar-provision.sh",
        "owner": "root:root",
        "permissions": "0700",
        "content": _samovar_provision_script,
    })
    files.append({
        "path": "/etc/systemd/system/samovar-provision.service",
        "owner": "root:root",
        "permissions": "0644",
        "content": _samovar_provision_service,
    })
    files.append({
        "path": "/etc/systemd/system/mihomo.service",
        "owner": "root:root",
        "permissions": "0644",
        "content": _load_mihomo_service(),
    })
    files.append({
        "path": "/etc/mihomo/compose.yml",
        "owner": "root:root",
        "permissions": "0644",
        "content": _load_mihomo_compose(),
    })
    files.append({
        "path": "/etc/mihomo/compose.env",
        "owner": "root:root",
        "permissions": "0600",
        "content": _load_mihomo_env(),
    })

    # ── Recovery agent ───────────────────────────────────────────────────────
    files.append({
        "path": "/usr/local/sbin/samovar-recovery-agent.py",
        "owner": "root:root",
        "permissions": "0700",
        "content": _load_recovery_agent(),
    })

    # ── Recovery systemd units ────────────────────────────────────────────────
    files.append({
        "path": "/etc/systemd/system/samovar-recovery.service",
        "owner": "root:root",
        "permissions": "0644",
        "content": _samovar_recovery_service,
    })
    files.append({
        "path": "/etc/systemd/system/samovar-recovery.timer",
        "owner": "root:root",
        "permissions": "0644",
        "content": _samovar_recovery_timer,
    })
    files.append({
        "path": "/etc/systemd/system/samovar-recovery-scan.service",
        "owner": "root:root",
        "permissions": "0644",
        "content": _samovar_recovery_scan_service,
    })
    files.append({
        "path": "/etc/systemd/system/samovar-recovery-bootstrap.service",
        "owner": "root:root",
        "permissions": "0644",
        "content": _samovar_recovery_bootstrap_service,
    })

    # ── udev rule ─────────────────────────────────────────────────────────────
    files.append({
        "path": "/etc/udev/rules.d/99-samovar-recovery.rules",
        "owner": "root:root",
        "permissions": "0644",
        "content": _samovar_recovery_udev_rules,
    })

    # ── allowed_signers ───────────────────────────────────────────────────────
    if allowed_signers:
        files.append({
            "path": "/etc/samovar-recovery/allowed_signers",
            "owner": "root:root",
            "permissions": "0640",
            "content": allowed_signers if allowed_signers.endswith("\n") else allowed_signers + "\n",
        })

    # ── Bootstrap inbox: signed config ────────────────────────────────────────
    _cfg_content = _load_samovar_config_bytes()
    if _cfg_content:
        files.append({
            "path": "/var/lib/samovar-recovery/bootstrap-inbox/samovar-config.json",
            "owner": "root:root",
            "permissions": "0600",
            "content": _cfg_content,
        })
    _sig_content = _load_samovar_config_sig()
    if _sig_content:
        files.append({
            "path": "/var/lib/samovar-recovery/bootstrap-inbox/samovar-config.json.sig",
            "owner": "root:root",
            "permissions": "0600",
            "content": _sig_content,
        })

    # ── compose wrapper ───────────────────────────────────────────────────────
    files.append({
        "path": "/usr/local/bin/compose",
        "owner": "root:root",
        "permissions": "0755",
        "content": "#!/bin/sh\nexec docker compose \"$@\"\n",
    })
    files.append({
        "path": "/etc/profile.d/99-compose-alias.sh",
        "owner": "root:root",
        "permissions": "0644",
        "content": "alias compose='docker compose'\n",
    })

    # ── Wi-Fi systemd.link (rename by MAC without Netplan match) ─────────────
    if NETWORK_INTERFACE in {"both", "wifi0"}:
        files.append({
            "path": "/etc/systemd/network/10-wifi0.link",
            "owner": "root:root",
            "permissions": "0644",
            "content": "[Match]\nMACAddress=34:13:e8:3c:b5:9a\n\n[Link]\nName=wifi0\n",
        })

    return files


def build_runcmd_generic() -> list[list[str]]:
    return [
        ["systemctl", "daemon-reload"],
        ["systemctl", "enable", "--now", "netbird-enroll.service"],
        ["/usr/local/sbin/samovar-notify", "Установка завершена, компьютер выключается"],
    ]


def build_runcmd_samovar() -> list[list[str]]:
    return [
        ["systemctl", "daemon-reload"],
        # Create required directories (idempotent)
        ["install", "-d", "-m", "0700", "/etc/samovar-recovery"],
        ["install", "-d", "-m", "0700", "/var/lib/samovar-recovery/bootstrap-inbox"],
        # Start provisioning now; it must finish before NetBird bootstrap runs.
        ["systemctl", "enable", "--now", "samovar-provision.service"],
        ["systemctl", "enable", "--now", "samovar-recovery-bootstrap.service"],
        ["systemctl", "enable", "--now", "samovar-recovery.timer"],
        # Enable fstrim for SSDs
        ["systemctl", "enable", "fstrim.timer"],
        ["/usr/local/sbin/samovar-notify", "Установка завершена, компьютер выключается"],
    ]


# ── Assemble the config document ──────────────────────────────────────────────

if not SAMOVAR_MODE:
    # ── Generic mode config ──────────────────────────────────────────────────
    config: dict[str, object] = {
        "autoinstall": {
            "version": 1,
            "locale": "en_US.UTF-8",
            "keyboard": {"layout": "us"},
            "storage": {"layout": {"name": "direct", "match": {"size": "largest"}}},
            "identity": {"hostname": hostname, "username": username, "password": password_hash},
            "ssh": {"install-server": True, "allow-pw": False, "authorized-keys": ssh_public_keys},
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
            "early-commands": [["sh", "-c", _notify_live_command("Установщик запущен")]],
            # A reboot with the USB stick still first in the UEFI boot order starts
            # the live installer again.  Power off instead, so removing the stick
            # is an explicit and safe post-install step.
            "shutdown": "poweroff",
            "user-data": {
                "write_files": build_write_files_generic(),
                "runcmd": build_runcmd_generic(),
            },
        }
    }
else:
    # ── Samovar mode config ──────────────────────────────────────────────────
    config = {
        "autoinstall": {
            "version": 1,
            "locale": "en_US.UTF-8",
            "keyboard": {"layout": "us"},
            # Serial-based disk layout — no size matching
            "storage": build_samovar_storage(),
            "identity": {"hostname": hostname, "username": username, "password": password_hash},
            "ssh": {
                "install-server": True,
                "allow-pw": False,
                "authorized-keys": ssh_public_keys,
            },
            "apt": build_apt(),
            "network": build_samovar_network(),
            "updates": "security",
            "shutdown": "poweroff",
            # Preflight: verify hardware serials before any disk writes
            "early-commands": samovar_early_commands,
            "user-data": {
                "write_files": build_write_files_samovar(),
                "runcmd": build_runcmd_samovar(),
            },
        }
    }


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


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

    if not SAMOVAR_MODE:
        # Generic mode: validate the original netbird-enroll.sh constraints
        bootstrap_content = files.get("/usr/local/sbin/netbird-enroll.sh", "")
        if '"log-driver": "local"' not in bootstrap_content or '"max-file": "10"' not in bootstrap_content:
            fail("Docker logging is not configured with bounded rotation.")
        if "SystemMaxUse=90M" not in bootstrap_content or "SystemMaxFileSize=8M" not in bootstrap_content:
            fail("systemd-journald is not configured with the required size limit.")
        service_content = files.get("/etc/systemd/system/netbird-enroll.service", "")
        if "Restart=on-failure" not in service_content or "StartLimitIntervalSec=0" not in service_content:
            fail("NetBird provisioner must retry failed first-boot attempts.")
    else:
        # Samovar mode: validate the samovar-provision.sh constraints
        provision_content = files.get("/usr/local/sbin/samovar-provision.sh", "")
        if '"log-driver": "local"' not in provision_content or '"max-file": "10"' not in provision_content:
            fail("Docker logging is not configured with bounded rotation.")
        if "SystemMaxUse=90M" not in provision_content or "SystemMaxFileSize=8M" not in provision_content:
            fail("systemd-journald is not configured with the required size limit.")
        provision_svc = files.get("/etc/systemd/system/samovar-provision.service", "")
        if "Restart=on-failure" not in provision_svc or "StartLimitIntervalSec=0" not in provision_svc:
            fail("Samovar provisioner must retry failed first-boot attempts.")
        # Verify storage uses serial matching, not size-based
        storage = autoinstall.get("storage", {})
        if isinstance(storage, dict) and "layout" in storage:
            layout = storage.get("layout", {})
            if isinstance(layout, dict) and "size" in str(layout.get("match", "")):
                fail("samovar mode must not use size-based disk matching.")


validate_autoinstall(config)
rendered = yaml.safe_dump(config, allow_unicode=True, default_flow_style=False, sort_keys=False)
parsed = yaml.safe_load(rendered)
runcmd = parsed["autoinstall"]["user-data"]["runcmd"]
if not all(isinstance(arg, str) for command in runcmd for arg in command):
    fail("internal YAML validation failed: runcmd args must all be strings.")
if parsed["autoinstall"]["version"] != 1:
    fail("internal YAML validation failed: version mismatch.")
sys.stdout.write(rendered)

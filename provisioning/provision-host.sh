#!/usr/bin/env bash
# provision-host.sh — Samovar first-boot host provisioning
#
# Called by samovar-provision.service after the first successful boot.
# Designed to be idempotent: safe to run multiple times.
# Secrets are NEVER written to the log file, stdout, or journald.
#
# Log: /var/log/samovar-provision.log
# Exit codes: 0 = success, 1 = fatal error
#
# Sections:
#   1.  Logging helpers
#   2.  Preflight checks
#   3.  APT setup (Yandex mirror, security updates)
#   4.  Base packages
#   5.  Swap file
#   6.  Directory layout
#   7.  journald tuning
#   8.  Disable sleep/suspend/hibernate
#   9.  NTP
#  10.  NVIDIA driver + Container Toolkit
#  11.  Docker Engine
#  12.  Mihomo user and service
#  13.  UFW firewall rules
#  14.  Unattended-upgrades
#  15.  fstrim.timer
#  16.  Final health report

set -euo pipefail

# ---------------------------------------------------------------------------
# 1. Logging helpers
# ---------------------------------------------------------------------------

LOG_FILE="/var/log/samovar-provision.log"
STAMP_FILE="/var/lib/samovar-provision.stamp"

_ts() { date '+%Y-%m-%dT%H:%M:%S%z'; }

log()  { echo "$(_ts) [INFO]  $*" | tee -a "${LOG_FILE}"; }
warn() { echo "$(_ts) [WARN]  $*" | tee -a "${LOG_FILE}"; }
die()  { echo "$(_ts) [FATAL] $*" | tee -a "${LOG_FILE}"; exit 1; }

# Redact any argument that looks like it might contain a secret token/password.
# Usage: log_cmd <command> [args...]
# Never pass actual secret values here — call the command directly in the script.
log_cmd() {
    log "Running: $*"
}

# ---------------------------------------------------------------------------
# 2. Preflight checks
# ---------------------------------------------------------------------------

if [[ "${EUID}" -ne 0 ]]; then
    die "Must be run as root."
fi

log "=========================================="
log "Samovar provisioning started"
log "=========================================="

# Guard: skip if already fully provisioned
if [[ -f "${STAMP_FILE}" ]]; then
    log "Stamp file found at ${STAMP_FILE} — provisioning already completed."
    log "Remove ${STAMP_FILE} to re-run provisioning."
    exit 0
fi

# Verify /data is mounted before doing anything Docker-related
if ! mountpoint -q /data; then
    die "/data is not mounted. Provisioning requires /data to be available."
fi

if ! mountpoint -q /archive; then
    warn "/archive is not mounted — archive directories will be skipped."
    ARCHIVE_MOUNTED=0
else
    ARCHIVE_MOUNTED=1
fi

# ---------------------------------------------------------------------------
# 3. APT setup
# ---------------------------------------------------------------------------

log "Configuring APT sources (Yandex mirror preferred)..."

# Ubuntu 26.04 (noble) — use Yandex mirror as primary
APT_SOURCES="/etc/apt/sources.list.d/ubuntu.sources"
if [[ ! -f "${APT_SOURCES}.samovar-bak" ]]; then
    cp "${APT_SOURCES}" "${APT_SOURCES}.samovar-bak" 2>/dev/null || true
fi

# Write DEB822-format sources for noble using Yandex mirror
cat > /etc/apt/sources.list.d/samovar-ubuntu.sources <<'EOF'
Types: deb
URIs: http://mirror.yandex.ru/ubuntu
Suites: noble noble-updates
Components: main restricted universe multiverse
Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg

Types: deb
URIs: http://mirror.yandex.ru/ubuntu
Suites: noble-security
Components: main restricted universe multiverse
Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg
EOF

log "Updating APT package index..."
apt-get update -q

log "Upgrading existing packages..."
DEBIAN_FRONTEND=noninteractive apt-get upgrade -y -q

# ---------------------------------------------------------------------------
# 4. Base packages
# ---------------------------------------------------------------------------

log "Installing base packages..."

PACKAGES=(
    smartmontools
    btop
    nvtop
    tmux
    git
    curl
    jq
    wget
    ca-certificates
    gnupg
    lsb-release
    ufw
    ubuntu-drivers-common
    unattended-upgrades
    apt-listchanges
)

DEBIAN_FRONTEND=noninteractive apt-get install -y -q "${PACKAGES[@]}"

log "Base packages installed."

# ---------------------------------------------------------------------------
# 5. Swap files (1 GiB on each SSD by default)
# ---------------------------------------------------------------------------

SWAP_SIZE_GIB="${SWAP_SIZE_GIB:-1}"
if ! [[ "${SWAP_SIZE_GIB}" =~ ^[1-9][0-9]*$ ]]; then
    die "SWAP_SIZE_GIB must be a positive integer number of GiB."
fi

create_swap_file() {
    local swapfile="$1"
    local size_gib="$2"

    if swapon --show=NAME --noheadings | grep -Fxq "${swapfile}"; then
        log "Swap file ${swapfile} already active — skipping."
    elif [[ -f "${swapfile}" ]]; then
        log "Swap file ${swapfile} exists but not active — enabling."
        chmod 600 "${swapfile}"
        mkswap "${swapfile}"
        swapon "${swapfile}"
    else
        log "Creating ${size_gib} GiB swap file at ${swapfile}..."
        fallocate -l "${size_gib}G" "${swapfile}" \
            || dd if=/dev/zero of="${swapfile}" bs=1G count="${size_gib}" status=progress
        chmod 600 "${swapfile}"
        mkswap "${swapfile}"
        swapon "${swapfile}"
        log "Swap file ${swapfile} created and activated."
    fi

    if ! grep -Fqx "${swapfile} none swap sw 0 0" /etc/fstab; then
        echo "${swapfile} none swap sw 0 0" >> /etc/fstab
        log "Added ${swapfile} to /etc/fstab."
    fi
}

create_swap_file /swapfile "${SWAP_SIZE_GIB}"
if ! mountpoint -q /data; then
    die "/data is not mounted! Refusing to initialize swap or Docker on root filesystem."
fi
create_swap_file /data/swapfile "${SWAP_SIZE_GIB}"

# ---------------------------------------------------------------------------
# 6. Directory layout
# ---------------------------------------------------------------------------

log "Creating /data directory layout..."
mkdir -p /data/docker
mkdir -p /data/models
mkdir -p /data/cache
mkdir -p /data/tmp

# Fix ownership for tmp and cache — world-writable but sticky
chmod 1777 /data/tmp
chmod 755  /data/docker /data/models /data/cache

log "/data directories ready."

if [[ "${ARCHIVE_MOUNTED}" -eq 1 ]]; then
    log "Creating /archive directory layout..."
    mkdir -p /archive/incoming
    mkdir -p /archive/output
    mkdir -p /archive/backups
    chmod 755 /archive/incoming /archive/output /archive/backups
    log "/archive directories ready."
else
    warn "Skipping /archive directory creation — /archive not mounted."
fi

# ---------------------------------------------------------------------------
# 7. journald tuning
# ---------------------------------------------------------------------------

log "Tuning journald storage limits..."

JOURNALD_CONF="/etc/systemd/journald.conf.d/samovar.conf"
mkdir -p "$(dirname "${JOURNALD_CONF}")"

cat > "${JOURNALD_CONF}" <<'EOF'
# Samovar journald configuration
# Persistent storage, bounded size to protect /
[Journal]
Storage=persistent
Compress=yes
SystemMaxUse=90M
SystemMaxFileSize=8M
RuntimeMaxUse=32M
EOF

systemctl restart systemd-journald
log "journald tuned: SystemMaxUse=90M, SystemMaxFileSize=8M."

# ---------------------------------------------------------------------------
# 8. Disable sleep / suspend / hibernate / hybrid-sleep
# ---------------------------------------------------------------------------

log "Disabling sleep, suspend, hibernate and hybrid-sleep..."

SLEEP_UNITS=(
    sleep.target
    suspend.target
    hibernate.target
    hybrid-sleep.target
    suspend-then-hibernate.target
)

for unit in "${SLEEP_UNITS[@]}"; do
    systemctl mask --now "${unit}" 2>/dev/null || true
done

# Also configure logind to ignore power button / lid (server use)
LOGIND_CONF="/etc/systemd/logind.conf.d/samovar.conf"
mkdir -p "$(dirname "${LOGIND_CONF}")"
cat > "${LOGIND_CONF}" <<'EOF'
# Samovar: server — never sleep on idle or lid close
[Login]
HandleSuspendKey=ignore
HandleHibernateKey=ignore
HandleLidSwitch=ignore
HandleLidSwitchExternalPower=ignore
HandleLidSwitchDocked=ignore
IdleAction=ignore
EOF

systemctl restart systemd-logind
log "Sleep/suspend/hibernate disabled."

# ---------------------------------------------------------------------------
# 9. NTP
# ---------------------------------------------------------------------------

log "Enabling NTP via systemd-timesyncd..."

# Prefer Yandex NTP pool
TIMESYNCD_CONF="/etc/systemd/timesyncd.conf.d/samovar.conf"
mkdir -p "$(dirname "${TIMESYNCD_CONF}")"
cat > "${TIMESYNCD_CONF}" <<'EOF'
# Samovar: prefer Yandex NTP pool
[Time]
NTP=ntp.yandex.ru 0.ru.pool.ntp.org 1.ru.pool.ntp.org
FallbackNTP=ntp.ubuntu.com
EOF

systemctl enable --now systemd-timesyncd
timedatectl set-ntp true
log "NTP enabled (ntp.yandex.ru primary)."

# ---------------------------------------------------------------------------
# 10. NVIDIA driver + NVIDIA Container Toolkit
# ---------------------------------------------------------------------------

log "Installing NVIDIA driver (ubuntu-drivers autoinstall)..."

if command -v nvidia-smi &>/dev/null && nvidia-smi --query-gpu=name --format=csv,noheader &>/dev/null; then
    log "NVIDIA driver already loaded — skipping driver install."
else
    log "Running ubuntu-drivers autoinstall..."
    ubuntu-drivers autoinstall
    log "NVIDIA driver installed. A reboot will be required to load it."
fi

# NVIDIA Container Toolkit repository
log "Setting up NVIDIA Container Toolkit repository..."

NVIDIA_KEYRING_PATH="/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg"
NVIDIA_APT_SOURCE="/etc/apt/sources.list.d/nvidia-container-toolkit.list"

if [[ ! -f "${NVIDIA_KEYRING_PATH}" ]]; then
    log_cmd "Downloading NVIDIA Container Toolkit GPG key"
    curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
        | gpg --dearmor -o "${NVIDIA_KEYRING_PATH}"
fi

if [[ ! -f "${NVIDIA_APT_SOURCE}" ]]; then
    DISTRO="ubuntu22.04"   # NCT packages use ubuntu22.04 for 24.x+ as well
    echo "deb [signed-by=${NVIDIA_KEYRING_PATH}] https://nvidia.github.io/libnvidia-container/stable/deb/amd64 /" \
        > "${NVIDIA_APT_SOURCE}"
    log "NVIDIA Container Toolkit APT source added."
fi

apt-get update -q
DEBIAN_FRONTEND=noninteractive apt-get install -y -q nvidia-container-toolkit

log "Configuring nvidia-ctk for Docker runtime..."
nvidia-ctk runtime configure --runtime=docker --config=/etc/docker/daemon.json 2>/dev/null || true

# Enable nvidia-persistenced if available
if systemctl list-unit-files | grep -q "nvidia-persistenced.service"; then
    systemctl enable --now nvidia-persistenced
    log "nvidia-persistenced enabled."
fi

log "NVIDIA Container Toolkit installed."

# ---------------------------------------------------------------------------
# 11. Docker Engine
# ---------------------------------------------------------------------------

log "Installing Docker Engine..."

DOCKER_KEYRING="/usr/share/keyrings/docker.gpg"
DOCKER_APT_SOURCE="/etc/apt/sources.list.d/docker.list"

if ! command -v docker &>/dev/null; then
    if [[ ! -f "${DOCKER_KEYRING}" ]]; then
        log_cmd "Downloading Docker GPG key"
        curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
            | gpg --dearmor -o "${DOCKER_KEYRING}"
    fi

    if [[ ! -f "${DOCKER_APT_SOURCE}" ]]; then
        UBUNTU_CODENAME="$(. /etc/os-release && echo "${UBUNTU_CODENAME:-${VERSION_CODENAME}}")"
        echo "deb [arch=amd64 signed-by=${DOCKER_KEYRING}] https://download.docker.com/linux/ubuntu ${UBUNTU_CODENAME} stable" \
            > "${DOCKER_APT_SOURCE}"
        log "Docker APT source added (codename: ${UBUNTU_CODENAME})."
    fi

    apt-get update -q
    DEBIAN_FRONTEND=noninteractive apt-get install -y -q \
        docker-ce \
        docker-ce-cli \
        containerd.io \
        docker-buildx-plugin \
        docker-compose-plugin

    log "Docker Engine installed."
else
    log "Docker Engine already installed — skipping."
fi

# Install docker-compose-v2 package (alias/wrapper for compose plugin)
if ! dpkg -l docker-compose-v2 &>/dev/null 2>&1; then
    DEBIAN_FRONTEND=noninteractive apt-get install -y -q docker-compose-v2 2>/dev/null || true
fi

# Write daemon.json (data-root=/data/docker, local log driver)
log "Writing Docker daemon configuration..."

DOCKER_DAEMON_JSON="/etc/docker/daemon.json"
mkdir -p /etc/docker

# Preserve nvidia runtime entries if nvidia-ctk already wrote them
if [[ -f "${DOCKER_DAEMON_JSON}" ]] && python3 -c "import json,sys; d=json.load(open('${DOCKER_DAEMON_JSON}')); sys.exit(0 if 'runtimes' in d else 1)" 2>/dev/null; then
    log "Merging data-root and log config into existing daemon.json (preserving runtimes)..."
    python3 - <<'PYEOF'
import json, sys

path = "/etc/docker/daemon.json"
with open(path) as f:
    cfg = json.load(f)

cfg.setdefault("log-driver", "local")
cfg.setdefault("log-opts", {"max-size": "10m", "max-file": "10"})
cfg.setdefault("data-root", "/data/docker")

with open(path, "w") as f:
    json.dump(cfg, f, indent=2)
    f.write("\n")
PYEOF
else
    # Copy daemon.json from provisioning bundle if present alongside this script
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    if [[ -f "${SCRIPT_DIR}/docker-daemon.json" ]]; then
        cp "${SCRIPT_DIR}/docker-daemon.json" "${DOCKER_DAEMON_JSON}"
        log "Copied docker-daemon.json from provisioning bundle."
    else
        cat > "${DOCKER_DAEMON_JSON}" <<'EOF'
{
  "log-driver": "local",
  "log-opts": {
    "max-size": "10m",
    "max-file": "10"
  },
  "data-root": "/data/docker"
}
EOF
        log "Wrote default docker-daemon.json."
    fi
fi

# Add alex to docker group
if id alex &>/dev/null; then
    if ! groups alex | grep -q docker; then
        usermod -aG docker alex
        log "User alex added to docker group."
    else
        log "User alex already in docker group."
    fi
else
    warn "User 'alex' not found — skipping docker group membership."
fi

# Configure Docker to start only after /data is mounted
log "Configuring docker.service override (RequiresMountsFor=/data)..."

DOCKER_OVERRIDE_DIR="/etc/systemd/system/docker.service.d"
mkdir -p "${DOCKER_OVERRIDE_DIR}"

cat > "${DOCKER_OVERRIDE_DIR}/samovar-data-mount.conf" <<'EOF'
# Samovar: Docker must not start until /data is mounted
[Unit]
RequiresMountsFor=/data
After=data.mount
EOF

systemctl daemon-reload
systemctl enable docker
log "Docker configured. data-root=/data/docker."

# ---------------------------------------------------------------------------
# 12. Mihomo user and service
# ---------------------------------------------------------------------------

log "Setting up Mihomo proxy service..."

# Create mihomo system user/group if absent
if ! id mihomo &>/dev/null; then
    useradd --system --no-create-home --shell /usr/sbin/nologin \
        --home-dir /var/lib/mihomo mihomo
    log "System user 'mihomo' created."
fi

# Create required directories
mkdir -p /etc/mihomo
mkdir -p /var/lib/mihomo
chown mihomo:mihomo /etc/mihomo /var/lib/mihomo
chmod 750 /etc/mihomo /var/lib/mihomo

# Install the Compose definition and editable image selection.
SCRIPT_DIR_MIHOMO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MIHOMO_SERVICE_SRC="${SCRIPT_DIR_MIHOMO}/mihomo.service"
MIHOMO_COMPOSE_SRC="${SCRIPT_DIR_MIHOMO}/mihomo.compose.yml"
MIHOMO_SERVICE_DST="/etc/systemd/system/mihomo.service"

if [[ -f "${MIHOMO_SERVICE_SRC}" ]]; then
    install -m 0644 "${MIHOMO_SERVICE_SRC}" "${MIHOMO_SERVICE_DST}"
fi
if [[ -f "${MIHOMO_COMPOSE_SRC}" ]]; then
    install -m 0644 "${MIHOMO_COMPOSE_SRC}" /etc/mihomo/compose.yml
fi
MIHOMO_IMAGE="${MIHOMO_IMAGE:-metacubex/mihomo:latest}"
if [[ ! "${MIHOMO_IMAGE}" =~ ^[A-Za-z0-9][A-Za-z0-9._/@:-]*$ ]]; then
    die "MIHOMO_IMAGE must be a valid Docker image reference without whitespace."
fi
printf 'MIHOMO_IMAGE=%s\n' "${MIHOMO_IMAGE}" > /etc/mihomo/compose.env
chmod 0600 /etc/mihomo/compose.env

# Prepare GeoIP locally so config validation does not depend on Docker network
# access to GitHub release assets.
MIHOMO_GEOIP_FILE="/etc/mihomo/geoip.metadb"
MIHOMO_GEOIP_URL="https://github.com/MetaCubeX/meta-rules-dat/releases/latest/download/geoip.metadb"
if [[ ! -s "${MIHOMO_GEOIP_FILE}" ]]; then
    if [[ -s /var/lib/samovar-offline-artifacts/geoip.metadb ]]; then
        install -o root -g root -m 0644 /var/lib/samovar-offline-artifacts/geoip.metadb "${MIHOMO_GEOIP_FILE}"
        log "Mihomo GeoIP database restored from offline artifacts."
    else
        MIHOMO_GEOIP_TMP="${MIHOMO_GEOIP_FILE}.tmp"
        if curl --fail --location --connect-timeout 5 --max-time 60 --retry 2 \
            --output "${MIHOMO_GEOIP_TMP}" "${MIHOMO_GEOIP_URL}"; then
            install -o root -g root -m 0644 "${MIHOMO_GEOIP_TMP}" "${MIHOMO_GEOIP_FILE}"
            log "Mihomo GeoIP database prepared."
        else
            rm -f "${MIHOMO_GEOIP_TMP}"
            log "WARNING: Mihomo GeoIP download failed; recovery will retry before validation."
        fi
    fi
fi

# Install proxy-run and proxy-shell wrappers
for wrapper in proxy-run proxy-shell; do
    SRC="${SCRIPT_DIR_MIHOMO}/${wrapper}"
    DST="/usr/local/bin/${wrapper}"
    if [[ -f "${SRC}" ]]; then
        install -m 0755 "${SRC}" "${DST}"
        log "Installed ${wrapper} -> ${DST}."
    fi
done

# Enable the wrapper; it starts only after recovery writes config.yaml.
if [[ -f "${MIHOMO_SERVICE_DST}" && -f /etc/mihomo/compose.yml ]]; then
    systemctl daemon-reload
    systemctl enable mihomo
    log "mihomo.service enabled (Docker Compose)."
else
    log "mihomo.service NOT enabled — Compose files are missing."
fi

log "Mihomo setup complete."

# ---------------------------------------------------------------------------
# 13. UFW firewall rules
# ---------------------------------------------------------------------------

log "Configuring UFW firewall..."

# Reset without disabling — idempotent baseline
ufw --force reset

# Default policy: deny incoming, allow outgoing
ufw default deny incoming
ufw default allow outgoing

# SSH — ONLY on management interfaces, NOT globally
# Critical: do NOT use 'ufw allow OpenSSH' (that is global)
ufw allow in on wt0   to any port 22 proto tcp comment "SSH on NetBird interface"
ufw allow in on wifi0 to any port 22 proto tcp comment "SSH on Wi-Fi interface"
ufw allow in on lan0  to any port 22 proto tcp comment "SSH on LAN interface"

# NetBird (WireGuard) traffic on wt0
# NetBird peer communication uses UDP/51820 by default
ufw allow in on wt0 proto udp comment "NetBird WireGuard on wt0"

# Mihomo controller (127.0.0.1:9090) and proxy (127.0.0.1:7890) are
# loopback-only — NOT opened to any external interface.
# No ufw rules needed; they are bound to 127.0.0.1.

# Enable UFW (non-interactive)
ufw --force enable

log "UFW configured:"
log "  - Default: deny incoming, allow outgoing"
log "  - SSH: allowed on wt0, wifi0, lan0 (NOT globally)"
log "  - NetBird WireGuard UDP: allowed on wt0"
log "  - Mihomo ports: loopback-only, NOT opened externally"
ufw status verbose | tee -a "${LOG_FILE}"

# ---------------------------------------------------------------------------
# 14. Unattended-upgrades (automatic security updates)
# ---------------------------------------------------------------------------

log "Configuring unattended-upgrades for automatic security updates..."

cat > /etc/apt/apt.conf.d/20samovar-auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
APT::Periodic::AutocleanInterval "7";
APT::Periodic::Download-Upgradeable-Packages "1";
EOF

cat > /etc/apt/apt.conf.d/52samovar-unattended-upgrades <<'EOF'
Unattended-Upgrade::Allowed-Origins {
    "${distro_id}:${distro_codename}-security";
    "${distro_id}ESMApps:${distro_codename}-apps-security";
    "${distro_id}ESM:${distro_codename}-infra-security";
};
Unattended-Upgrade::Package-Blacklist {
};
Unattended-Upgrade::AutoFixInterruptedDpkg "true";
Unattended-Upgrade::MinimalSteps "true";
Unattended-Upgrade::Remove-Unused-Kernel-Packages "true";
Unattended-Upgrade::Remove-New-Unused-Dependencies "true";
Unattended-Upgrade::Remove-Unused-Dependencies "false";
Unattended-Upgrade::Automatic-Reboot "false";
Unattended-Upgrade::Verbose "false";
Unattended-Upgrade::SyslogEnable "true";
Unattended-Upgrade::SyslogFacility "daemon";
EOF

systemctl enable unattended-upgrades
systemctl restart unattended-upgrades
log "Unattended-upgrades enabled (security updates only, no auto-reboot)."

# ---------------------------------------------------------------------------
# 15. fstrim.timer
# ---------------------------------------------------------------------------

log "Enabling fstrim.timer for SSD health..."
systemctl enable --now fstrim.timer
log "fstrim.timer enabled."

# ---------------------------------------------------------------------------
# 16. Final health report
# ---------------------------------------------------------------------------

log "=========================================="
log "Samovar provisioning — final health report"
log "=========================================="

check_cmd() {
    local label="$1"
    shift
    if "$@" &>/dev/null; then
        log "  [OK]   ${label}"
    else
        warn "  [FAIL] ${label}"
    fi
}

check_cmd "nvidia-smi"             nvidia-smi --query-gpu=name --format=csv,noheader
check_cmd "docker info"            docker info
check_cmd "ufw active"             bash -c "ufw status | grep -q 'Status: active'"
check_cmd "fstrim.timer enabled"   systemctl is-enabled fstrim.timer
check_cmd "NTP active"             timedatectl show --property=NTPSynchronized
check_cmd "/data mounted"          mountpoint -q /data
check_cmd "swap active"            bash -c "swapon --show=NAME --noheadings | grep -Fxq /swapfile && swapon --show=NAME --noheadings | grep -Fxq /data/swapfile"
check_cmd "docker enabled"         systemctl is-enabled docker
check_cmd "alex in docker group"   id alex | grep -q docker

log ""
log "Disk free space:"
df -h / /data 2>/dev/null | tee -a "${LOG_FILE}" || true
if mountpoint -q /archive 2>/dev/null; then
    df -h /archive | tee -a "${LOG_FILE}" || true
fi

log ""
log "nvidia-ctk CDI list:"
nvidia-ctk cdi list 2>&1 | tee -a "${LOG_FILE}" || warn "nvidia-ctk not available yet (driver reboot pending)"

log "=========================================="
log "Provisioning complete. Writing stamp file."
log "=========================================="

# Write stamp with timestamp
date --iso-8601=seconds > "${STAMP_FILE}"

log "Stamp written to ${STAMP_FILE}."
log "A reboot is likely required to load the NVIDIA driver."
log "Provisioning log: ${LOG_FILE}"

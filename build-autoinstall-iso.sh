#!/usr/bin/env bash
set -euo pipefail

WORK_DIR="$(cd "$(dirname "$0")" && pwd)"

# Windows / Git Bash / MSYS2 path compatibility for Docker volume mounts
DOCKER_WORK_DIR="$WORK_DIR"
if [[ "${OSTYPE:-}" == "msys" || "${OSTYPE:-}" == "cygwin" ]]; then
  export MSYS_NO_PATHCONV=1
  if command -v cygpath >/dev/null 2>&1; then
    DOCKER_WORK_DIR="$(cygpath -m "$WORK_DIR")"
  else
    DOCKER_WORK_DIR="$(cd "$WORK_DIR" && pwd -W 2>/dev/null || echo "$WORK_DIR")"
  fi
fi

# Space-delimited list of keys that appeared in .env (even with empty values).
# Used so shell-pre-set HOSTNAME (macOS/Linux) does not silently skip the prompt.
ENV_FILE_KEYS=" "

# Load optional .env from the project directory.
# Keys present in the file win over the process environment.
load_env_file() {
  local env_file="$1"
  [[ -f "$env_file" ]] || return 0

  echo "Loading environment from $env_file"
  local line key value
  while IFS= read -r line || [[ -n "$line" ]]; do
    # Strip CR (Windows line endings) and trim leading/trailing whitespace.
    line="${line//$'\r'/}"
    line="${line#"${line%%[![:space:]]*}"}"
    line="${line%"${line##*[![:space:]]}"}"

    # Skip blank lines and comments.
    [[ -z "$line" || "$line" == \#* ]] && continue

    # Optional "export " prefix.
    if [[ "$line" == export[[:space:]]* ]]; then
      line="${line#export}"
      line="${line#"${line%%[![:space:]]*}"}"
    fi

    [[ "$line" == *=* ]] || {
      echo "Warning: ignoring invalid .env line: $line" >&2
      continue
    }

    key="${line%%=*}"
    value="${line#*=}"
    key="${key%"${key##*[![:space:]]}"}"
    key="${key#"${key%%[![:space:]]*}"}"

    [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || {
      echo "Warning: ignoring invalid .env key: $key" >&2
      continue
    }

    # Strip matching single or double quotes around the value.
    if [[ "$value" =~ ^\"(.*)\"$ ]]; then
      value="${BASH_REMATCH[1]}"
    elif [[ "$value" =~ ^\'(.*)\'$ ]]; then
      value="${BASH_REMATCH[1]}"
    fi

    ENV_FILE_KEYS="${ENV_FILE_KEYS}${key} "
    export "$key=$value"
  done < "$env_file"
}

env_file_has() {
  [[ "$ENV_FILE_KEYS" == *" $1 "* ]]
}

# True when a non-empty value should skip the interactive prompt.
# HOSTNAME is only trusted from .env (or explicit shell export of TARGET_HOSTNAME),
# because shells usually pre-set HOSTNAME to the builder machine name.
configured() {
  local key="$1"
  # bash 3.2-safe indirect expansion
  local value
  value="$(eval "printf '%s' \"\${$key-}\"")"
  [[ -n "$value" ]] || return 1

  if [[ "$key" == "HOSTNAME" ]]; then
    env_file_has HOSTNAME || [[ -n "${TARGET_HOSTNAME:-}" ]]
    return
  fi

  return 0
}

load_env_file "$WORK_DIR/.env"

# Optional alias: TARGET_HOSTNAME avoids clashing with the shell's HOSTNAME.
if [[ -n "${TARGET_HOSTNAME:-}" ]]; then
  HOSTNAME="$TARGET_HOSTNAME"
fi

UBUNTU_VERSION="${UBUNTU_VERSION:-24.04.4}"

need() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "Error: '$1' is required."
    exit 1
  }
}

need docker
need curl

echo "This creates a FULLY UNATTENDED installer."
echo "It will erase the largest non-USB installation disk."
echo

# CPU architecture of the *target* machine (not necessarily this builder).
# Accepts common aliases; ISO and APT mirrors follow this choice.
if ! configured ARCH; then
  read -r -p "CPU architecture (amd64/arm64) [amd64]: " ARCH
  ARCH="${ARCH:-amd64}"
fi
case "${ARCH}" in
  amd64|x86_64|x64)
    ARCH=amd64
    ;;
  arm64|aarch64|arm)
    ARCH=arm64
    ;;
  *)
    echo "Error: unsupported ARCH '${ARCH}'."
    echo "Use amd64 (x86_64) or arm64 (aarch64)."
    exit 1
    ;;
esac
export ARCH

# LTS series path segment for download mirrors (24.04.4 → 24.04).
if [[ "$UBUNTU_VERSION" =~ ^([0-9]+\.[0-9]+) ]]; then
  UBUNTU_SERIES="${BASH_REMATCH[1]}"
else
  UBUNTU_SERIES="$UBUNTU_VERSION"
fi

ISO_NAME="ubuntu-${UBUNTU_VERSION}-live-server-${ARCH}.iso"
OUTPUT_ISO="${OUTPUT_ISO:-ubuntu-${UBUNTU_VERSION}-autoinstall-${ARCH}.iso}"

# An absolute output path must be mounted separately in Docker. Git Bash users
# may provide a Windows path (G:\Samovar\output.iso), so normalize it first.
if [[ ("${OSTYPE:-}" == msys* || "${OSTYPE:-}" == cygwin*) \
      && "$OUTPUT_ISO" =~ ^[A-Za-z]:[\\/].* ]] && command -v cygpath >/dev/null 2>&1; then
  OUTPUT_ISO="$(cygpath -u "$OUTPUT_ISO")"
fi

OUTPUT_ISO_PATH="$WORK_DIR/$OUTPUT_ISO"
OUTPUT_ISO_DIR="$(dirname "$OUTPUT_ISO_PATH")"
OUTPUT_ISO_NAME="$(basename "$OUTPUT_ISO_PATH")"
OUTPUT_ISO_CONTAINER_PATH="/work/$OUTPUT_ISO"
DOCKER_OUTPUT_MOUNT=()
if [[ "$OUTPUT_ISO" == /* ]]; then
  OUTPUT_ISO_PATH="$OUTPUT_ISO"
  OUTPUT_ISO_DIR="${OUTPUT_ISO%/*}"
  [[ -n "$OUTPUT_ISO_DIR" ]] || OUTPUT_ISO_DIR="/"
  OUTPUT_ISO_NAME="${OUTPUT_ISO##*/}"
  OUTPUT_ISO_CONTAINER_PATH="/output/$OUTPUT_ISO_NAME"
  mkdir -p "$OUTPUT_ISO_DIR"
  DOCKER_OUTPUT_DIR="$OUTPUT_ISO_DIR"
  if [[ "${OSTYPE:-}" == msys* || "${OSTYPE:-}" == cygwin* ]] && command -v cygpath >/dev/null 2>&1; then
    DOCKER_OUTPUT_DIR="$(cygpath -m "$OUTPUT_ISO_DIR")"
  fi
  DOCKER_OUTPUT_MOUNT=(-v "$DOCKER_OUTPUT_DIR:/output")
fi
OUTPUT_ISO_CHECKSUM_PATH="${OUTPUT_ISO_PATH}.sha256"

# ---------------------------------------------------------------------------
# ISO download mirror selection
#
# ISO_URL      — full URL to the .iso (wins over everything else)
# ISO_MIRROR   — auto | default | base URL | full .iso URL
#   auto       — probe popular CD mirrors in parallel, pick the fastest (default)
#   default    — official Canonical only (releases.ubuntu.com / cdimage.ubuntu.com)
#   https://…  — base (…/ubuntu-releases) or a complete .iso URL
#
# Probe runs only when the source ISO is missing (skipped if already downloaded).
# ---------------------------------------------------------------------------

official_iso_url() {
  if [[ "$ARCH" == "amd64" ]]; then
    printf '%s\n' "https://releases.ubuntu.com/${UBUNTU_SERIES}/${ISO_NAME}"
  else
    printf '%s\n' "https://cdimage.ubuntu.com/releases/${UBUNTU_SERIES}/release/${ISO_NAME}"
  fi
}

# Curated CD mirrors that host live-server ISOs (paths differ by arch).
iso_candidate_urls() {
  if [[ "$ARCH" == "amd64" ]]; then
    printf '%s\n' \
      "https://releases.ubuntu.com/${UBUNTU_SERIES}/${ISO_NAME}" \
      "https://mirror.yandex.ru/ubuntu-releases/${UBUNTU_SERIES}/${ISO_NAME}" \
      "https://ftp.halifax.rwth-aachen.de/ubuntu-releases/${UBUNTU_SERIES}/${ISO_NAME}" \
      "https://ftp.uni-stuttgart.de/ubuntu-releases/${UBUNTU_SERIES}/${ISO_NAME}" \
      "https://mirror.nl.leaseweb.net/ubuntu-releases/${UBUNTU_SERIES}/${ISO_NAME}" \
      "https://mirror.rackspace.com/ubuntu-releases/${UBUNTU_SERIES}/${ISO_NAME}" \
      "https://mirror.csclub.uwaterloo.ca/ubuntu-releases/${UBUNTU_SERIES}/${ISO_NAME}" \
      "https://mirrors.ocf.berkeley.edu/ubuntu-releases/${UBUNTU_SERIES}/${ISO_NAME}" \
      "https://mirror.math.princeton.edu/pub/ubuntu-iso/${UBUNTU_SERIES}/${ISO_NAME}" \
      "https://mirror.aarnet.edu.au/pub/ubuntu/releases/${UBUNTU_SERIES}/${ISO_NAME}" \
      "https://ftp.jaist.ac.jp/pub/Linux/ubuntu-releases/${UBUNTU_SERIES}/${ISO_NAME}" \
      "https://ftp.riken.jp/Linux/ubuntu-releases/${UBUNTU_SERIES}/${ISO_NAME}" \
      "https://mirror.nju.edu.cn/ubuntu-releases/${UBUNTU_SERIES}/${ISO_NAME}" \
      "https://mirrors.aliyun.com/ubuntu-releases/${UBUNTU_SERIES}/${ISO_NAME}"
  else
    # arm64 lives under the cdimage tree; fewer public mirrors carry it.
    printf '%s\n' \
      "https://cdimage.ubuntu.com/releases/${UBUNTU_SERIES}/release/${ISO_NAME}" \
      "https://ftp.jaist.ac.jp/pub/Linux/ubuntu-cdimage/releases/${UBUNTU_SERIES}/release/${ISO_NAME}"
  fi
}

format_speed() {
  awk -v s="${1:-0}" 'BEGIN {
    if (s+0 >= 1048576) printf "%.1f MB/s", s/1048576
    else if (s+0 >= 1024) printf "%.0f KB/s", s/1024
    else printf "%.0f B/s", s+0
  }'
}

# Probe one URL: download the first ~2 MiB and record speed_download.
# Writes "SPEED URL" to $2 (SPEED is 0 on failure). Always returns 0.
probe_iso_mirror() {
  local url="$1"
  local out="$2"
  local result code speed

  result="$(
    curl -fsSL --range 0-2097151 \
      --connect-timeout 3 \
      --max-time 12 \
      -o /dev/null \
      -w '%{http_code} %{speed_download}' \
      "$url" 2>/dev/null
  )" || {
    printf '0 %s\n' "$url" >"$out"
    return 0
  }

  code="${result%% *}"
  speed="${result#* }"
  if [[ "$code" == "200" || "$code" == "206" ]] && [[ -n "$speed" ]]; then
    printf '%s %s\n' "$speed" "$url" >"$out"
  else
    printf '0 %s\n' "$url" >"$out"
  fi
}

# Parallel speed-test. Sets SELECTED_ISO_URL on success (return 0).
select_fastest_iso_url() {
  local tmpdir i url best_speed best_url speed candidate pid
  local pids=""

  SELECTED_ISO_URL=""
  tmpdir="$(mktemp -d "${TMPDIR:-/tmp}/iso-mirror.XXXXXX")"
  i=0
  # bash 3.2: avoid process substitution + arrays where possible
  while IFS= read -r url; do
    [[ -n "$url" ]] || continue
    probe_iso_mirror "$url" "$tmpdir/$i" &
    pids="$pids $!"
    i=$((i + 1))
  done <<EOF
$(iso_candidate_urls)
EOF

  if [[ "$i" -eq 0 ]]; then
    rm -rf "$tmpdir"
    return 1
  fi

  set +e
  for pid in $pids; do
    wait "$pid"
  done
  set -e

  echo "ISO mirror probe results:"
  best_speed=0
  best_url=""
  for f in "$tmpdir"/*; do
    [[ -f "$f" ]] || continue
    read -r speed candidate <"$f" || true
    speed="$(awk -v s="${speed:-0}" 'BEGIN { printf "%.0f", s+0 }')"
    if [[ "$speed" -gt 0 ]]; then
      printf '  %-10s  %s\n' "$(format_speed "$speed")" "$candidate"
    else
      printf '  %-10s  %s\n' "fail" "$candidate"
    fi
    if [[ "$speed" -gt "$best_speed" ]]; then
      best_speed="$speed"
      best_url="$candidate"
    fi
  done
  rm -rf "$tmpdir"

  if [[ -n "$best_url" && "$best_speed" -gt 0 ]]; then
    echo "Selected: $(format_speed "$best_speed")  $best_url"
    SELECTED_ISO_URL="$best_url"
    return 0
  fi
  return 1
}

# Set ISO_URL from ISO_URL / ISO_MIRROR / auto probe.
resolve_iso_url() {
  local mirror="$1"
  local base

  if [[ -n "${ISO_URL:-}" ]]; then
    return 0
  fi

  case "$mirror" in
    default)
      ISO_URL="$(official_iso_url)"
      return 0
      ;;
    auto|"")
      echo "Probing Ubuntu ISO mirrors for best download speed (${ARCH})..."
      if select_fastest_iso_url; then
        ISO_URL="$SELECTED_ISO_URL"
        return 0
      fi
      ISO_URL="$(official_iso_url)"
      echo "Mirror probe found no working host; using official: $ISO_URL"
      return 0
      ;;
  esac

  # Full .iso URL pasted into ISO_MIRROR.
  if [[ "$mirror" == *.iso ]]; then
    ISO_URL="$mirror"
    return 0
  fi

  base="${mirror%/}"
  if [[ "$ARCH" == "amd64" ]]; then
    ISO_URL="${base}/${UBUNTU_SERIES}/${ISO_NAME}"
  else
    # Custom base for arm64: cdimage-style tree.
    if [[ "$base" == */releases ]]; then
      ISO_URL="${base}/${UBUNTU_SERIES}/release/${ISO_NAME}"
    else
      ISO_URL="${base}/releases/${UBUNTU_SERIES}/release/${ISO_NAME}"
    fi
  fi
}

# Detect Samovar mode
SAMOVAR_MODE="${SAMOVAR_MODE:-}"
if [[ -z "$SAMOVAR_MODE" && -f "$WORK_DIR/samovar-config.json" ]]; then
  SAMOVAR_MODE="samovar"
fi
export SAMOVAR_MODE

DEFAULT_HOSTNAME="friend-server"
DEFAULT_USERNAME="server"
if [[ "$SAMOVAR_MODE" == "samovar" ]]; then
  DEFAULT_HOSTNAME="samovar"
  DEFAULT_USERNAME="alex"
fi

if ! configured HOSTNAME; then
  read -r -p "Hostname [$DEFAULT_HOSTNAME]: " HOSTNAME
  HOSTNAME="${HOSTNAME:-$DEFAULT_HOSTNAME}"
fi
export HOSTNAME

if ! configured USERNAME; then
  read -r -p "Linux username [$DEFAULT_USERNAME]: " USERNAME
  USERNAME="${USERNAME:-$DEFAULT_USERNAME}"
fi
export USERNAME

# Must match render-autoinstall.py (safe for /etc/sudoers.d).
if ! [[ "$USERNAME" =~ ^[a-z_][a-z0-9_-]*$ ]]; then
  echo "Error: invalid username '$USERNAME'."
  echo "Use lowercase letters, digits, underscore, or hyphen (e.g. server)."
  exit 1
fi

if ! configured PASSWORD; then
  while true; do
    read -r -s -p "Console password (sudo is passwordless): " PASSWORD
    echo
    read -r -s -p "Repeat password: " PASSWORD_2
    echo
    [[ -n "$PASSWORD" ]] || { echo "Password cannot be empty."; continue; }
    [[ "$PASSWORD" == "$PASSWORD_2" ]] || { echo "Passwords do not match."; continue; }
    break
  done
fi
export PASSWORD

if ! configured SSH_PUBLIC_KEY; then
  DEFAULT_SSH_KEY=""
  for ssh_dir in "$HOME/.ssh" "${USERPROFILE:-}/.ssh"; do
    [[ -n "$ssh_dir" && -d "$ssh_dir" ]] || continue
    if [[ -z "$DEFAULT_SSH_KEY" && -f "$ssh_dir/id_ed25519.pub" ]]; then
      DEFAULT_SSH_KEY="$(cat "$ssh_dir/id_ed25519.pub")"
    elif [[ -z "$DEFAULT_SSH_KEY" && -f "$ssh_dir/id_ecdsa.pub" ]]; then
      DEFAULT_SSH_KEY="$(cat "$ssh_dir/id_ecdsa.pub")"
    elif [[ -z "$DEFAULT_SSH_KEY" && -f "$ssh_dir/id_rsa.pub" ]]; then
      DEFAULT_SSH_KEY="$(cat "$ssh_dir/id_rsa.pub")"
    fi
  done

  if [[ -n "$DEFAULT_SSH_KEY" ]]; then
    echo "Found SSH public key:"
    echo "$DEFAULT_SSH_KEY"
    read -r -p "Use it? [Y/n]: " USE_DEFAULT_KEY
    if [[ "${USE_DEFAULT_KEY:-Y}" =~ ^[Nn]$ ]]; then
      read -r -p "Paste SSH public key: " SSH_PUBLIC_KEY
    else
      SSH_PUBLIC_KEY="$DEFAULT_SSH_KEY"
    fi
  else
    read -r -p "Paste SSH public key: " SSH_PUBLIC_KEY
  fi
fi
export SSH_PUBLIC_KEY

if ! [[ "$SSH_PUBLIC_KEY" =~ ^(ssh-|ecdsa-|sk-) ]]; then
  echo "Error: the SSH public key should start with a valid OpenSSH key type (ssh-ed25519, ecdsa-sha2-nistp256, ssh-rsa, etc.)."
  exit 1
fi

if [[ "$SAMOVAR_MODE" == "samovar" && -z "${NETBIRD_SETUP_KEY:-}" ]]; then
  NETBIRD_SETUP_KEY="samovar-managed-via-config"
fi

if ! configured NETBIRD_SETUP_KEY; then
  read -r -s -p "NetBird ONE-OFF setup key: " NETBIRD_SETUP_KEY
  echo
fi
export NETBIRD_SETUP_KEY

[[ -n "$NETBIRD_SETUP_KEY" ]] || {
  echo "Error: NetBird setup key cannot be empty."
  exit 1
}

echo
echo "Target architecture: ${ARCH}"
echo "Source ISO:          ${ISO_NAME}"

if [[ ! -f "$WORK_DIR/$ISO_NAME" ]]; then
  ISO_MIRROR="${ISO_MIRROR:-auto}"
  resolve_iso_url "$ISO_MIRROR"
  echo
  echo "Downloading Ubuntu Server ${UBUNTU_VERSION} (${ARCH})..."
  echo "  $ISO_URL"
  # Resume partial downloads; fail on HTTP errors.
  curl -fL --progress-bar -C - "$ISO_URL" -o "$WORK_DIR/$ISO_NAME"
else
  echo "Using existing $ISO_NAME"
fi

# APT mirror settings (optional; defaults keep Subiquity geoip country-mirror).
APT_REGION="${APT_REGION:-auto}"
APT_MIRROR="${APT_MIRROR:-}"
APT_SECURITY_MIRROR="${APT_SECURITY_MIRROR:-}"
APT_FALLBACK="${APT_FALLBACK:-offline-install}"
NETWORK_INTERFACE="${NETWORK_INTERFACE:-both}"
DISK_SERIAL_PREFIX="${DISK_SERIAL_PREFIX:-}"
SWAP_SIZE_GIB="${SWAP_SIZE_GIB:-1}"
NOTIFY_TOPIC="${NOTIFY_TOPIC:-samovar_test}"
MIHOMO_IMAGE="${MIHOMO_IMAGE:-metacubex/mihomo:latest}"
OFFLINE_BUNDLE_REFRESH="${OFFLINE_BUNDLE_REFRESH:-auto}"
OFFLINE_BUNDLE_CACHE="${OFFLINE_BUNDLE_CACHE:-offline/packages/${UBUNTU_VERSION}-${ARCH}}"
if [[ ! "$MIHOMO_IMAGE" =~ ^[A-Za-z0-9][A-Za-z0-9._/@:-]*$ ]]; then
  echo "Error: MIHOMO_IMAGE must be a valid Docker image reference without whitespace." >&2
  exit 1
fi
if [[ "$OFFLINE_BUNDLE_REFRESH" != "auto" && "$OFFLINE_BUNDLE_REFRESH" != "never" ]]; then
  echo "Error: OFFLINE_BUNDLE_REFRESH must be auto or never." >&2
  exit 1
fi
if [[ "$OFFLINE_BUNDLE_CACHE" == /* || "$OFFLINE_BUNDLE_CACHE" == *"../"* || "$OFFLINE_BUNDLE_CACHE" == ".." ]]; then
  echo "Error: OFFLINE_BUNDLE_CACHE must be a relative path inside the project." >&2
  exit 1
fi
export APT_REGION APT_MIRROR APT_SECURITY_MIRROR APT_FALLBACK NETWORK_INTERFACE DISK_SERIAL_PREFIX SWAP_SIZE_GIB NOTIFY_TOPIC MIHOMO_IMAGE OFFLINE_BUNDLE_REFRESH OFFLINE_BUNDLE_CACHE

echo
echo "APT mirrors: region=${APT_REGION}"
if [[ -n "$APT_MIRROR" ]]; then
  echo "             custom=${APT_MIRROR}"
fi
if [[ -n "$APT_SECURITY_MIRROR" ]]; then
  echo "             security=${APT_SECURITY_MIRROR}"
fi
echo "             fallback=${APT_FALLBACK}"
echo
echo "Offline bundle: refresh=${OFFLINE_BUNDLE_REFRESH}"
echo "                cache=${OFFLINE_BUNDLE_CACHE}"
echo "Preparing cached offline APT bundle..."

docker run --rm \
  --platform "linux/${ARCH}" \
  -e OFFLINE_BUNDLE_CACHE="$OFFLINE_BUNDLE_CACHE" \
  -e OFFLINE_BUNDLE_REFRESH="$OFFLINE_BUNDLE_REFRESH" \
  -v "$DOCKER_WORK_DIR:/work" \
  -w /work \
  "ubuntu:${UBUNTU_SERIES}" bash -euc '
    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq dpkg-dev python3 >/dev/null
    bash /work/offline/build-apt-bundle.sh \
      /work/offline/packages.lock.json \
      "/work/$OFFLINE_BUNDLE_CACHE" \
      "$OFFLINE_BUNDLE_REFRESH"
  '

echo "Generating password hash and autoinstall.yaml..."

docker run --rm \
  -e HOSTNAME="$HOSTNAME" \
  -e USERNAME="$USERNAME" \
  -e PASSWORD="$PASSWORD" \
  -e SSH_PUBLIC_KEY="$SSH_PUBLIC_KEY" \
  -e NETBIRD_SETUP_KEY="$NETBIRD_SETUP_KEY" \
  -e ARCH="$ARCH" \
  -e APT_REGION="$APT_REGION" \
  -e APT_MIRROR="$APT_MIRROR" \
  -e APT_SECURITY_MIRROR="$APT_SECURITY_MIRROR" \
  -e APT_FALLBACK="$APT_FALLBACK" \
  -e NETWORK_INTERFACE="$NETWORK_INTERFACE" \
  -e DISK_SERIAL_PREFIX="$DISK_SERIAL_PREFIX" \
  -e SWAP_SIZE_GIB="$SWAP_SIZE_GIB" \
  -e NOTIFY_TOPIC="$NOTIFY_TOPIC" \
  -e MIHOMO_IMAGE="$MIHOMO_IMAGE" \
  -e SAMOVAR_MODE="${SAMOVAR_MODE:-}" \
  -e SAMOVAR_CONFIG_FILE="${SAMOVAR_CONFIG_FILE:-samovar-config.json}" \
  -e ALLOWED_SIGNERS="${ALLOWED_SIGNERS:-}" \
  -e SSH_PUBLIC_KEYS="${SSH_PUBLIC_KEYS:-$SSH_PUBLIC_KEY}" \
  -e SUDO_NOPASSWD="${SUDO_NOPASSWD:-true}" \
  -v "$DOCKER_WORK_DIR:/work" \
  -w /work \
  ubuntu:24.04 bash -euc '
    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq openssl python3 python3-yaml python3-jsonschema >/dev/null

    export PASSWORD_HASH
    PASSWORD_HASH="$(openssl passwd -6 "$PASSWORD")"

    output="$(mktemp /work/.autoinstall.yaml.XXXXXX)"
    trap "rm -f \"$output\"" EXIT
    python3 /work/render-autoinstall.py > "$output"
    chmod 600 "$output"
    mv "$output" /work/autoinstall.yaml
    trap - EXIT
  '

echo "Building bootable ISO..."

rm -f "$OUTPUT_ISO_PATH"

docker run --rm \
  -e ISO_NAME="$ISO_NAME" \
  -e OUTPUT_ISO_PATH="$OUTPUT_ISO_CONTAINER_PATH" \
  -e NETWORK_INTERFACE="$NETWORK_INTERFACE" \
  -e NOTIFY_TOPIC="$NOTIFY_TOPIC" \
  -e MIHOMO_IMAGE="$MIHOMO_IMAGE" \
  -e OFFLINE_BUNDLE_CACHE="$OFFLINE_BUNDLE_CACHE" \
  -v "$DOCKER_WORK_DIR:/work" \
  "${DOCKER_OUTPUT_MOUNT[@]}" \
  ubuntu:24.04 bash -euc '
    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq xorriso python3 python3-yaml >/dev/null

    mkdir -p /tmp/iso-build

    xorriso \
      -osirrox on \
      -indev "/work/$ISO_NAME" \
      -extract /boot/grub/grub.cfg /tmp/iso-build/grub.cfg \
      -extract /boot/grub/loopback.cfg /tmp/iso-build/loopback.cfg \
      >/dev/null 2>&1

    python3 /work/patch-grub.py \
      /tmp/iso-build/grub.cfg \
      /tmp/iso-build/grub-patched.cfg

    python3 /work/patch-grub.py \
      /tmp/iso-build/loopback.cfg \
      /tmp/iso-build/loopback-patched.cfg

    python3 /work/validate-autoinstall-iso.py \
      /work/autoinstall.yaml \
      /tmp/iso-build/grub-patched.cfg \
      /tmp/iso-build/loopback-patched.cfg

    xorriso \
      -indev "/work/$ISO_NAME" \
      -outdev "$OUTPUT_ISO_PATH" \
      -map /tmp/iso-build/grub-patched.cfg /boot/grub/grub.cfg \
      -map /tmp/iso-build/loopback-patched.cfg /boot/grub/loopback.cfg \
      -map /work/autoinstall.yaml /autoinstall.yaml \
      -map "/work/$OFFLINE_BUNDLE_CACHE" /samovar-offline-apt \
      -boot_image any replay

    xorriso \
      -indev "$OUTPUT_ISO_PATH" \
      -find /autoinstall.yaml -exec report_lba -- \
      >/dev/null

    xorriso \
      -osirrox on \
      -indev "$OUTPUT_ISO_PATH" \
      -extract /autoinstall.yaml /tmp/iso-build/embedded-autoinstall.yaml \
      -extract /samovar-offline-apt/Packages.gz /tmp/iso-build/offline-Packages.gz \
      -extract /boot/grub/grub.cfg /tmp/iso-build/embedded-grub.cfg \
      -extract /boot/grub/loopback.cfg /tmp/iso-build/embedded-loopback.cfg \
      >/dev/null 2>&1

    python3 /work/validate-autoinstall-iso.py \
      /tmp/iso-build/embedded-autoinstall.yaml \
      /tmp/iso-build/embedded-grub.cfg \
      /tmp/iso-build/embedded-loopback.cfg
  '

echo "Writing SHA-256 checksum..."
if command -v sha256sum >/dev/null 2>&1; then
  (cd "$OUTPUT_ISO_DIR" && sha256sum "$OUTPUT_ISO_NAME" > "${OUTPUT_ISO_NAME}.sha256")
elif command -v shasum >/dev/null 2>&1; then
  (cd "$OUTPUT_ISO_DIR" && shasum -a 256 "$OUTPUT_ISO_NAME" > "${OUTPUT_ISO_NAME}.sha256")
elif command -v certutil.exe >/dev/null 2>&1; then
  (cd "$OUTPUT_ISO_DIR" && certutil.exe -hashfile "$OUTPUT_ISO_NAME" SHA256 | awk 'NR==2 {print tolower($0) "  '"$OUTPUT_ISO_NAME"'"}' > "${OUTPUT_ISO_NAME}.sha256")
elif command -v openssl >/dev/null 2>&1; then
  (cd "$OUTPUT_ISO_DIR" && openssl dgst -sha256 -r "$OUTPUT_ISO_NAME" > "${OUTPUT_ISO_NAME}.sha256")
fi

echo
echo "Done:"
echo "  $OUTPUT_ISO_PATH"
if [[ -f "$OUTPUT_ISO_CHECKSUM_PATH" ]]; then
  echo "  $OUTPUT_ISO_CHECKSUM_PATH"
fi
echo
echo "Write it to a USB drive with Balena Etcher, Rufus, or Raspberry Pi Imager."
if [[ "$SAMOVAR_MODE" != "samovar" ]]; then
  echo "After the server appears in NetBird, delete or revoke the one-off setup key."
fi

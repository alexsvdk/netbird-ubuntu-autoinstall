#!/usr/bin/env bash
set -euo pipefail

# Resolve a complete Ubuntu + external APT dependency set into a persistent
# download cache, then publish it as a signed local repository. The generated
# lock records the exact .deb filename, architecture, version, and SHA-256.

SEEDS_FILE=${1:?usage: build-apt-bundle.sh SEEDS_FILE LOCK_FILE CACHE_DIR REFRESH_MODE}
LOCK_FILE=${2:?usage: build-apt-bundle.sh SEEDS_FILE LOCK_FILE CACHE_DIR REFRESH_MODE}
CACHE_DIR=${3:?usage: build-apt-bundle.sh SEEDS_FILE LOCK_FILE CACHE_DIR REFRESH_MODE}
REFRESH_MODE=${4:-auto}

case "$REFRESH_MODE" in
  auto|never) ;;
  *)
    echo "Error: OFFLINE_BUNDLE_REFRESH must be auto or never." >&2
    exit 1
    ;;
esac

command -v python3 >/dev/null

DOWNLOADS="$CACHE_DIR/downloads"
REPOSITORY="$CACHE_DIR/repository"
KEY_HOME="$CACHE_DIR/signing-key"
WORK_DIR="$(mktemp -d)"
trap 'rm -rf "$WORK_DIR"' EXIT

read_target() {
  python3 - "$SEEDS_FILE" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as source:
    target = json.load(source)["target"]
print("|".join((target["release"], target["codename"], target["architecture"])))
PY
}

IFS='|' read -r TARGET_RELEASE TARGET_CODENAME TARGET_ARCH <<EOF
$(read_target)
EOF

if [[ "${ARCH:-$TARGET_ARCH}" != "$TARGET_ARCH" ]]; then
  echo "Error: bundle target architecture is $TARGET_ARCH, but ARCH is ${ARCH:-unset}." >&2
  exit 1
fi
if [[ -n "${UBUNTU_SERIES:-}" && "$UBUNTU_SERIES" != "$TARGET_RELEASE" ]]; then
  echo "Error: ISO release $UBUNTU_SERIES does not match bundle target $TARGET_RELEASE ($TARGET_CODENAME)." >&2
  exit 1
fi

verify_locked_repository() {
  python3 - "$LOCK_FILE" "$REPOSITORY" "$TARGET_ARCH" <<'PY'
import hashlib
import json
import sys
from pathlib import Path

lock_path = Path(sys.argv[1])
repository = Path(sys.argv[2])
cli_target_arch = sys.argv[3] if len(sys.argv) > 3 and sys.argv[3] else None

try:
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError) as error:
    raise SystemExit(f"Error: cannot read offline lock: {error}")

if lock.get("schema") != 3 or lock.get("state") != "locked":
    raise SystemExit("Error: offline lock is not generated; rerun with OFFLINE_BUNDLE_REFRESH=auto.")
if not lock.get("packages"):
    raise SystemExit("Error: offline lock contains no packages.")

metadata = lock.get("metadata")
if not isinstance(metadata, dict) or not metadata:
    raise SystemExit("Error: offline lock metadata is missing or empty.")

target_arch = cli_target_arch or (lock.get("target") or {}).get("architecture")
if not target_arch:
    raise SystemExit("Error: target architecture unknown for metadata verification.")

expected_meta_keys = {
    "dists/samovar/InRelease",
    "dists/samovar/Release",
    f"dists/samovar/main/binary-{target_arch}/Packages",
    f"dists/samovar/main/binary-{target_arch}/Packages.gz",
    "samovar-offline-archive-keyring.gpg",
}
actual_meta_keys = set(metadata.keys())
if actual_meta_keys != expected_meta_keys:
    missing = expected_meta_keys - actual_meta_keys
    extra = actual_meta_keys - expected_meta_keys
    msg = []
    if missing:
        msg.append(f"missing {sorted(missing)}")
    if extra:
        msg.append(f"unexpected {sorted(extra)}")
    raise SystemExit(f"Error: locked metadata keys mismatch: {'; '.join(msg)}")

for rel_path, expected_hash in metadata.items():
    meta_file = repository / rel_path
    if not meta_file.is_file():
        raise SystemExit(f"Error: locked metadata file missing: {rel_path}")
    if hashlib.sha256(meta_file.read_bytes()).hexdigest() != expected_hash:
        raise SystemExit(f"Error: metadata SHA-256 mismatch: {rel_path}")

locked_paths = set()
for package in lock.get("packages", []):
    path = repository / package["path"]
    if not path.is_file():
        raise SystemExit(f"Error: locked package is missing: {package['path']}")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != package["sha256"]:
        raise SystemExit(f"Error: SHA-256 mismatch: {package['path']}")
    locked_paths.add(package["path"])

repo_debs = {p.relative_to(repository).as_posix() for p in repository.rglob("*.deb")}
unlisted = repo_debs - locked_paths
if unlisted:
    raise SystemExit(f"Error: unlisted .deb files in offline repository: {sorted(unlisted)}")
PY
}

if [[ "$REFRESH_MODE" == "never" ]]; then
  verify_locked_repository
  echo "Using verified offline APT bundle: $REPOSITORY"
  exit 0
fi

command -v apt-get >/dev/null
command -v apt-ftparchive >/dev/null
command -v dpkg-deb >/dev/null
command -v gpg >/dev/null

if ! command -v curl >/dev/null 2>&1; then
  DEBIAN_FRONTEND=noninteractive apt-get update -qq && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq ca-certificates curl gnupg >/dev/null
fi
command -v curl >/dev/null

mkdir -p "$DOWNLOADS/partial" "$CACHE_DIR"

write_ubuntu_sources() {
  local mirror="$1"
  python3 - "$SEEDS_FILE" "$mirror" /etc/apt/sources.list.d/samovar-ubuntu.list <<'PY'
import json
import sys

seeds_path, mirror, output = sys.argv[1:]
with open(seeds_path, encoding="utf-8") as source:
    target = json.load(source)["target"]
components = " ".join(target["components"])
with open(output, "w", encoding="utf-8") as destination:
    for suite in target["suites"]:
        destination.write(f"deb {mirror} {suite} {components}\n")
PY
}

rm -f /etc/apt/sources.list /etc/apt/sources.list.d/*.list /etc/apt/sources.list.d/*.sources
selected_mirror=""
while IFS= read -r mirror; do
  write_ubuntu_sources "$mirror"
  if apt-get update -qq; then
    selected_mirror="$mirror"
    break
  fi
done <<EOF
$(python3 - "$SEEDS_FILE" <<'PY'
import json
import sys
with open(sys.argv[1], encoding="utf-8") as source:
    print("\n".join(json.load(source)["ubuntu_mirrors"]))
PY
)
EOF

if [[ -z "$selected_mirror" ]]; then
  echo "Error: no configured Ubuntu mirror provided Resolute package metadata." >&2
  exit 1
fi

DEBIAN_FRONTEND=noninteractive apt-get install -y -qq ca-certificates curl gnupg >/dev/null

add_external_sources() {
  python3 - "$SEEDS_FILE" <<'PY' | while IFS=$'\x1f' read -r name repository suite component key_url; do
import json
import sys

with open(sys.argv[1], encoding="utf-8") as source:
    repositories = json.load(source)["external_apt"]
for name, repository in repositories.items():
    print("\x1f".join((name, repository["repository"], repository["suite"], repository["component"], repository["key_url"])))
PY
    keyring="/usr/share/keyrings/samovar-${name}.gpg"
    curl -fsSL "$key_url" | gpg --dearmor --yes -o "$keyring"
    if [[ "$suite" == "/" ]]; then
      printf 'deb [signed-by=%s] %s /\n' "$keyring" "$repository" >"/etc/apt/sources.list.d/samovar-${name}.list"
    else
      printf 'deb [signed-by=%s] %s %s %s\n' "$keyring" "$repository" "$suite" "$component" >"/etc/apt/sources.list.d/samovar-${name}.list"
    fi
  done
}

add_external_sources
apt-get update -qq

ROOTS_FILE="$WORK_DIR/roots.txt"
python3 - "$SEEDS_FILE" >"$ROOTS_FILE" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as source:
    seeds = json.load(source)
for package in seeds["ubuntu_packages"]:
    print(package)
for repository in seeds["external_apt"].values():
    for package in repository["packages"]:
        print(package)
PY

STATUS_FILE="$WORK_DIR/status"
: >"$STATUS_FILE"
APT_OPTIONS=(
  -o "Dir::State::status=$STATUS_FILE"
  -o "Dir::Cache::archives=$DOWNLOADS"
  -o "Dir::Cache::archives::partial=$DOWNLOADS/partial"
)

apt-get "${APT_OPTIONS[@]}" --download-only --yes install $(tr '\n' ' ' <"$ROOTS_FILE")
apt-get "${APT_OPTIONS[@]}" --simulate install $(tr '\n' ' ' <"$ROOTS_FILE") >"$WORK_DIR/simulation.txt"

python3 - "$WORK_DIR/simulation.txt" "$DOWNLOADS" >"$WORK_DIR/selected.json" <<'PY'
import json
import re
import subprocess
import sys
from pathlib import Path

simulation, downloads = map(Path, sys.argv[1:])
selected = set()
for line in simulation.read_text(encoding="utf-8").splitlines():
    match = re.match(r"Inst\s+(\S+?)(?::\S+)?\s+\(([^ )]+)", line)
    if match:
        selected.add(match.groups())

packages = []
for deb in sorted(downloads.glob("*.deb")):
    fields = subprocess.check_output(
        ["dpkg-deb", "-f", str(deb), "Package", "Version", "Architecture"],
        text=True,
    ).splitlines()
    if len(fields) != 3 or (fields[0], fields[1]) not in selected:
        continue
    packages.append({"name": fields[0], "version": fields[1], "arch": fields[2], "source": str(deb)})

missing = sorted(selected - {(package["name"], package["version"]) for package in packages})
if missing:
    raise SystemExit(f"Error: downloaded package closure is incomplete: {missing}")
print(json.dumps(packages, sort_keys=True))
PY

STAGING="$WORK_DIR/repository"
mkdir -p "$STAGING/pool/ubuntu" "$STAGING/pool/netbird" "$STAGING/pool/nvidia-container-toolkit"
python3 - "$SEEDS_FILE" "$WORK_DIR/selected.json" "$STAGING" >"$WORK_DIR/locked-packages.json" <<'PY'
import hashlib
import json
import shutil
import sys
from pathlib import Path

seeds_path, selected_path, staging_path = map(Path, sys.argv[1:])
seeds = json.loads(seeds_path.read_text(encoding="utf-8"))
selected = json.loads(selected_path.read_text(encoding="utf-8"))
staging = staging_path
external = {
    package: name
    for name, config in seeds["external_apt"].items()
    for package in config["packages"]
}
locked = []
for package in selected:
    bucket = external.get(package["name"], "ubuntu")
    destination = staging / "pool" / bucket / Path(package["source"]).name
    shutil.copy2(package["source"], destination)
    locked.append({
        "name": package["name"],
        "version": package["version"],
        "arch": package["arch"],
        "filename": destination.name,
        "path": str(destination.relative_to(staging)),
        "sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
    })
print(json.dumps(sorted(locked, key=lambda item: (item["name"], item["version"], item["arch"])), indent=2))
PY

mkdir -p "$STAGING/dists/samovar/main/binary-$TARGET_ARCH"
apt-ftparchive packages "$STAGING/pool" >"$STAGING/dists/samovar/main/binary-$TARGET_ARCH/Packages"
gzip -9n -k -f "$STAGING/dists/samovar/main/binary-$TARGET_ARCH/Packages"
apt-ftparchive \
  -o "APT::FTPArchive::Release::Origin=Samovar" \
  -o "APT::FTPArchive::Release::Label=Samovar Offline" \
  -o "APT::FTPArchive::Release::Suite=samovar" \
  -o "APT::FTPArchive::Release::Codename=samovar" \
  -o "APT::FTPArchive::Release::Architectures=$TARGET_ARCH" \
  -o "APT::FTPArchive::Release::Components=main" \
  release "$STAGING/dists/samovar" >"$STAGING/dists/samovar/Release"

mkdir -p "$KEY_HOME"
export GNUPGHOME="$KEY_HOME"
chmod 700 "$GNUPGHOME"
if ! gpg --batch --list-secret-keys samovar-offline@samovar.invalid >/dev/null 2>&1; then
  gpg --batch --pinentry-mode loopback --passphrase '' \
    --quick-generate-key 'Samovar Offline APT <samovar-offline@samovar.invalid>' rsa3072 sign 0
fi
FINGERPRINT="$(gpg --batch --with-colons --list-keys samovar-offline@samovar.invalid | awk -F: '$1 == "fpr" {print $10; exit}')"
gpg --batch --yes --export "$FINGERPRINT" >"$STAGING/samovar-offline-archive-keyring.gpg"
gpg --batch --yes --pinentry-mode loopback --passphrase '' --local-user "$FINGERPRINT" \
  --clearsign --output "$STAGING/dists/samovar/InRelease" "$STAGING/dists/samovar/Release"

python3 - "$SEEDS_FILE" "$WORK_DIR/locked-packages.json" "$LOCK_FILE" "$selected_mirror" "$STAGING" "$TARGET_ARCH" <<'PY'
import datetime
import hashlib
import json
import sys
from pathlib import Path

seeds_path, packages_path, lock_path, mirror, staging_dir, target_arch = sys.argv[1:]
staging = Path(staging_dir)
seeds = json.loads(Path(seeds_path).read_text(encoding="utf-8"))

metadata = {}
required_meta = (
    "dists/samovar/InRelease",
    "dists/samovar/Release",
    f"dists/samovar/main/binary-{target_arch}/Packages",
    f"dists/samovar/main/binary-{target_arch}/Packages.gz",
    "samovar-offline-archive-keyring.gpg",
)
for m_rel in required_meta:
    mp = staging / m_rel
    if not mp.is_file():
        raise SystemExit(f"Error: required offline metadata file not generated: {m_rel}")
    metadata[m_rel] = hashlib.sha256(mp.read_bytes()).hexdigest()

lock = {
    "schema": 3,
    "state": "locked",
    "generated_at": datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat(),
    "generated_from": "offline/packages.seeds.json",
    "target": seeds["target"],
    "ubuntu_mirror": mirror,
    "external_apt": seeds["external_apt"],
    "metadata": metadata,
    "packages": json.loads(Path(packages_path).read_text(encoding="utf-8")),
}
names = {package["name"] for package in lock["packages"]}
required_prefixes = (
    "linux-image-",
    "linux-modules-",
    "linux-modules-extra-",
    "linux-modules-nvidia-595-open-",
)
for prefix in required_prefixes:
    if not any(name.startswith(prefix) for name in names):
        raise SystemExit(f"Error: resolved NVIDIA kernel closure lacks {prefix}*")

kernel_versions = {
    package["version"]
    for package in lock["packages"]
    if package["name"].startswith("linux-image-") and package["name"] != "linux-image-generic"
}
nvidia_module_versions = {
    package["version"]
    for package in lock["packages"]
    if package["name"].startswith("linux-modules-nvidia-595-open-")
    and package["name"] != "linux-modules-nvidia-595-open-generic"
}
if not kernel_versions.intersection(nvidia_module_versions):
    raise SystemExit("Error: NVIDIA kernel module version does not match a locked Linux image version.")
Path(lock_path).write_text(json.dumps(lock, indent=2) + "\n", encoding="utf-8")
PY

rm -rf "$REPOSITORY"
mv "$STAGING" "$REPOSITORY"
verify_locked_repository
echo "Signed offline APT bundle ready: $REPOSITORY"
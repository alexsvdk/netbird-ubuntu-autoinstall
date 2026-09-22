#!/usr/bin/env bash
set -euo pipefail

# Build a flat APT repository that can be copied from the installer media to
# the target. The directory is persistent: apt only downloads packages that
# are missing or have a newer candidate version.

LOCK_FILE=${1:?usage: build-apt-bundle.sh LOCK_FILE CACHE_DIR REFRESH_MODE}
CACHE_DIR=${2:?usage: build-apt-bundle.sh LOCK_FILE CACHE_DIR REFRESH_MODE}
REFRESH_MODE=${3:-auto}

case "$REFRESH_MODE" in
  auto|never) ;;
  *)
    echo "Error: OFFLINE_BUNDLE_REFRESH must be auto or never." >&2
    exit 1
    ;;
esac

command -v apt-get >/dev/null
command -v dpkg-scanpackages >/dev/null
command -v python3 >/dev/null

mapfile_supported=false
if (mapfile -t _ </dev/null) 2>/dev/null; then
  mapfile_supported=true
fi

mkdir -p "$CACHE_DIR/debs"
request_file="$CACHE_DIR/packages.txt"
request_tmp="$(mktemp)"
trap 'rm -f "$request_tmp"' EXIT

python3 - "$LOCK_FILE" >"$request_tmp" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as source:
    packages = json.load(source)["packages"]

for package in packages["required"]:
    print(package["name"])
PY

if [[ ! -s "$request_tmp" ]]; then
  echo "Error: offline package lock contains no required packages." >&2
  exit 1
fi

if [[ "$REFRESH_MODE" == "never" ]]; then
  if [[ ! -f "$CACHE_DIR/Packages.gz" || ! -f "$request_file" ]]; then
    echo "Error: offline cache does not exist; rerun with OFFLINE_BUNDLE_REFRESH=auto." >&2
    exit 1
  fi
  cmp -s "$request_tmp" "$request_file" || {
    echo "Error: offline package lock changed; rerun with OFFLINE_BUNDLE_REFRESH=auto." >&2
    exit 1
  }
  echo "Using existing offline APT bundle: $CACHE_DIR"
  exit 0
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq

# Keep downloaded .debs outside /var/cache/apt so the cache survives containers.
apt-get \
  -o Dir::Cache::archives="$CACHE_DIR/debs" \
  -o Dir::Cache::archives::partial="$CACHE_DIR/debs/partial" \
  --download-only \
  --yes \
  install $(tr '\n' ' ' <"$request_tmp")

# A flat repository is sufficient for an ISO and does not require apt-ftparchive.
(
  cd "$CACHE_DIR"
  dpkg-scanpackages debs /dev/null >Packages
  gzip -9n -f -k Packages
)

mv "$request_tmp" "$request_file"
trap - EXIT
echo "Offline APT bundle ready: $CACHE_DIR"
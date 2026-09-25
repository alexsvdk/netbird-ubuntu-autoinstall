#!/usr/bin/env bash
set -euo pipefail

# Persist OCI artifacts outside Git. Docker only downloads changed layers during
# refresh; the lock records the immutable registry digest and saved-tar SHA-256.
# Also bundles GeoIP database for offline Mihomo initialization.

LOCK_FILE=${1:?usage: build-oci-artifacts.sh LOCK_FILE CACHE_DIR REFRESH_MODE IMAGE_REF}
CACHE_DIR=${2:?usage: build-oci-artifacts.sh LOCK_FILE CACHE_DIR REFRESH_MODE IMAGE_REF}
REFRESH_MODE=${3:-auto}
IMAGE_REF=${4:?usage: build-oci-artifacts.sh LOCK_FILE CACHE_DIR REFRESH_MODE IMAGE_REF}
GEOIP_URL="https://github.com/MetaCubeX/meta-rules-dat/releases/latest/download/geoip.metadb"

case "$REFRESH_MODE" in
  auto|never) ;;
  *)
    echo "Error: OFFLINE_ARTIFACT_REFRESH must be auto or never." >&2
    exit 1
    ;;
esac

command -v python3 >/dev/null
command -v sha256sum >/dev/null || command -v shasum >/dev/null

artifact_tar="$CACHE_DIR/mihomo-image.tar"
geoip_file="$CACHE_DIR/geoip.metadb"

verify_lock() {
  python3 - "$LOCK_FILE" "$artifact_tar" "$geoip_file" <<'PY'
import hashlib
import json
import sys
from pathlib import Path

lock_path, artifact_path, geoip_path = map(Path, sys.argv[1:])
lock = json.loads(lock_path.read_text(encoding="utf-8"))
artifacts = lock.get("artifacts", [])
if lock.get("schema") != 2 or lock.get("state") != "locked":
    raise SystemExit("Error: OCI artifact lock is not generated; refresh it online first.")
by_name = {a.get("name"): a for a in artifacts}
if "mihomo" not in by_name:
    raise SystemExit("Error: Mihomo artifact missing from lock.")
if not artifact_path.is_file():
    raise SystemExit("Error: locked Mihomo OCI artifact is missing.")
if hashlib.sha256(artifact_path.read_bytes()).hexdigest() != by_name["mihomo"].get("sha256"):
    raise SystemExit("Error: Mihomo OCI artifact SHA-256 mismatch.")
if "geoip" not in by_name:
    raise SystemExit("Error: GeoIP artifact missing from lock.")
if not geoip_path.is_file():
    raise SystemExit("Error: locked GeoIP artifact is missing.")
if hashlib.sha256(geoip_path.read_bytes()).hexdigest() != by_name["geoip"].get("sha256"):
    raise SystemExit("Error: GeoIP artifact SHA-256 mismatch.")
PY
}

if [[ "$REFRESH_MODE" == "never" ]]; then
  verify_lock
  echo "Using verified OCI and GeoIP artifact cache: $CACHE_DIR"
  exit 0
fi

command -v docker >/dev/null
command -v curl >/dev/null

mkdir -p "$CACHE_DIR"

# 1. Mihomo container image
previous_image_id="$(docker image inspect "$IMAGE_REF" --format '{{.Id}}' 2>/dev/null || true)"
docker pull --platform linux/amd64 "$IMAGE_REF" >/dev/null
current_image_id="$(docker image inspect "$IMAGE_REF" --format '{{.Id}}')"
DIGEST_REF="$(docker image inspect "$IMAGE_REF" --format '{{index .RepoDigests 0}}')"
if [[ -z "$DIGEST_REF" || "$DIGEST_REF" != *@sha256:* ]]; then
  echo "Error: Docker did not report an immutable digest for $IMAGE_REF." >&2
  exit 1
fi
docker save --output "$artifact_tar.tmp" "$DIGEST_REF"
mv "$artifact_tar.tmp" "$artifact_tar"

if command -v sha256sum >/dev/null 2>&1; then
  SHA256="$(sha256sum "$artifact_tar" | awk '{print $1}')"
else
  SHA256="$(shasum -a 256 "$artifact_tar" | awk '{print $1}')"
fi
printf '%s  %s\n' "$SHA256" "$(basename "$artifact_tar")" >"$CACHE_DIR/mihomo-image.tar.sha256"

# 2. GeoIP database
echo "Fetching Mihomo GeoIP database..."
curl -fsSL --connect-timeout 10 --max-time 120 --retry 3 -o "$geoip_file.tmp" "$GEOIP_URL"
mv "$geoip_file.tmp" "$geoip_file"

if command -v sha256sum >/dev/null 2>&1; then
  GEOIP_SHA256="$(sha256sum "$geoip_file" | awk '{print $1}')"
else
  GEOIP_SHA256="$(shasum -a 256 "$geoip_file" | awk '{print $1}')"
fi
printf '%s  %s\n' "$GEOIP_SHA256" "$(basename "$geoip_file")" >"$CACHE_DIR/geoip.metadb.sha256"

# 3. Write lock
python3 - "$LOCK_FILE" "$IMAGE_REF" "$DIGEST_REF" "$artifact_tar" "$SHA256" "$geoip_file" "$GEOIP_SHA256" "$GEOIP_URL" <<'PY'
import datetime
import json
import sys
from pathlib import Path

lock_path, image_ref, digest_ref, artifact_path, sha256, geoip_path, geoip_sha256, geoip_url = sys.argv[1:]
lock = {
    "schema": 2,
    "state": "locked",
    "generated_at": datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat(),
    "artifacts": [
        {
            "name": "mihomo",
            "type": "oci-image",
            "image": image_ref,
            "digest": digest_ref,
            "platform": "linux/amd64",
            "filename": Path(artifact_path).name,
            "sha256": sha256,
            "target_path": "/var/lib/samovar-offline-artifacts/mihomo-image.tar",
        },
        {
            "name": "geoip",
            "type": "data-file",
            "url": geoip_url,
            "filename": Path(geoip_path).name,
            "sha256": geoip_sha256,
            "target_path": "/var/lib/samovar-offline-artifacts/geoip.metadb",
        },
    ],
}
Path(lock_path).write_text(json.dumps(lock, indent=2) + "\n", encoding="utf-8")
PY

verify_lock
if [[ "$previous_image_id" =~ ^sha256:[0-9a-f]{64}$ && "$previous_image_id" != "$current_image_id" ]]; then
  # Do not force removal: Docker keeps it if another tag or container uses it.
  docker image rm "$previous_image_id" >/dev/null 2>&1 || true
fi
echo "Locked Mihomo OCI and GeoIP artifacts ready: $CACHE_DIR"
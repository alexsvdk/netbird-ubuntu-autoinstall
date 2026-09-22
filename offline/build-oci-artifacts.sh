#!/usr/bin/env bash
set -euo pipefail

# Persist OCI artifacts outside Git. Docker only downloads changed layers during
# refresh; the lock records the immutable registry digest and saved-tar SHA-256.

LOCK_FILE=${1:?usage: build-oci-artifacts.sh LOCK_FILE CACHE_DIR REFRESH_MODE IMAGE_REF}
CACHE_DIR=${2:?usage: build-oci-artifacts.sh LOCK_FILE CACHE_DIR REFRESH_MODE IMAGE_REF}
REFRESH_MODE=${3:-auto}
IMAGE_REF=${4:?usage: build-oci-artifacts.sh LOCK_FILE CACHE_DIR REFRESH_MODE IMAGE_REF}

case "$REFRESH_MODE" in
  auto|never) ;;
  *)
    echo "Error: OFFLINE_ARTIFACT_REFRESH must be auto or never." >&2
    exit 1
    ;;
esac

command -v docker >/dev/null
command -v python3 >/dev/null
command -v sha256sum >/dev/null || command -v shasum >/dev/null

artifact_tar="$CACHE_DIR/mihomo-image.tar"

verify_lock() {
  python3 - "$LOCK_FILE" "$artifact_tar" <<'PY'
import hashlib
import json
import sys
from pathlib import Path

lock_path, artifact_path = map(Path, sys.argv[1:])
lock = json.loads(lock_path.read_text(encoding="utf-8"))
artifacts = lock.get("artifacts", [])
if lock.get("schema") != 2 or lock.get("state") != "locked" or len(artifacts) != 1:
    raise SystemExit("Error: OCI artifact lock is not generated; refresh it online first.")
artifact = artifacts[0]
if not artifact_path.is_file():
    raise SystemExit("Error: locked Mihomo OCI artifact is missing.")
if hashlib.sha256(artifact_path.read_bytes()).hexdigest() != artifact.get("sha256"):
    raise SystemExit("Error: Mihomo OCI artifact SHA-256 mismatch.")
PY
}

if [[ "$REFRESH_MODE" == "never" ]]; then
  verify_lock
  echo "Using verified OCI artifact cache: $artifact_tar"
  exit 0
fi

mkdir -p "$CACHE_DIR"
docker pull --platform linux/amd64 "$IMAGE_REF" >/dev/null
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

python3 - "$LOCK_FILE" "$IMAGE_REF" "$DIGEST_REF" "$artifact_tar" "$SHA256" <<'PY'
import datetime
import json
import sys
from pathlib import Path

lock_path, image_ref, digest_ref, artifact_path, sha256 = sys.argv[1:]
lock = {
    "schema": 2,
    "state": "locked",
    "generated_at": datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat(),
    "artifacts": [{
        "name": "mihomo",
        "type": "oci-image",
        "image": image_ref,
        "digest": digest_ref,
        "platform": "linux/amd64",
        "filename": Path(artifact_path).name,
        "sha256": sha256,
        "target_path": "/var/lib/samovar-offline-artifacts/mihomo-image.tar",
    }],
}
Path(lock_path).write_text(json.dumps(lock, indent=2) + "\n", encoding="utf-8")
PY

verify_lock
echo "Locked Mihomo OCI artifact ready: $artifact_tar"
#!/usr/bin/env bash
set -euo pipefail
# Sign samovar-config.json with the owner's Ed25519 SSH key
# Usage: ./build-recovery-config.sh [path/to/samovar-config.json] [path/to/key]
# Defaults: samovar-config.json in current dir, ~/.ssh/id_ed25519

CONFIG_FILE="${1:-samovar-config.json}"
SIGNING_KEY="${2:-$HOME/.ssh/id_ed25519}"

[[ -f "$CONFIG_FILE" ]] || { echo "Error: $CONFIG_FILE not found"; exit 1; }
[[ -f "$SIGNING_KEY" ]] || { echo "Error: $SIGNING_KEY not found"; exit 1; }

echo "Signing $CONFIG_FILE with $SIGNING_KEY ..."
ssh-keygen -Y sign \
  -f "$SIGNING_KEY" \
  -n samovar-recovery \
  "$CONFIG_FILE"

echo "Signature written to ${CONFIG_FILE}.sig"
echo
echo "Next steps:"
echo "  1. Keep samovar-config.json and ${CONFIG_FILE}.sig private"
echo "  2. Run ./build-autoinstall-iso.sh to build the ISO"
echo "  3. Or copy both files to a FAT32 USB with label SAMOVARCFG for recovery"

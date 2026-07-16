#!/usr/bin/env python3
import re
import sys
from pathlib import Path

source = Path(sys.argv[1])
destination = Path(sys.argv[2])

text = source.read_text(encoding="utf-8")
lines = text.splitlines(keepends=True)
patched = []
changed = 0

for line in lines:
    if re.match(r"^\s*(linux|linuxefi)\s+.*?/casper/vmlinuz", line) and "autoinstall" not in line:
        if " ---" in line:
            line = line.replace(" ---", " autoinstall fsck.mode=skip ---", 1)
        else:
            line = line.rstrip("\n") + " autoinstall fsck.mode=skip\n"
        changed += 1

    if re.match(r"^\s*set\s+timeout\s*=", line):
        prefix = line[: len(line) - len(line.lstrip())]
        line = f"{prefix}set timeout=3\n"

    patched.append(line)

if changed == 0:
    raise SystemExit("Could not find a GRUB linux /casper/vmlinuz line to patch")

destination.write_text("".join(patched), encoding="utf-8")
print(f"Patched {changed} GRUB boot entrie(s)")

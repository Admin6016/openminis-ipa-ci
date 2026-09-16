#!/usr/bin/env bash
#
# [T-trollstore-capabilities] Add filesystem-escaping entitlements, opt-in.
#
# Only run when the build explicitly asks for them. These are PRIVATE
# entitlements: they can only take effect through TrollStore (or a jailbreak),
# because a normal code signature is rejected without a matching provisioning
# profile. They are what makes the `ios_fs` tool appear.
#
# ## The risk this guards against
#
# `com.apple.private.security.no-container` changes what `NSHomeDirectory()` and
# `urls(for:.documentDirectory)` resolve to. Minis finds its Alpine rootfs and
# its App Group container through exactly those APIs, so applying this to a
# build that is not being tested in isolation can leave the app unable to locate
# its own data. Hence: never applied by default, and the app should be launched
# once and checked before such a build is handed out.
#
# Usage: apply_enhanced_entitlements.sh <path/to/Minis.entitlements>

set -euo pipefail

ENT="${1:?usage: apply_enhanced_entitlements.sh <Minis.entitlements>}"

[ -f "$ENT" ] || { echo "no entitlements at $ENT" >&2; exit 1; }

if grep -q 'com.apple.private.security.no-container' "$ENT"; then
    echo "[ent] enhanced entitlements already present"
    exit 0
fi

echo "[ent] adding filesystem-escaping entitlements to $(basename "$ENT")"

python3 - "$ENT" <<'PY'
import plistlib, sys
from pathlib import Path

p = Path(sys.argv[1])
with p.open("rb") as fh:
    d = plistlib.load(fh)

# The minimum set that yields real filesystem reach. Deliberately NOT including
# task_for_pid / dynamic-codesigning: those widen the blast radius further and
# nothing in the current tool set needs them, so they stay opt-in for later.
d["com.apple.private.security.no-container"] = True
d["com.apple.private.security.storage.AppDataContainers"] = True

with p.open("wb") as fh:
    plistlib.dump(d, fh)

print("[ent] now holds:")
for k in sorted(d.keys()):
    print("       ", k)
PY

echo "[ent] done — this build escapes the app container"

#!/usr/bin/env bash
#
# Ad-hoc sign a Minis .app with its App Group / capability entitlements, for
# distribution as an UNSIGNED ipa that TrollStore will re-sign on install.
#
# Why this exists
# ---------------
# The app calls:
#     FileManager.default.containerURL(
#         forSecurityApplicationGroupIdentifier: "group.com.openminis.app")!
# at startup (AIChatViewModel.minisAppGroupRoot). That API returns nil unless
# the running binary carries `com.apple.security.application-groups` in its
# code signature, and the `!` turns that nil into a SIGTRAP crash before the
# first frame renders. A completely unsigned binary therefore crashes on
# launch; it must be ad-hoc signed with entitlements.
#
# TrollStore re-signs with ldid on install and picks the entitlements up from
# the existing signature, so embedding them here is what makes App Groups work
# on a TrollStore device.
#
# Signing order is inside-out, which codesign enforces: nested frameworks and
# dylibs first, then each extension bundle, then the app itself. Signing the
# outer bundle first would invalidate the inner signatures.
#
# Usage: sign_adhoc.sh <path/to/Minis.app> <path/to/src/ios>

set -euo pipefail

APP="${1:?usage: sign_adhoc.sh <Minis.app> <src/ios dir>}"
ENT_DIR="${2:?usage: sign_adhoc.sh <Minis.app> <src/ios dir>}"

[ -d "$APP" ] || { echo "no such app bundle: $APP" >&2; exit 1; }

echo "== signing target: $APP"
echo "== entitlements dir: $ENT_DIR"

# ---------------------------------------------------------------------------
# 1. Nested frameworks and dylibs, deepest first.
#    --force is required because Xcode may have left a no-op signature holder;
#    --timestamp=none avoids a network round trip to Apple's TSA.
# ---------------------------------------------------------------------------
sign_frameworks() {
  local list
  # Deepest paths first so a framework nested inside another is signed first.
  list=$(find "$APP" -depth \( -name '*.framework' -o -name '*.dylib' \) -print 2>/dev/null || true)
  if [ -z "$list" ]; then
    echo "   (no frameworks found)"
    return
  fi
  local n=0
  while IFS= read -r fw; do
    [ -e "$fw" ] || continue
    codesign --force --sign - --timestamp=none "$fw" >/dev/null 2>&1 \
      || { echo "   FAILED: $fw" >&2; return 1; }
    n=$((n + 1))
  done <<< "$list"
  echo "   signed $n framework(s)/dylib(s)"
}
echo "== [1/3] frameworks"
sign_frameworks

# ---------------------------------------------------------------------------
# 2. Extensions, each with its OWN entitlements file. These only declare the
#    App Group (they are not the crash site, but they must agree with the host
#    app or the FileProvider / Share Sheet cannot reach the shared container).
# ---------------------------------------------------------------------------
echo "== [2/3] extensions"
sign_ext() {
  local bundle="$1" ent="$2" label="$3"
  if [ ! -d "$bundle" ]; then
    echo "   skip $label (not present)"
    return 0
  fi
  if [ ! -f "$ent" ]; then
    echo "   skip $label (no entitlements at $ent)"
    return 0
  fi
  # Re-sign inner frameworks of the appex first.
  local inner
  inner=$(find "$bundle" -depth \( -name '*.framework' -o -name '*.dylib' \) -print 2>/dev/null || true)
  if [ -n "$inner" ]; then
    while IFS= read -r f; do
      [ -e "$f" ] || continue
      codesign --force --sign - --timestamp=none "$f" >/dev/null 2>&1 || true
    done <<< "$inner"
  fi
  codesign --force --sign - --timestamp=none --entitlements "$ent" "$bundle" >/dev/null
  echo "   signed $label"
}
sign_ext "$APP/PlugIns/AgentWidgetExtension.appex" "$ENT_DIR/AgentWidget/AgentWidget.entitlements"   "AgentWidgetExtension.appex"
sign_ext "$APP/PlugIns/MinisShare.appex"           "$ENT_DIR/ShareExtension/ShareExtension.entitlements" "MinisShare.appex"
sign_ext "$APP/PlugIns/MinisFileProvider.appex"    "$ENT_DIR/FileProvider/FileProvider.entitlements"     "MinisFileProvider.appex"

# ---------------------------------------------------------------------------
# 3. The app bundle itself, with the full capability set.
# ---------------------------------------------------------------------------
echo "== [3/3] app bundle"
[ -f "$ENT_DIR/Minis.entitlements" ] || { echo "missing $ENT_DIR/Minis.entitlements" >&2; exit 1; }
codesign --force --sign - --timestamp=none --entitlements "$ENT_DIR/Minis.entitlements" "$APP" >/dev/null
echo "   signed Minis.app"

# ---------------------------------------------------------------------------
# 4. Verify. This is the check that would have caught the original crash:
#    the App Group entitlement MUST be readable back out of the signature.
# ---------------------------------------------------------------------------
echo "== verifying"
if ! codesign -dv "$APP" >/dev/null 2>&1; then
  echo "!! signature invalid on $APP" >&2
  exit 1
fi

echo "--- entitlements read back from Minis.app ---"
codesign -d --entitlements - --xml "$APP" 2>/dev/null \
  | python3 -c 'import sys,plistlib;d=plistlib.loads(sys.stdin.buffer.read());print("\n".join(f"   {k} = {v}" for k,v in sorted(d.items())))' \
  || codesign -d --entitlements - "$APP" 2>&1 | sed 's/^/   /'

GRP=$(codesign -d --entitlements - --xml "$APP" 2>/dev/null \
      | python3 -c 'import sys,plistlib;print(plistlib.loads(sys.stdin.buffer.read()).get("com.apple.security.application-groups",[]))' 2>/dev/null || echo "")
case "$GRP" in
  *group.com.openminis.app*)
    echo "   OK: com.apple.security.application-groups contains group.com.openminis.app" ;;
  *)
    echo "!! FAIL: App Group entitlement missing from the signed binary." >&2
    echo "!! The app would crash on launch with 'Unexpectedly found nil'." >&2
    exit 1 ;;
esac

for ext in "$APP/PlugIns"/*.appex; do
  [ -d "$ext" ] || continue
  if codesign -dv "$ext" >/dev/null 2>&1; then
    echo "   OK: $(basename "$ext") signature valid"
  else
    echo "!! signature invalid: $ext" >&2
    exit 1
  fi
done

echo "== ad-hoc signing complete"

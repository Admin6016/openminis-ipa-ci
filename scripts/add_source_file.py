#!/usr/bin/env python3
"""
Register a new source file in Minis.xcodeproj so it is actually compiled.

The Minis app target uses explicit PBXFileReference / PBXBuildFile entries for
src/ios/Agent/**, NOT a PBXFileSystemSynchronizedRootGroup — so dropping a
.swift file into the folder does nothing on its own. This script appends the
four entries Xcode would create:

  1. PBXBuildFile        (links the fileRef into the Sources phase)
  2. PBXFileReference    (the file itself)
  3. group children       (so it appears in the navigator)
  4. Sources build phase  (so it is actually compiled)

It anchors on an existing sibling file's entries, which are unique strings in
the pbxproj, and is idempotent (skips if the new basename is already present).

Usage: python3 add_source_file.py <repo-root> <basename> [anchor-basename]
Example: python3 add_source_file.py . AutoContinue.swift AIChatViewModel+Fallback.swift
"""
import re
import sys
from pathlib import Path

root = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
new_base = sys.argv[2]
anchor_base = sys.argv[3] if len(sys.argv) > 3 else None

PBX = root / "src" / "ios" / "Minis.xcodeproj" / "project.pbxproj"
if not PBX.exists():
    sys.exit(f"FATAL: {PBX} not found")

src = PBX.read_text()

if f"/* {new_base} in Sources */" in src or f"/* {new_base} */" in src:
    print(f"[ALREADY] {new_base} is already registered")
    sys.exit(0)

# ---------------------------------------------------------------------------
# Locate the anchor file's four entry shapes. If no anchor was given, pick the
# first sourcecode.swift reference we can see and use it.
#
# NOTE: this project does NOT use 24-hex object ids. It uses short custom ids
# like "E5VMB0002" / "AC0000200" (9-17 alphanumerics). Match that shape.
# ---------------------------------------------------------------------------
OBJID = r"[A-Za-z0-9]{8,24}"

buildfile_re = re.compile(
    r"\n(\t\t(" + OBJID + r") /\* ([^*]+\.swift) in Sources \*/ = "
    r"\{isa = PBXBuildFile; fileRef = (" + OBJID + r") /\* [^*]+ \*/; \};)"
)
fileref_re = re.compile(
    r"\n(\t\t(" + OBJID + r") /\* ([^*]+\.swift) \*/ = "
    r"\{isa = PBXFileReference; lastKnownFileType = sourcecode\.swift; "
    r"path = \"?([^\";]+)\"?; sourceTree = \"<group>\"; \};)"
)

buildfiles = {m.group(3): m for m in buildfile_re.finditer(src)}
filerefs = {m.group(3): m for m in fileref_re.finditer(src)}

if anchor_base:
    if anchor_base not in filerefs:
        sys.exit(f"FATAL: anchor fileRef {anchor_base!r} not found in pbxproj")
    if anchor_base not in buildfiles:
        sys.exit(f"FATAL: anchor buildFile {anchor_base!r} not found in pbxproj")
    anchor = anchor_base
else:
    common = [n for n in filerefs if n in buildfiles]
    if not common:
        sys.exit("FATAL: no usable anchor (a .swift file present in both sections)")
    anchor = common[0]

print(f"[INFO  ] anchoring on {anchor}")

fm = filerefs[anchor]
bm = buildfiles[anchor]
anchor_fileref_id = re.match(r"\n\t\t(" + OBJID + r")", fm.group(0)).group(1)
anchor_buildfile_id = re.match(r"\n\t\t(" + OBJID + r")", bm.group(0)).group(1)

# ---------------------------------------------------------------------------
# Generate fresh, collision-free ids.
#
# BUG (fixed here): the collision check below used to scan for 24-hex ids while
# PRODUCING 15-character ones ("AC%013X"), so it could never see its own
# output. Every run therefore restarted at seed 1 and handed out
# AC0000000000001 / ...002 again. Registering a SECOND file in the same project
# produced DUPLICATE PBXFileReference ids, which corrupts the pbxproj — the
# duplicate's source file then silently never compiles, surfacing much later as
# "cannot find X in scope" for every symbol that file declares.
#
# Two changes make it correct:
#   1. collect EVERY object id in the file, at whatever width the project uses
#      (this project mixes 8/9/10/11/15/24/25 characters), not one fixed width;
#   2. seed the counter PAST the highest existing 'AC' id, so a second
#      invocation continues the sequence instead of colliding with the first.
# ---------------------------------------------------------------------------
existing_ids = set(re.findall(r"\b([0-9A-Za-z]+) /\*", src))
existing_ids |= set(re.findall(r"\b([0-9A-Za-z]{8,})\b", src))

_AC_RE = re.compile(r"\bAC([0-9A-Fa-f]{13})\b")


def _next_seed() -> int:
    """One past the largest existing AC-id, so repeated runs never collide."""
    used = [int(m, 16) for m in _AC_RE.findall(src)]
    return (max(used) + 1) if used else 1


_next = _next_seed()


def fresh_id() -> str:
    global _next
    while True:
        cand = "AC%013X" % _next
        _next += 1
        if cand not in existing_ids:
            existing_ids.add(cand)
            return cand


new_fileref_id = fresh_id()
new_buildfile_id = fresh_id()
print(f"[INFO  ] new ids: fileRef={new_fileref_id} buildFile={new_buildfile_id}")

# ---------------------------------------------------------------------------
# 1. PBXBuildFile
# ---------------------------------------------------------------------------
new_buildfile = (
    f"\n\t\t{new_buildfile_id} /* {new_base} in Sources */ = "
    f"{{isa = PBXBuildFile; fileRef = {new_fileref_id} /* {new_base} */; }};"
)
src = src.replace(bm.group(0), bm.group(0) + new_buildfile, 1)

# ---------------------------------------------------------------------------
# 2. PBXFileReference
# ---------------------------------------------------------------------------
new_fileref = (
    f"\n\t\t{new_fileref_id} /* {new_base} */ = "
    f"{{isa = PBXFileReference; lastKnownFileType = sourcecode.swift; "
    f'path = "{new_base}"; sourceTree = "<group>"; }};'
)
src = src.replace(fm.group(0), fm.group(0) + new_fileref, 1)

# ---------------------------------------------------------------------------
# 3. group children — the bare reference line, always `<id> /* name */,`
# ---------------------------------------------------------------------------
child_anchor = f"\n\t\t\t\t{anchor_fileref_id} /* {anchor} */,"
if src.count(child_anchor) != 1:
    sys.exit(f"FATAL: group-child anchor for {anchor} matched {src.count(child_anchor)} times")
src = src.replace(child_anchor, child_anchor + f"\n\t\t\t\t{new_fileref_id} /* {new_base} */,", 1)

# ---------------------------------------------------------------------------
# 4. Sources build phase
# ---------------------------------------------------------------------------
src_anchor = f"\n\t\t\t\t{anchor_buildfile_id} /* {anchor} in Sources */,"
if src.count(src_anchor) != 1:
    sys.exit(f"FATAL: Sources-phase anchor for {anchor} matched {src.count(src_anchor)} times")
src = src.replace(src_anchor, src_anchor + f"\n\t\t\t\t{new_buildfile_id} /* {new_base} in Sources */,", 1)

# ---------------------------------------------------------------------------
# Sanity: brace BALANCE must be unchanged. (Absolute counts legitimately grow
# — each PBXBuildFile / PBXFileReference entry is itself a `{ ... }`.)
# ---------------------------------------------------------------------------
before = PBX.read_text()
def balance(s):
    return s.count("{") - s.count("}"), s.count("(") - s.count(")")
if balance(src) != balance(before):
    sys.exit(f"FATAL: brace/paren balance changed {balance(before)} -> {balance(src)}; refusing to write")

PBX.write_text(src)
print(f"[APPLY ] registered {new_base} (4 entries)")

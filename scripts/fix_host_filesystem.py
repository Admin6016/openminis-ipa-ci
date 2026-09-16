#!/usr/bin/env python3
r"""
TrollStore-gated real-filesystem access.

Adds an `ios_fs` tool that reaches the REAL iOS filesystem, registered ONLY on
builds whose code signature carries a filesystem-escaping entitlement.

## Why capability-gated rather than build-flagged

TrollStore is an install method, not a runtime property — nothing in-process
reports "TrollStore installed me". What actually differs is the entitlement set
the binary was signed with. So the gate is `HostCapabilities.canReachRealFilesystem`,
read straight off this process's own signature via SecTaskCopyValueForEntitlement.

Consequence: an App Store / ordinary-sideload build holds no `no-container`
entitlement, `makeAgentTools()` never returns the definition, and the model
never sees the tool — it cannot plan around something it cannot call.

## Risk this patch does NOT hide

`com.apple.private.security.no-container` changes what `NSHomeDirectory()` and
`urls(for:.documentDirectory)` resolve to. Minis locates its rootfs and its
App Group container through exactly those APIs, so a careless entitlement set
can stop the app from finding its own data. The entitlements are therefore
applied by a SEPARATE, opt-in build input, so a normal build is unaffected and
an enhanced build can be tested in isolation.

Usage: python3 fix_host_filesystem.py <repo-root>
"""
import sys
from pathlib import Path

root = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
CH = root / "src" / "ios" / "Agent" / "Chat"
DEFS = CH / "AIChatViewModel+ToolDefinitions.swift"
DISPATCH = CH / "AIChatViewModel+ConcurrentTools.swift"

for p in (DEFS, DISPATCH):
    if not p.exists():
        sys.exit(f"FATAL: missing {p}")

MARKER = "[ci-fix-hostfs]"
if MARKER in DEFS.read_text():
    print("[ALREADY] host filesystem patch present")
    sys.exit(0)

print("[GATE   ] wiring the capability-gated ios_fs tool")

edits = []


def edit(path, desc, old, new, count=1):
    s = path.read_text()
    n = s.count(old)
    if n != count:
        sys.exit(f"FATAL: {desc}\n  {path.name}: expected {count}, found {n}\n  anchor: {old[:110]!r}")
    path.write_text(s.replace(old, new, count))
    edits.append(desc)


# ==========================================================================
# 1. Advertise the tool only when the capability is present.
# ==========================================================================
edit(DEFS, "register ios_fs behind the capability gate",
     r"""        // [ci-fix-introspect] Live progress of parallel sub-agent runs.""",
     r"""        // [ci-fix-hostfs] Real-iOS-filesystem tool. `makeHostFilesystemTool()`
        // returns nil unless this binary holds a filesystem-escaping
        // entitlement, so the tool is simply ABSENT from the schema on any
        // build without one — an App Store or ordinary-sideload install never
        // sees it, and the model cannot call something it cannot see.
        if let hostFS = Self.makeHostFilesystemTool() {
            tools.append(hostFS)
        }

        // [ci-fix-introspect] Live progress of parallel sub-agent runs.""")

# ==========================================================================
# 2. Dispatch it.
# ==========================================================================
edit(DISPATCH, "dispatch ios_fs",
     r"""        case "agent_status":""",
     r"""        case "ios_fs":
            // [ci-fix-hostfs] Reached only on builds that advertised the tool,
            // i.e. ones holding a filesystem-escaping entitlement — the runner
            // refuses again internally as defence in depth.
            toolOutput = Self.runHostFilesystem(
                action: (toolArgs["action"] as? String) ?? "",
                path: (toolArgs["path"] as? String) ?? "",
                args: toolArgs
            )
            toolSuccess = !toolOutput.hasPrefix("Error:") && !toolOutput.hasPrefix("Refused:")
            if msgIdx < messages.count, blockIdx < messages[msgIdx].blocks.count {
                messages[msgIdx].blocks[blockIdx].content = toolOutput
            }

        case "agent_status":""")

# ==========================================================================
# 3. (no framework linking needed)
#
# An earlier revision read the entitlements off our own signature, which needed
# Security.framework. The current design probes the filesystem directly instead
# (see HostCapabilities), so nothing has to be linked and the pbxproj is left
# untouched — less risk for the same gate.
# ==========================================================================

for d in edits:
    print(f"[APPLY  ] {d}")
print(f"[fix] host filesystem: {len(edits)} edit(s)")
print("[NOTE   ] entitlements are applied separately, only when the build opts in")

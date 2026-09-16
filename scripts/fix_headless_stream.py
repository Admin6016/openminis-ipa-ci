#!/usr/bin/env python3
"""
Fix the headless (sub-agent) stream path in AIChatViewModel+SSEStream.swift.

## The bug

`processStreamEvents(headless: true)` disables UI writes by pushing `msgIdx`
out of range (`Int.max`). The intent is stated in the doc comment: "all write
sites are guarded by `msgIdx < messages.count`". That holds for writes to
`messages[...]` — but two sites put DATA COLLECTION behind the same guard, and
`continue`/`return -1` skips the collection entirely:

  1. `contentBlockStart` / `.text`
     `currentTextBlockIdx` stays nil because the MainActor closure returns -1
     when `msgIdx >= messages.count`. Then `.textDelta` does
         `if let blockIdx = currentTextBlockIdx { result.assistantText += text }`
     so no text is ever accumulated.

  2. `contentBlockStop` / `.toolCallComplete`
         guard blockIdx >= 0 else { continue }
         result.toolEntries.append(...)      // never reached
     so tool calls are dropped.

Result in `SubAgentRunner.run()`: the first turn yields an empty
`StreamResult`, so `guard !toolEntries.isEmpty else { finish(.succeeded …) }`
fires immediately — which is exactly the observed symptom: every sub-agent
reports "Done" with 0 tool calls, 1 turn and no output.

## The fix

Separate UI writes from data collection. The UI index stays suppressed in
headless mode (`textUIBlockIdx = nil`), while the data-collection indices
become plain logical counters that are always populated.

This script is marker-guarded (idempotent) and fails loudly if an anchor is
missing, so a rebased upstream cannot be silently mis-patched.

Usage: python3 fix_headless_stream.py <repo-root>
"""
import sys
from pathlib import Path

root = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
SSE = root / "src" / "ios" / "Agent" / "Chat" / "AIChatViewModel+SSEStream.swift"

if not SSE.exists():
    sys.exit(f"FATAL: {SSE} not found")

src = SSE.read_text()

# ---------------------------------------------------------------------------
# FEATURE GATE: the headless mode must actually be present.
# ---------------------------------------------------------------------------
if "headless" not in src:
    print("[GATE   ] no headless support in this tree — nothing to patch")
    sys.exit(0)

MARKER = "// [ci-fix-headless]"

if MARKER in src:
    n = src.count(MARKER)
    print(f"[ALREADY] headless stream patch present ({n} marker(s))")
    sys.exit(0)

print("[GATE   ] headless stream path detected — applying fix")

# ===========================================================================
# Patch 1 — declare the index pair next to the existing local state.
# `currentTextBlockIdx` keeps its meaning (the UI block index) and is set to
# nil in headless mode. `textContentActive` carries the "is a text block open"
# signal that the data path needs.
# ===========================================================================
anchor1 = """        let msgIdx = headless ? Int.max : rawMsgIdx
        var result = StreamResult()
        var currentTextBlockIdx: Int? = nil"""
repl1 = """        let msgIdx = headless ? Int.max : rawMsgIdx
        var result = StreamResult()
        var currentTextBlockIdx: Int? = nil
        // [ci-fix-headless] In headless mode `msgIdx` is `Int.max`, so every
        // `guard msgIdx < messages.count` write-site bails out. `currentTextBlockIdx`
        // therefore stays nil, and the `.textDelta` handler — which accumulates
        // `result.assistantText` only `if let blockIdx = currentTextBlockIdx` —
        // accumulated NOTHING. A sub-agent thus completed its first turn with an
        // empty result (0 tool calls, 1 turn, no text). Track the open/closed
        // state of a text block separately so data collection does not depend on
        // whether a UI block could be created.
        var textContentActive = false"""

if anchor1 not in src:
    sys.exit("FATAL: patch 1 anchor not found (SSEStream local state block)")
src = src.replace(anchor1, repl1, 1)

# ===========================================================================
# Patch 2 — contentBlockStart / .text
# Do the UI append only when it is addressable; always mark the text block open.
# ===========================================================================
anchor2 = """                    if currentTextBlockIdx == nil {
                        let idx = await MainActor.run {
                            guard msgIdx < messages.count else { return -1 }
                            let blockCount = messages[msgIdx].blocks.count"""
repl2 = """                    if currentTextBlockIdx == nil {
                        // [ci-fix-headless] Mark the text block open unconditionally.
                        // The UI append below is skipped in headless mode (its guard
                        // returns -1), but `.textDelta` must still see this as an
                        // active text block so `result.assistantText` accumulates.
                        textContentActive = true
                        let idx = await MainActor.run {
                            guard msgIdx < messages.count else { return -1 }
                            let blockCount = messages[msgIdx].blocks.count"""

if anchor2 not in src:
    sys.exit("FATAL: patch 2 anchor not found (contentBlockStart .text branch)")
src = src.replace(anchor2, repl2, 1)

# ===========================================================================
# Patch 3 — `.textDelta` gates on the UI index; gate it on the new flag and
# keep the UI flush guarded by `currentTextBlockIdx` explicitly.
# ===========================================================================
anchor3 = """            case .textDelta(let text):
                if let blockIdx = currentTextBlockIdx {
                    result.assistantText += text
"""
repl3 = """            case .textDelta(let text):
                // [ci-fix-headless] Was `if let blockIdx = currentTextBlockIdx`, which
                // is nil in headless mode, so the accumulation never ran. Accumulate
                // on the logical flag and use the UI index (optional) only for writes.
                if textContentActive {
                    let blockIdx = currentTextBlockIdx ?? -1
                    result.assistantText += text
"""
if anchor3 not in src:
    sys.exit("FATAL: patch 3 anchor not found (.textDelta accumulation)")
src = src.replace(anchor3, repl3, 1)

# ===========================================================================
# Patch 4 — contentBlockStop / .toolCallComplete
# `guard blockIdx >= 0 else { continue }` skipped the toolEntry append in
# headless mode. Suppress only the UI flush; keep the record.
# ===========================================================================
anchor4 = """                currentStreamingToolId = nil
                guard blockIdx >= 0 else { continue }
                // Harvest the per-tool streaming-chunk ring before it goes out
                // of scope — the preflight validator downstream uses it for
                // diagnosis when args turn out to be empty / missing fields.
                let harvestedRing = toolInputChunkRings.removeValue(forKey: id) ?? []
                result.toolEntries.append(StreamResult.ToolEntry(id: id, name: name, args: args, blockIdx: blockIdx, metadata: metadata, inputChunkRing: harvestedRing))"""
repl4 = """                currentStreamingToolId = nil
                // [ci-fix-headless] `guard blockIdx >= 0 else { continue }` used to
                // sit here and, in headless mode (msgIdx == Int.max), it returned -1
                // and skipped the `result.toolEntries.append(...)` below — the tool
                // call was dropped and the sub-agent saw an empty turn. The append is
                // what feeds `SubAgentRunner.run()`, so it must run unconditionally.
                // Downstream UI writes are all guarded by
                // `msgIdx < messages.count && blockIdx < messages[msgIdx].blocks.count`
                // and therefore short-circuit safely on a -1 index.
                // Harvest the per-tool streaming-chunk ring before it goes out
                // of scope — the preflight validator downstream uses it for
                // diagnosis when args turn out to be empty / missing fields.
                let harvestedRing = toolInputChunkRings.removeValue(forKey: id) ?? []
                result.toolEntries.append(StreamResult.ToolEntry(id: id, name: name, args: args, blockIdx: blockIdx, metadata: metadata, inputChunkRing: harvestedRing))"""
if anchor4 not in src:
    sys.exit("FATAL: patch 4 anchor not found (toolCallComplete append)")
src = src.replace(anchor4, repl4, 1)

# ===========================================================================
# Patch 5 — reset the flag where the text block is closed, so a second text
# block in the same stream is tracked correctly.
# ===========================================================================
anchor5 = """                // Reset text block tracking for potential next text block after tool use
                currentTextBlockIdx = nil
                result.assistantText = ""
                result.spokenTextOffset = 0"""
repl5 = """                // Reset text block tracking for potential next text block after tool use
                // [ci-fix-headless] Keep the logical flag in step with the UI index.
                currentTextBlockIdx = nil
                textContentActive = false
                result.assistantText = ""
                result.spokenTextOffset = 0"""
if anchor5 not in src:
    sys.exit("FATAL: patch 5 anchor not found (text tracking reset)")
src = src.replace(anchor5, repl5, 1)

SSE.write_text(src)
print(f"[APPLY  ] wrote 5 patches to {SSE.relative_to(root)}")
print("[fix] done — sub-agent tool calls and text should now accumulate")

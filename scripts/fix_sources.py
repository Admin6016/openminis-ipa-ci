#!/usr/bin/env python3
"""
Source-compatibility patch for OpenMinis-dev (private fork).

The fork's parallel-sub-agent feature does not compile as committed: three
cross-file visibility problems in

  src/ios/Agent/Chat/AIChatViewModel+ConcurrentTools.swift
  src/ios/Agent/Chat/AIChatViewModel.swift
  src/ios/Agent/Chat/SubAgentRunner.swift

Design rules:
  * A single FEATURE GATE decides whether the feature is present at all. If it
    is not, every fix reports SKIP and the script exits 0 — a tree that never
    had the feature must still build untouched.
  * Each fix is keyed on a MARKER (text only produced by that fix). Marker
    present -> ALREADY, so the script is safe to re-run and survives a rebase
    that left the feature alone.
  * Marker absent AND anchor absent -> SKIP with a reason, never a crash.
  * Marker absent but anchor present -> APPLY.

Usage: python3 fix_sources.py <repo-root>
Exit code is always 0 unless the tree is unreadable.
"""
import re
import sys
from pathlib import Path

root = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
chat = root / "src" / "ios" / "Agent" / "Chat"
CT = chat / "AIChatViewModel+ConcurrentTools.swift"
VM = chat / "AIChatViewModel.swift"
SR = chat / "SubAgentRunner.swift"

applied = already = skipped = 0


def report(kind, desc, detail=""):
    global applied, already, skipped
    if kind == "apply":
        applied += 1
    elif kind == "already":
        already += 1
    else:
        skipped += 1
    print(f"[{kind.upper():7s}] {desc}" + (f"  — {detail}" if detail else ""))


def sub(path: Path, desc: str, old: str, new: str, marker: str):
    """Replace `old` with `new`, guarded by `marker` for idempotency."""
    if not path.exists():
        report("skip", desc, f"{path.name} absent")
        return
    s = path.read_text()
    if marker in s:
        report("already", desc)
        return
    n = s.count(old)
    if n != 1:
        report("skip", desc, f"anchor found {n}x in {path.name} (code changed upstream?)")
        return
    path.write_text(s.replace(old, new, 1))
    report("apply", desc)


def sub_regex(path: Path, desc: str, pattern: str, repl: str, marker: str):
    if not path.exists():
        report("skip", desc, f"{path.name} absent")
        return
    s = path.read_text()
    if marker in s:
        report("already", desc)
        return
    n = len(re.findall(pattern, s))
    if n == 0:
        report("skip", desc, f"no match in {path.name}")
        return
    path.write_text(re.sub(pattern, repl, s))
    report("apply", desc, f"{n} site(s)")


# ---------------------------------------------------------------------------
# FEATURE GATE
# The sub-agent feature always introduces SubAgentRunner.swift and a
# `spawn_agents` tool. Require both; otherwise leave the tree alone.
# ---------------------------------------------------------------------------
gate_ok = SR.exists() and CT.exists() and "spawn_agents" in CT.read_text()
if not gate_ok:
    reason = "SubAgentRunner.swift absent" if not SR.exists() else \
             "ConcurrentTools.swift absent" if not CT.exists() else \
             "no 'spawn_agents' reference"
    print(f"[GATE   ] parallel-sub-agent feature not present ({reason}) — nothing to patch")
    for d in (
        "ConcurrentTools: add file-private sub-agent logger",
        "AIChatViewModel: widen baseSystemPrompt to internal",
        "SubAgentRunner: qualify BatchImageBudget",
        "SubAgentRunner: qualify ToolExecOutcome",
    ):
        report("skip", d, "feature gate")
    print(f"\n[fix] applied={applied} already={already} skipped={skipped}")
    sys.exit(0)

print("[GATE   ] parallel-sub-agent feature detected — applying compatibility fixes")

# ---------------------------------------------------------------------------
# 1. ConcurrentTools logs sub-agent progress via `saLogger`, which is declared
#    file-private in SubAgentModels.swift and SubAgentRunner.swift — not
#    visible here. Give this file its own.
# ---------------------------------------------------------------------------
sub(
    CT,
    "ConcurrentTools: add file-private sub-agent logger",
    'private let ctLogger = AppLogger(category: "AIChatVM")',
    'private let ctLogger = AppLogger(category: "AIChatVM")\n'
    '// [ci-fix] Sub-agent progress logging from this file. The `saLogger` in\n'
    '// SubAgentModels/SubAgentRunner is file-private and not visible here.\n'
    'private let saLogger = AppLogger(category: "SubAgent")',
    marker='private let saLogger',
)

# ---------------------------------------------------------------------------
# 2. subAgentSystemPrompt() reads baseSystemPrompt, declared `private` in
#    AIChatViewModel.swift and therefore invisible from this file.
# ---------------------------------------------------------------------------
sub(
    VM,
    "AIChatViewModel: widen baseSystemPrompt to internal",
    "\n    private var baseSystemPrompt: String {",
    "\n    // [ci-fix] was `private`; ConcurrentTools.subAgentSystemPrompt() reads\n"
    "    // it from another file, so it must be at least internal.\n"
    "    var baseSystemPrompt: String {",
    marker="// [ci-fix] was `private`; ConcurrentTools.subAgentSystemPrompt()",
)

# ---------------------------------------------------------------------------
# 3. SubAgentRunner references BatchImageBudget / ToolExecOutcome, which are
#    nested inside `extension AIChatViewModel`, so their qualified names are
#    AIChatViewModel.BatchImageBudget / AIChatViewModel.ToolExecOutcome.
# ---------------------------------------------------------------------------
sub_regex(
    SR,
    "SubAgentRunner: qualify BatchImageBudget",
    r"(?<!\.)\bBatchImageBudget\b",
    "AIChatViewModel.BatchImageBudget",
    marker="AIChatViewModel.BatchImageBudget",
)
sub_regex(
    SR,
    "SubAgentRunner: qualify ToolExecOutcome",
    r"(?<!\.)\bToolExecOutcome\b",
    "AIChatViewModel.ToolExecOutcome",
    marker="AIChatViewModel.ToolExecOutcome",
)

print(f"\n[fix] applied={applied} already={already} skipped={skipped}")
sys.exit(0)

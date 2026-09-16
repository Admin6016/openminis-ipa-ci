#!/usr/bin/env python3
r"""
Two more changes:

**(4) A global default continuation prompt for "Keep Going".**
Today `AutoContinueConfig.defaultPrompt` is a compile-time constant, so the
per-session fallback is always the same sentence. This makes it a user setting:
a single global value in UserDefaults, edited once in Settings, used as the
fallback for every session. Existing per-session overrides keep working —
the global value is only consulted where the literals used to be.

**(5) Raise the concurrent-session ceiling from 5 to 10.**
`SessionConcurrencyManager.maxConcurrent` gates how many sessions may have an
LLM request in flight; sessions past the cap are FIFO-suspended. The stale
comment in SubAgentModels.swift still says 5 while the code says 6 — both are
updated so the two numbers stop disagreeing.

Usage: python3 fix_settings_extras.py <repo-root>
"""
import sys
from pathlib import Path

root = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
CH = root / "src" / "ios" / "Agent" / "Chat"
LIFE = CH / "ChatLifecycleSupport.swift"
SUBM = CH / "SubAgentModels.swift"

for p in (LIFE, SUBM):
    if not p.exists():
        sys.exit(f"FATAL: missing {p}")

edits = []


def edit(path, desc, old, new, count=1):
    s = path.read_text()
    n = s.count(old)
    if n != count:
        sys.exit(f"FATAL: {desc}\n  {path.name}: expected {count}, found {n}\n  anchor: {old[:110]!r}")
    path.write_text(s.replace(old, new, count))
    edits.append(desc)


# ==========================================================================
# 5. Concurrent sessions 5 -> 10.
# ==========================================================================
edit(LIFE, "SessionConcurrencyManager: 5 -> 10 concurrent sessions",
     r"""    let maxConcurrent: Int = 5""",
     r"""    /// [ci-fix-settings] Raised 5 -> 10. The old value suspended the 6th
    /// session even when the device had headroom; each suspended session also
    /// holds a waiter continuation, so the FIFO queue drained slowly. 10 keeps
    /// a realistic multi-session workflow (a few long agent runs plus short
    /// side chats) running concurrently. Raise further only with evidence the
    /// request budget can take it — see SubAgentBudget for the sibling cap on
    /// sub-agents, which is deliberately much lower.
    let maxConcurrent: Int = 10""")

edit(SUBM, "SubAgentModels: refresh the stale cap reference",
     r"""/// [T-parallel-subagents] `SessionConcurrencyManager.maxConcurrent` (5) gates
/// *sessions*, not agents — sub-agents live inside one session and must not
/// acquire those slots. Without a separate global limiter, N sessions each
/// fanning out M sub-agents would put N×M LLM streams in flight against a
/// budget sized for 5. This actor is the missing ceiling.""",
     r"""/// [T-parallel-subagents] `SessionConcurrencyManager.maxConcurrent` gates
/// *sessions*, not agents — sub-agents live inside one session and must not
/// acquire those slots. Without a separate global limiter, N sessions each
/// fanning out M sub-agents would put N×M LLM streams in flight against a
/// budget sized for a handful of sessions. This actor is the missing ceiling.""")

# ==========================================================================
# 4. Global default continuation prompt.
# ==========================================================================
AC = CH / "AutoContinue.swift"
if not AC.exists():
    print("[SKIP   ] AutoContinue.swift not installed — global default prompt not wired")
else:
    edit(AC, "AutoContinueConfig: read the global default prompt",
         # The original spans TWO lines (declaration + its string literal on the
         # next line), so the anchor must include both — anchoring on the bare
         # declaration leaves the literal orphaned and the file will not parse.
         '    static let defaultPrompt =\n'
         '        "继续。不要停下来问我，也不要总结进度——直接接着上一步做下去，直到目标完整达成。"',
         "    /// [ci-fix-settings] The BUILT-IN fallback, used only when the user has\n"
         "    /// never set a global default. Prefer `resolvedDefaultPrompt`, which reads\n"
         "    /// the user's setting first.\n"
         "    static let builtinPrompt =\n"
         "        \"继续。不要停下来问我，也不要总结进度——直接接着上一步做下去，直到目标完整达成。\"\n"
         "\n"
         "    /// [ci-fix-settings] UserDefaults key for the app-wide default continuation\n"
         "    /// prompt, edited once in Settings and used as the fallback for every\n"
         "    /// session. Stored as a plain string (not JSON) so it is inspectable and\n"
         "    /// survives schema churn.\n"
         "    static let globalPromptDefaultsKey = \"autoContinueGlobalDefaultPrompt\"\n"
         "\n"
         "    /// [ci-fix-settings] The global default, or the built-in when unset/blank.\n"
         "    static var resolvedDefaultPrompt: String {\n"
         "        let raw = UserDefaults.standard.string(forKey: globalPromptDefaultsKey)?\n"
         "            .trimmingCharacters(in: .whitespacesAndNewlines) ?? \"\"\n"
         "        return raw.isEmpty ? builtinPrompt : raw\n"
         "    }\n"
         "\n"
         "    /// Persist the app-wide default. An empty value clears the override so the\n"
         "    /// built-in applies again.\n"
         "    @MainActor\n"
         "    static func setGlobalDefaultPrompt(_ text: String?) {\n"
         "        let trimmed = text?.trimmingCharacters(in: .whitespacesAndNewlines) ?? \"\"\n"
         "        if trimmed.isEmpty {\n"
         "            UserDefaults.standard.removeObject(forKey: globalPromptDefaultsKey)\n"
         "        } else {\n"
         "            UserDefaults.standard.set(trimmed, forKey: globalPromptDefaultsKey)\n"
         "        }\n"
         "        NotificationCenter.default.post(name: .autoContinueGlobalPromptChanged, object: nil)\n"
         "    }\n"
         "\n"
         "    /// Back-compat alias. Several call sites say `defaultPrompt`; keep the name\n"
         "    /// working but make it resolve through the user setting.\n"
         "    static var defaultPrompt: String { resolvedDefaultPrompt }")

    # All the existing fallback sites keep working unchanged because they use
    # `defaultPrompt`, which is now the resolved value. Nothing further to do.

    edit(AC, "AutoContinue: declare the global-prompt-changed notification",
         r"""private let acLogger = AppLogger(category: "AutoContinue")""",
         r"""private let acLogger = AppLogger(category: "AutoContinue")

extension Notification.Name {
    /// [ci-fix-settings] Posted when the app-wide default continuation prompt
    /// changes, so any open sheet can refresh its placeholder.
    static let autoContinueGlobalPromptChanged = Notification.Name("autoContinueGlobalPromptChanged")
}""")

    # ----------------------------------------------------------------------
    # Settings UI: a global section so the default is editable app-wide.
    # ----------------------------------------------------------------------
    edit(AC, "AutoContinueSheet: global default prompt section",
         r"""    @State private var enabled: Bool
    @State private var prompt: String

    init(vm: AIChatViewModel) {
        self.vm = vm
        _enabled = State(initialValue: vm.autoContinueConfig.enabled)
        _prompt = State(initialValue: vm.autoContinueConfig.prompt)
    }""",
         r"""    @State private var enabled: Bool
    @State private var prompt: String
    /// [ci-fix-settings] Draft of the APP-WIDE default prompt.
    @State private var globalPrompt: String
    @State private var showGlobalReset = false

    init(vm: AIChatViewModel) {
        self.vm = vm
        _enabled = State(initialValue: vm.autoContinueConfig.enabled)
        _prompt = State(initialValue: vm.autoContinueConfig.prompt)
        // Seeded from the stored override, or the built-in when unset, so the
        // field always shows what would actually be sent.
        _globalPrompt = State(initialValue: UserDefaults.standard
            .string(forKey: AutoContinueConfig.globalPromptDefaultsKey)
            ?? AutoContinueConfig.builtinPrompt)
    }""")

    edit(AC, "AutoContinueSheet: add the global section to the form",
         r"""                Section {
                    Button(AppLocalized("Reset to default message")) {
                        prompt = AutoContinueConfig.defaultPrompt
                    }
                }""",
         r"""                Section {
                    Button(AppLocalized("Use the global default")) {
                        prompt = AutoContinueConfig.resolvedDefaultPrompt
                    }
                } header: {
                    Text(AppLocalized("This session"))
                } footer: {
                    Text(AppLocalized("Leave the field above empty to follow the app-wide default set below."))
                }

                // [ci-fix-settings] App-wide default. Kept in this sheet rather
                // than a separate settings screen so the two prompts are edited
                // side by side — the relationship (per-session override vs
                // global fallback) is the thing users get wrong.
                Section {
                    TextEditor(text: $globalPrompt)
                        .frame(minHeight: 80)
                        .font(.system(size: 15))
                    Button(AppLocalized("Reset to built-in")) {
                        showGlobalReset = true
                    }
                    .foregroundStyle(.red)
                    .confirmationDialog(
                        AppLocalized("Reset the global default?"),
                        isPresented: $showGlobalReset,
                        titleVisibility: .visible
                    ) {
                        Button(AppLocalized("Reset"), role: .destructive) {
                            AutoContinueConfig.setGlobalDefaultPrompt(nil)
                            globalPrompt = AutoContinueConfig.builtinPrompt
                        }
                        Button(AppLocalized("Cancel"), role: .cancel) {}
                    } message: {
                        Text(AppLocalized("New sessions will fall back to the built-in message."))
                    }
                } header: {
                    Text(AppLocalized("Default for all sessions"))
                } footer: {
                    Text(AppLocalized("Used whenever a session's own message is empty. Saving applies it immediately."))
                }""")

    # Persist the global prompt on Done, next to the per-session save.
    edit(AC, "AutoContinueSheet: save the global prompt on Done",
         r"""                        vm.setAutoContinue(enabled: enabled, prompt: prompt)
                        dismiss()""",
         r"""                        // [ci-fix-settings] Persist both: the session's own
                        // message and the app-wide default.
                        AutoContinueConfig.setGlobalDefaultPrompt(globalPrompt)
                        vm.setAutoContinue(enabled: enabled, prompt: prompt)
                        dismiss()""")

for d in edits:
    print(f"[APPLY  ] {d}")
print(f"[fix] settings extras: {len(edits)} edit(s)")

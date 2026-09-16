#!/usr/bin/env python3
"""
Wire the "Keep Going" (auto-continue) feature into the Minis host sources.

`AutoContinue.swift` is installed separately by the workflow; this script adds
the HOST-SIDE integration it needs:

  AIChatViewModel.swift
    - stored properties (config, countdown task, countdown, fired count)
    - `canResume` didSet  -> notify the mode        (the single chokepoint)
    - `sessionId` didSet  -> load the per-session config
    - `cancel()`          -> disarm the countdown on Stop
    - `resume()` split into `resume()` + `resume(continuationText:)` so the
      user's own sentence is injected instead of the generic system-reminder

  AIChatView.swift
    - sheet state + presentation
    - a menu row in BOTH the live UIKit menu and the SwiftUI mirror, plus the
      Key field that drives its checkmark

Each patch is guarded by a per-patch PROBE string that appears only in that
patch's replacement text. Using one shared marker was a bug: the first patch
inserted it, after which every later patch saw the marker and silently skipped,
leaving the tree half-patched.

Usage: python3 fix_auto_continue.py <repo-root>
"""
import sys
from pathlib import Path

root = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
VM = root / "src" / "ios" / "Agent" / "Chat" / "AIChatViewModel.swift"
AV = root / "src" / "ios" / "Views" / "Chat" / "AIChatView.swift"

if not VM.exists() or not AV.exists():
    sys.exit(f"FATAL: expected sources not found under {root}")

applied = already = 0
failures = []


def patch(name, path, old, new, count, probe):
    """Apply one patch. Idempotency is decided by PROBE OCCURRENCE COUNT, not
    mere presence: a replacement that is inserted `count` times makes the probe
    appear `count` times, so `probe_count >= count` is the already-applied
    condition. (A plain `probe in s` check silently skipped multi-site patches
    whose probe text also occurred in unrelated source.)"""
    global applied, already
    s = path.read_text()
    have = s.count(probe)
    if have >= count:
        already += 1
        print(f"[ALREADY] {name} (probe x{have})")
        return
    n = s.count(old)
    if n != count:
        failures.append(f"{name}: anchor matched {n}x, expected {count}")
        print(f"[FAIL   ] {name} (anchor x{n})")
        return
    path.write_text(s.replace(old, new))
    applied += 1
    print(f"[APPLY  ] {name}")


patch('VM: stored properties', VM,
'    @Published var parallelAgentsEnabled = true\n',
'    @Published var parallelAgentsEnabled = true\n\n    // MARK: - Auto-Continue ("Keep Going") — [T-auto-continue]\n    //\n    // An opt-in, per-session mode: when the agent stops and parks on the\n    // Resume banner, re-send a user-authored continuation prompt automatically\n    // and keep working until the goal is done or the user turns it off.\n    //\n    // The mechanism hangs off `canResume` — the app\'s single chokepoint for\n    // "stopped, waiting for the user". A normally-completed turn never sets\n    // canResume, so this fires on a genuine interruption only, not after every\n    // finished reply. See AutoContinue.swift.\n\n    /// This session\'s persisted auto-continue settings.\n    @Published var autoContinueConfig: AutoContinueConfig = .disabled\n\n    /// Pending countdown to the next automatic continuation, if armed.\n    /// Not `@Published`: it is machinery; the UI reads the storage below.\n    var autoContinueCountdownTask: Task<Void, Never>?\n\n    /// Seconds left on the countdown; 0 when idle.\n    @Published var autoContinueCountdownStorage: Int = 0\n\n    /// How many times this session has auto-continued. Shown in the settings\n    /// sheet so the user can confirm the mode is working (and notice if it is\n    /// working more than they expected).\n    @Published var autoContinueFiredCount: Int = 0\n\n    /// True while `fireAutoContinue` is driving a send, so the user-input\n    /// reset hook can distinguish the mode own sends from real user input.\n    var isAutoContinueSending: Bool = false\n', 1, '// MARK: - Auto-Continue')

patch('VM: canResume didSet hook', VM,
'    var isRedetectingInterruptedTail = false\n\n    @Published var canResume = false {\n        didSet {',
'    var isRedetectingInterruptedTail = false\n\n    @Published var canResume = false {\n        didSet {\n            // [T-auto-continue] Single chokepoint for every "the session\n            // stopped and is waiting for the user" transition.\n            if oldValue != canResume {\n                autoContinueOnCanResumeChanged(canResume)\n            }', 1, 'autoContinueOnCanResumeChanged(canResume)')

# The interruption sites set canResume INSIDE the loop, while isProcessing is
# still true; the loop only idles later. So the countdown must also be armed
# from the isProcessing falling edge, not just from canResume.
patch('VM: isProcessing falling-edge hook', VM,
    '            if isProcessing && !oldValue {\n                // Agent loop starting',
    '\n'.join([
        '            // [T-auto-continue] The interruption flags (canResume) are set',
        '            // INSIDE the loop, while isProcessing is still true. This falling',
        '            // edge is where the session actually goes idle, so it is the other',
        '            // half of the arming condition.',
        '            if !isProcessing && oldValue {',
        '                autoContinueOnProcessingChanged(false)',
        '            }',
        '            if isProcessing && !oldValue {',
        '                // Agent loop starting',
    ]), 1, 'autoContinueOnProcessingChanged(false)')

patch('VM: sessionId didSet hook', VM,
'    var sessionId: String? {\n        didSet { browserTabPool.sessionId = sessionId }\n    }',
'    var sessionId: String? {\n        didSet {\n            browserTabPool.sessionId = sessionId\n            // [T-auto-continue] The mode is per-session; swap it in when the\n            // bound session changes (different chat, or a new draft).\n            if oldValue != sessionId {\n                loadAutoContinueConfig()\n            }\n        }\n    }', 1, 'loadAutoContinueConfig()')

# A user-authored send means a new goal, so the consecutive-continuation
# counter restarts. The callee ignores the mode own sends.
patch('VM: user-send resets counter', VM,
    '        guard !text.isEmpty || !pendingAttachments.isEmpty, !isProcessing else {',
    '\n'.join([
        '        // [T-auto-continue] A real user send starts a fresh goal, so the',
        '        // consecutive-continuation counter resets. The mode own sends',
        '        // are excluded inside the callee.',
        '        autoContinueNoteUserInput()',
        '        guard !text.isEmpty || !pendingAttachments.isEmpty, !isProcessing else {',
    ]), 1, 'autoContinueNoteUserInput()')

patch('VM: cancel() disarms', VM,
'func cancel() {\n        let lastBlocks = (messages.last?.role == .assistant)',
'func cancel() {\n        // [T-auto-continue] Stop cancels a pending automatic continuation. The\n        // mode itself stays armed — Stop means "not right now" — so the next\n        // genuine interruption picks up again.\n        autoContinueHandleUserStop()\n        let lastBlocks = (messages.last?.role == .assistant)', 1, 'autoContinueHandleUserStop()')

patch('VM: resume overload', VM,
'    func resume() {\n        guard !isProcessing, canResume else { return }',
'    func resume() {\n        resume(continuationText: nil)\n    }\n\n    /// [T-auto-continue] `resume()` with an optional caller-supplied\n    /// continuation message.\n    ///\n    /// Manual Resume passes nil and keeps the original `<system-reminder>`\n    /// ("the user stopped the previous response"), which correctly describes a\n    /// user-initiated cancel. Auto-continue passes the user\'s own preset\n    /// sentence instead — the whole point of the mode is that the user\n    /// authored what gets said next, and the system-reminder framing would be\n    /// wrong there because nobody stopped anything; the agent simply parked.\n    func resume(continuationText: String?) {\n        guard !isProcessing, canResume else { return }', 1, 'func resume(continuationText: String?)')

patch('VM: inject continuation text', VM,
'let continueMsg = AgentMessage(role: .user, parts: [\n                .text("<system-reminder>The user stopped the previous response but now wants to continue. Pick up exactly where you left off.</system-reminder>")\n            ])',
'// [T-auto-continue] Auto-continue supplies the user\'s own preset\n            // sentence; manual Resume keeps the original system-reminder.\n            let text = continuationText ?? "<system-reminder>The user stopped the previous response but now wants to continue. Pick up exactly where you left off.</system-reminder>"\n            let continueMsg = AgentMessage(role: .user, parts: [.text(text)])', 1, 'let text = continuationText ??')

patch('View: sheet state', AV,
'    @State private var showSessionMemory = false',
'    @State private var showSessionMemory = false\n    /// [T-auto-continue] "Keep Going" settings sheet.\n    @State private var showAutoContinue = false', 1, '@State private var showAutoContinue = false')

patch('View: sheet presentation', AV,
'        .sheet(isPresented: $showSessionMemory) {\n            SessionMemoryView(vm: cached.vm)\n        }',
'        .sheet(isPresented: $showSessionMemory) {\n            SessionMemoryView(vm: cached.vm)\n        }\n        // [T-auto-continue] "Keep Going" editor.\n        .sheet(isPresented: $showAutoContinue) {\n            AutoContinueSheet(vm: cached.vm)\n        }', 1, 'AutoContinueSheet(vm: cached.vm)')

# [T-auto-continue] One-tap switch in the composer row, right of `/`.
patch('View: keep-going button in composer row', AV,
    '            attachmentMenuButton\n            slashMenuButton\n            if vm.editingMessageIndex != nil { editExitButton }',
    '\n'.join([
        '            attachmentMenuButton',
        '            slashMenuButton',
        '            // [T-auto-continue] One-tap Keep Going switch, right of the',
        '            // slash button. Tap toggles; long-press opens the editor so the',
        '            // continuation message can be changed without going back to the',
        '            // overflow menu.',
        '            keepGoingButton',
        '            if vm.editingMessageIndex != nil { editExitButton }',
    ]), 1, 'keepGoingButton')

# The button itself, placed just before the slashMenuButton definition.
patch('View: keep-going button implementation', AV,
    '    /// `/` button that opens the slash command menu.',
    '\n'.join([
        '    /// [T-auto-continue] One-tap "Keep Going" switch.',
        '    ///',
        '    /// Tap toggles the per-session mode; long-press opens the sheet to edit',
        '    /// the continuation message. Styled like its neighbours (`+` and `/`) so',
        '    /// the row reads as one set of composer controls, with the accent fill',
        '    /// reserved for the ON state.',
        '    private var keepGoingButton: some View {',
        '        let on = vm.autoContinueEnabled',
        '        let armed = vm.autoContinueArmed',
        '        return Button {',
        '            vm.setAutoContinue(enabled: !on, prompt: nil)',
        '        } label: {',
        '            Image(systemName: on ? "infinity.circle.fill" : "infinity")',
        '                .font(.system(size: 17, weight: .medium))',
        '                .foregroundStyle(on ? Color.white : ChatColors.secondaryText)',
        '                .frame(width: 34, height: 34)',
        '                .background(on ? Color.accentColor : ChatColors.inputIconBg)',
        '                .clipShape(Circle())',
        '                .overlay(Circle().stroke(ChatColors.inputIconBorder, lineWidth: 0.5))',
        '                .overlay(alignment: .bottomTrailing) {',
        '                    // Countdown pip: while a continuation is counting down,',
        '                    // show the seconds left so a pending send is visible',
        '                    // rather than looking like nothing is happening.',
        '                    if armed, vm.autoContinueCountdown > 0 {',
        '                        Text("\(vm.autoContinueCountdown)")',
        '                            .font(.system(size: 9, weight: .bold))',
        '                            .foregroundStyle(.white)',
        '                            .padding(.horizontal, 3)',
        '                            .background(Capsule().fill(Color.orange))',
        '                            .offset(x: 3, y: 3)',
        '                    }',
        '                }',
        '        }',
        '        .buttonStyle(.plain)',
        '        .accessibilityLabel(Text("Keep Going"))',
        '        .accessibilityValue(Text(on ? AppLocalized("On") : AppLocalized("Off")))',
        '        .accessibilityHint(Text(AppLocalized("Automatically continues the conversation until the goal is done")))',
        '        .simultaneousGesture(LongPressGesture().onEnded { _ in',
        '            showAutoContinue = true',
        '        })',
        '    }',
        '',
        '    /// `/` button that opens the slash command menu.',
    ]), 1, 'keepGoingButton: some View')

patch('View: menu call site action', AV,
'            onMemories: { showSessionMemory = true },',
'            onMemories: { showSessionMemory = true },\n            // [T-auto-continue] Opens the "Keep Going" editor.\n            onAutoContinue: { showAutoContinue = true },', 1, 'onAutoContinue: { showAutoContinue = true }')

patch('View: pass enabled flag', AV,
'            showFastModeToggle: activeModelSupportsFastMode,\n            fastModeEnabled: codexFastModeEnabled,\n            onNewChat: { requestNewChatFromMenu() },',
'            showFastModeToggle: activeModelSupportsFastMode,\n            fastModeEnabled: codexFastModeEnabled,\n            // [T-auto-continue] Drives the menu checkmark.\n            autoContinueEnabled: cached.vm.autoContinueEnabled,\n            onNewChat: { requestNewChatFromMenu() },', 1, 'autoContinueEnabled: cached.vm.autoContinueEnabled')

patch('View: SwiftUI menu properties', AV,
'    /// [T-codex-fast-mode] Mirrors ChatTrailingMenuButton.\n    let showFastModeToggle: Bool\n    let fastModeEnabled: Bool\n\n    let onNewChat: () -> Void',
'    /// [T-codex-fast-mode] Mirrors ChatTrailingMenuButton.\n    let showFastModeToggle: Bool\n    let fastModeEnabled: Bool\n    /// [T-auto-continue] Mirrors the persisted per-session flag.\n    let autoContinueEnabled: Bool\n\n    let onNewChat: () -> Void', 1, 'Mirrors the persisted per-session flag')

patch('View: SwiftUI/UIKit menu param', AV,
'    let onMemories: () -> Void\n    let setSpeakEnabled: (Bool) -> Void\n    let setEnhancedCache: (Bool) -> Void',
'    let onMemories: () -> Void\n    /// [T-auto-continue] Opens the "Keep Going" editor.\n    let onAutoContinue: () -> Void\n    let setSpeakEnabled: (Bool) -> Void\n    let setEnhancedCache: (Bool) -> Void', 2, 'Opens the "Keep Going" editor')

patch('View: SwiftUI == adds field', AV,
'            && lhs.fastModeEnabled == rhs.fastModeEnabled\n    }',
'            && lhs.fastModeEnabled == rhs.fastModeEnabled\n            && lhs.autoContinueEnabled == rhs.autoContinueEnabled\n    }', 1, '&& lhs.autoContinueEnabled == rhs.autoContinueEnabled')

patch('View: SwiftUI menu row', AV,
'                Label(AppLocalized("Speak Responses"), systemImage: "speaker.wave.2")\n            }\n\n            // [T-codex-fast-mode-menu-group]',
'                Label(AppLocalized("Speak Responses"), systemImage: "speaker.wave.2")\n            }\n\n            // [T-auto-continue] Mirrors the UIKit buildMenu — its own group,\n            // since it changes how the agent is driven rather than what it can\n            // reach. Tapping opens the editor (message + on/off).\n            Divider()\n\n            Button { onAutoContinue() } label: {\n                Label(\n                    autoContinueEnabled\n                        ? AppLocalized("Keep Going — On")\n                        : AppLocalized("Keep Going"),\n                    systemImage: autoContinueEnabled ? "infinity.circle.fill" : "infinity")\n            }\n\n            // [T-codex-fast-mode-menu-group]', 1, 'Keep Going — On')

# The UIKit struct's declaration is distinguished from the SwiftUI mirror by
# the Codex doc-comment that precedes its own showFastModeToggle.
patch('View: UIKit struct property', AV,
    '    /// Codex OAuth instance (OpenAI OAuth, no custom base).\n    let showFastModeToggle: Bool\n    let fastModeEnabled: Bool\n\n    let onNewChat: () -> Void',
'''    /// Codex OAuth instance (OpenAI OAuth, no custom base).
    let showFastModeToggle: Bool
    let fastModeEnabled: Bool
    /// [T-auto-continue] Mirrors the persisted per-session flag.
    let autoContinueEnabled: Bool

    let onNewChat: () -> Void''', 1, 'no custom base).\n    let showFastModeToggle: Bool\n    let fastModeEnabled: Bool\n    /// [T-auto-continue]')

patch('View: UIKit Key field', AV,
'        let showFastModeToggle: Bool\n        let fastModeEnabled: Bool\n    }',
'        let showFastModeToggle: Bool\n        let fastModeEnabled: Bool\n        /// [T-auto-continue] Menu shows a checkmark when the mode is armed.\n        let autoContinueEnabled: Bool\n    }', 1, 'Menu shows a checkmark when the mode is armed')

patch('View: UIKit Key initialiser', AV,
'            showFastModeToggle: showFastModeToggle,\n            fastModeEnabled: fastModeEnabled)',
'            showFastModeToggle: showFastModeToggle,\n            fastModeEnabled: fastModeEnabled,\n            autoContinueEnabled: autoContinueEnabled)', 1, 'autoContinueEnabled: autoContinueEnabled)')

patch('View: UIKit menu row', AV,
'            coordinator.parent.setSpeakEnabled(!key.speakEnabled)\n        })\n        groups.append(UIMenu(options: .displayInline, children: sessionGroup))',
'            coordinator.parent.setSpeakEnabled(!key.speakEnabled)\n        })\n        groups.append(UIMenu(options: .displayInline, children: sessionGroup))\n\n        // [T-auto-continue] "Keep Going" gets its own group: it changes how\n        // the agent is DRIVEN, not what it can reach, so it should not read as\n        // one more capability toggle. State shows armed/off at a glance.\n        groups.append(UIMenu(options: .displayInline, children: [\n            UIAction(title: AppLocalized("Keep Going"),\n                     image: UIImage(systemName: "infinity"),\n                     state: key.autoContinueEnabled ? .on : .off) { _ in coordinator.parent.onAutoContinue() },\n        ]))', 1, 'key.autoContinueEnabled ? .on : .off')

print(f"\n[fix_auto_continue] applied={applied} already={already}")
if failures:
    print("FAILED ANCHORS:", file=sys.stderr)
    for f in failures:
        print("  -", f, file=sys.stderr)
    sys.exit(1)
print("OK")

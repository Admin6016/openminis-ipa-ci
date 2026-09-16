//
//  AutoContinue.swift
//  MinisApp
//
//  [T-auto-continue] "Keep Going" — an opt-in, per-session mode where the
//  agent, instead of parking on the Resume banner waiting for the user,
//  automatically re-sends a user-authored continuation prompt and keeps
//  working until the goal is reached or the user turns the mode off.
//
//  ## Why it hangs off `canResume` and not `isProcessing`
//
//  `canResume` is the app's single chokepoint for "this session stopped and
//  is waiting for the user to continue": every interruption path sets it, and
//  its `didSet` in AIChatViewModel is documented as the one place that sees
//  all of those writes. A normal, complete turn leaves `canResume == false`
//  (the loop simply ran out of tool calls and finished), which is exactly the
//  distinction we need — auto-continue must fire when the agent was CUT OFF,
//  not every time it completes a reply. Hooking `isProcessing → false`
//  instead would re-prompt after every single finished turn, which is not
//  what "continue until the goal is done" means.
//
//  ## Lifecycle
//
//  canResume true   → arm a countdown, then continue (which clears canResume)
//  canResume false  → cancel any pending countdown
//  user taps Stop   → cancel the pending countdown (mode stays armed)
//
//  ## Persistence
//
//  Per session, in UserDefaults under "autoContinue.<sessionId>". Deliberately
//  NOT in SessionInferenceConfig: that store carries iCloud sync, tombstones
//  and a lenient decoder, and this is a per-device behavioural toggle that has
//  no business round-tripping through the sync engine. `autoCompactEnabled`
//  already establishes UserDefaults as the precedent for this class of flag.
//

import Foundation
import SwiftUI

private let acLogger = AppLogger(category: "AutoContinue")

// MARK: - Persisted per-session configuration

/// Per-session "Keep Going" settings.
struct AutoContinueConfig: Codable, Equatable {
    /// Whether the mode is armed for this session.
    var enabled: Bool
    /// The user's continuation prompt, re-sent every time the agent parks.
    var prompt: String

    static let defaultPrompt =
        "继续。不要停下来问我，也不要总结进度——直接接着上一步做下去，直到目标完整达成。"

    static let disabled = AutoContinueConfig(enabled: false, prompt: defaultPrompt)
}

// MARK: - View model surface

extension AIChatViewModel {

    /// Delay before the automatic continuation is sent. Short enough that a
    /// run of back-to-back goals is not perceptibly slowed, long enough that
    /// the user can hit Stop if the model just finished something they were
    /// happy with.
    static let autoContinueDelaySeconds = 5

    /// UserDefaults key for a session's config. New (unsaved) drafts use
    /// `autoContinueDraftKey` and migrate onto the real id on first save.
    static func autoContinueDefaultsKey(for sessionId: String?) -> String {
        "autoContinue.\(sessionId ?? autoContinueDraftKey)"
    }
    static let autoContinueDraftKey = "__draft__"

    // MARK: Derived UI state

    /// Whether "Keep Going" is armed for the current session.
    var autoContinueEnabled: Bool { autoContinueConfig.enabled }

    /// The user's continuation prompt for the current session.
    var autoContinuePrompt: String { autoContinueConfig.prompt }

    /// Seconds remaining before the armed continuation fires; 0 when idle.
    var autoContinueCountdown: Int { autoContinueCountdownStorage }

    /// True while a continuation is counting down — drives the banner.
    var autoContinueArmed: Bool { autoContinueCountdownTask != nil }

    // MARK: Load / persist

    /// Read this session's config out of UserDefaults. Called from the
    /// `sessionId` didSet so switching sessions swaps the mode along with
    /// everything else.
    func loadAutoContinueConfig() {
        let key = Self.autoContinueDefaultsKey(for: sessionId)
        if let data = UserDefaults.standard.data(forKey: key),
           let cfg = try? JSONDecoder().decode(AutoContinueConfig.self, from: data) {
            autoContinueConfig = cfg
        } else {
            autoContinueConfig = .disabled
        }
        autoContinueFiredCount = 0
        disarmAutoContinue(reason: "session switched")
        acLogger.info("[AutoContinue] loaded sid=\(self.sessionId?.prefix(8) ?? "draft") enabled=\(autoContinueConfig.enabled)")
    }

    /// Write the current config back. Also called when a draft acquires its
    /// real id so the armed state survives session creation.
    func persistAutoContinueConfig() {
        let key = Self.autoContinueDefaultsKey(for: sessionId)
        if let data = try? JSONEncoder().encode(autoContinueConfig) {
            UserDefaults.standard.set(data, forKey: key)
        }
        // When a new session is created from a draft, carry the draft's config
        // over so enabling the mode before the first message isn't lost.
        if let sid = sessionId, sid != Self.autoContinueDraftKey {
            let draftKey = Self.autoContinueDefaultsKey(for: nil)
            if UserDefaults.standard.data(forKey: draftKey) != nil {
                UserDefaults.standard.removeObject(forKey: draftKey)
            }
        }
    }

    // MARK: User actions

    /// Enable/disable the mode. Enabling with no prompt falls back to the
    /// default nudge so a one-tap enable is immediately useful.
    func setAutoContinue(enabled: Bool?, prompt: String?) {
        var cfg = autoContinueConfig
        if let enabled { cfg.enabled = enabled }
        if let prompt {
            let trimmed = prompt.trimmingCharacters(in: .whitespacesAndNewlines)
            cfg.prompt = trimmed.isEmpty ? AutoContinueConfig.defaultPrompt : trimmed
        }
        if cfg.prompt.isEmpty { cfg.prompt = AutoContinueConfig.defaultPrompt }

        let wasEnabled = autoContinueConfig.enabled
        autoContinueConfig = cfg
        persistAutoContinueConfig()

        if cfg.enabled {
            if !wasEnabled {
                acLogger.info("[AutoContinue] armed sid=\(self.sessionId?.prefix(8) ?? "draft") promptLen=\(cfg.prompt.count)")
            }
            // If the session is ALREADY parked on the Resume banner when the
            // user flips the switch, arm immediately — otherwise the user has
            // to poke the dead session once before the mode takes effect,
            // which is the opposite of the feature's purpose.
            if canResume, !isProcessing {
                armAutoContinue()
            }
        } else {
            acLogger.info("[AutoContinue] disabled sid=\(self.sessionId?.prefix(8) ?? "draft")")
            disarmAutoContinue(reason: "user disabled")
        }
    }

    /// Called from the `canResume` didSet — the single chokepoint covering
    /// every interruption path in the app.
    ///
    /// NOTE on ordering: every `canResume = true` site runs INSIDE
    /// `runAgentLoop`, i.e. while `isProcessing` is still true; the flag only
    /// flips to false later, in the task epilogue. So a `guard !isProcessing`
    /// here would never pass and the mode would never arm — which is exactly
    /// the bug the first version shipped. Instead, record the intent and let
    /// `armAutoContinue` retry until the loop has actually unwound.
    func autoContinueOnCanResumeChanged(_ nowCanResume: Bool) {
        guard autoContinueConfig.enabled else { return }
        if nowCanResume {
            armAutoContinue()
        } else {
            // canResume cleared: either the continuation began or the user
            // cancelled. Either way the pending countdown is stale.
            disarmAutoContinue(reason: "canResume cleared")
        }
    }

    /// Called from the `isProcessing` didSet when a turn ends. Pairs with
    /// `autoContinueOnCanResumeChanged`: the interruption sets `canResume`
    /// while the loop is still running, and the loop only goes idle here.
    func autoContinueOnProcessingChanged(_ processing: Bool) {
        guard !processing else { return }
        guard autoContinueConfig.enabled else { return }
        guard canResume else { return }
        armAutoContinue()
    }

    // MARK: Countdown

    /// Start (or restart) the countdown that leads to an automatic continue.
    ///
    /// Tolerates being called while the loop is still unwinding: rather than
    /// bailing out (which is what made the first version a no-op), it waits
    /// for `isProcessing` to clear, then proceeds. The wait is bounded so a
    /// stuck loop can't leave a task parked forever.
    func armAutoContinue() {
        guard autoContinueConfig.enabled else { return }
        guard canResume else { return }
        guard autoContinueCountdownTask == nil else { return }  // already armed

        let total = Self.autoContinueDelaySeconds
        autoContinueCountdownStorage = total

        acLogger.info("[AutoContinue] countdown=\(total)s sid=\(self.sessionId?.prefix(8) ?? "draft") processing=\(self.isProcessing)")

        autoContinueCountdownTask = Task { @MainActor [weak self] in
            // Phase 0 — wait for the in-flight loop to finish unwinding. Every
            // canResume=true site fires while isProcessing is still true, so
            // this phase is the normal path, not an edge case.
            var waited: Double = 0
            while let self, self.isProcessing, waited < Self.autoContinueMaxWaitSeconds {
                if Task.isCancelled { return }
                try? await Task.sleep(nanoseconds: 200_000_000)
                waited += 0.2
            }
            guard let self else { return }
            guard !self.isProcessing else {
                acLogger.info("[AutoContinue] gave up waiting for idle after \(Int(waited))s")
                self.autoContinueCountdownStorage = 0
                self.autoContinueCountdownTask = nil
                return
            }

            // Phase 1 — visible countdown, so the user can Stop it.
            for remaining in stride(from: total, through: 1, by: -1) {
                self.autoContinueCountdownStorage = remaining
                do {
                    try await Task.sleep(nanoseconds: 1_000_000_000)
                } catch {
                    return  // cancelled
                }
                if Task.isCancelled { return }
            }
            self.autoContinueCountdownStorage = 0
            self.autoContinueCountdownTask = nil
            self.fireAutoContinue()
        }
    }

    /// Upper bound on how long `armAutoContinue` waits for the loop to unwind
    /// before giving up. The loop's own epilogue is immediate; this only
    /// matters if it is wedged.
    static let autoContinueMaxWaitSeconds: Double = 60

    /// Cancel the pending countdown. Safe to call when nothing is armed.
    func disarmAutoContinue(reason: String) {
        guard autoContinueCountdownTask != nil || autoContinueCountdownStorage != 0 else { return }
        acLogger.info("[AutoContinue] disarm (\(reason)) sid=\(self.sessionId?.prefix(8) ?? "draft")")
        autoContinueCountdownTask?.cancel()
        autoContinueCountdownTask = nil
        autoContinueCountdownStorage = 0
    }

    /// Re-check the guards at fire time, then hand off to `resume()`, which
    /// already owns the "inject a continuation turn and re-enter the agent
    /// loop" machinery. Reusing it keeps the interrupted-tail trimming,
    /// history repair and badge handling identical to a manual Resume — and
    /// means this feature cannot drift out of sync with how Resume works.
    private func fireAutoContinue() {
        guard autoContinueConfig.enabled else {
            acLogger.info("[AutoContinue] fire skipped — disabled during countdown")
            return
        }
        guard canResume else {
            acLogger.info("[AutoContinue] fire skipped — canResume=false (already resumed?)")
            return
        }
        guard !isProcessing else {
            acLogger.info("[AutoContinue] fire skipped — isProcessing=true")
            return
        }
        acLogger.info("[AutoContinue] FIRING sid=\(self.sessionId?.prefix(8) ?? "draft")")
        autoContinueFiredCount += 1
        // Pass the user's own preset sentence rather than the generic
        // system-reminder — that is the entire point of the mode.
        resume(continuationText: autoContinueConfig.prompt)
    }

    /// Disarm the countdown when the user taps Stop.
    ///
    /// Deliberately does NOT clear `enabled`: Stop means "not right now", and
    /// leaving the mode armed is what lets the very next interruption pick up
    /// again. Turning the mode off is its own explicit action (the toolbar
    /// toggle), so the two gestures stay separable.
    func autoContinueHandleUserStop() {
        disarmAutoContinue(reason: "user tapped Stop")
    }
}

// MARK: - Settings sheet

/// Editor for the per-session "Keep Going" mode.
///
/// Presented from the chat overflow menu. Kept intentionally small: one
/// switch, one prompt field, and a plain-language note about what the mode
/// will do, because the failure mode of a mis-set prompt here is a runaway
/// overnight loop.
struct AutoContinueSheet: View {
    @Environment(\.dismiss) private var dismiss
    @ObservedObject var vm: AIChatViewModel

    @State private var enabled: Bool
    @State private var prompt: String

    init(vm: AIChatViewModel) {
        self.vm = vm
        _enabled = State(initialValue: vm.autoContinueConfig.enabled)
        _prompt = State(initialValue: vm.autoContinueConfig.prompt)
    }

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    Toggle(isOn: $enabled) {
                        Label(AppLocalized("Keep Going"), systemImage: "infinity")
                    }
                } footer: {
                    Text(AppLocalized("When the agent stops and waits for you, Minis sends the message below automatically and keeps working until the goal is done. Turn this off, or tap Stop, to cancel."))
                }

                Section {
                    TextEditor(text: $prompt)
                        .frame(minHeight: 96)
                        .font(.system(size: 15))
                        .disabled(!enabled)
                        .opacity(enabled ? 1 : 0.5)
                } header: {
                    Text(AppLocalized("Continuation message"))
                } footer: {
                    if prompt.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                        Text(AppLocalized("Empty — the default message will be used."))
                            .foregroundStyle(.secondary)
                    } else {
                        Text(AppLocalized("Sent every time the agent stops. Keep it short and imperative."))
                    }
                }

                if enabled {
                    Section {
                        LabeledContent(AppLocalized("Auto-continued so far"), value: "\(vm.autoContinueFiredCount)")
                        if vm.autoContinueArmed {
                            LabeledContent(AppLocalized("Continuing in"), value: "\(vm.autoContinueCountdown)s")
                        }
                    } header: {
                        Text(AppLocalized("This session"))
                    }
                }

                Section {
                    Button(AppLocalized("Reset to default message")) {
                        prompt = AutoContinueConfig.defaultPrompt
                    }
                }
            }
            .navigationTitle(AppLocalized("Keep Going"))
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button(AppLocalized("Done")) {
                        vm.setAutoContinue(enabled: enabled, prompt: prompt)
                        dismiss()
                    }
                }
                ToolbarItem(placement: .cancellationAction) {
                    Button(AppLocalized("Cancel")) { dismiss() }
                }
            }
        }
    }
}

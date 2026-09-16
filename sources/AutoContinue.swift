//
//  AutoContinue.swift
//  MinisApp
//
//  [T-auto-continue] "Keep Going" — an opt-in, per-session mode where, every
//  time the agent stops and would otherwise wait for the user, Minis sends a
//  user-authored continuation message and keeps working until the goal is
//  done or the user turns the mode off. It is designed to run unattended for
//  a long time (hours / overnight), which is why it has both a visible counter
//  and a backstop cap.
//
//  ## The two stop shapes it must cover
//
//  1. INTERRUPTED — the stream dropped, a tool was cancelled, the turn limit
//     was hit. `canResume` becomes true (it is the app's single chokepoint for
//     "stopped, waiting for the user"; 12 writers, all funnel through it), and
//     there is a partial tail to repair.
//  2. FINISHED NORMALLY — the model simply ended its reply, often asking
//     "shall I continue?". `canResume` stays FALSE. This is the common case
//     for a multi-step goal, and an implementation that only watches
//     `canResume` silently does nothing here.
//
//  ## Why the arming signal is the `isProcessing` falling edge
//
//  Every `canResume = true` site runs INSIDE `runAgentLoop`, i.e. while
//  `isProcessing` is still true; the flag only flips false later, in the task
//  epilogue. So `canResume`'s didSet cannot act on its own — and an earlier
//  version that guarded on `!isProcessing` there never armed at all. The
//  `isProcessing` didSet's falling edge is where the session actually goes
//  idle, so that is the trigger; `canResume` at that moment then selects WHICH
//  of the two shapes above we are in.
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
    /// The user's continuation prompt, re-sent every time the agent stops.
    var prompt: String

    static let defaultPrompt =
        "继续。不要停下来问我，也不要总结进度——直接接着上一步做下去，直到目标完整达成。"

    static let disabled = AutoContinueConfig(enabled: false, prompt: defaultPrompt)
}

// MARK: - View model surface

extension AIChatViewModel {

    /// Delay before the automatic continuation is sent. Long enough that the
    /// user can hit Stop if the model just finished something they were happy
    /// with, short enough not to perceptibly slow a run of back-to-back steps.
    static let autoContinueDelaySeconds = 5

    /// Upper bound on how long `armAutoContinue` waits for the loop to unwind
    /// before giving up. The loop's epilogue is immediate; this only matters
    /// if it is wedged.
    static let autoContinueMaxWaitSeconds: Double = 60

    /// Hard backstop on consecutive auto-continuations with no user input in
    /// between. The mode is meant to run unattended for a long time, but an
    /// unbounded loop can burn a large amount of credit overnight if the model
    /// gets stuck in a cycle — so this exists as a visible, countable stop
    /// rather than a silent one. Reset whenever the user sends anything.
    static let autoContinueMaxConsecutive = 500

    /// UserDefaults key for a session's config. New (unsaved) drafts use
    /// `autoContinueDraftKey` and migrate onto the real id on first save.
    static func autoContinueDefaultsKey(for sessionId: String?) -> String {
        "autoContinue.\(sessionId ?? autoContinueDraftKey)"
    }
    static let autoContinueDraftKey = "__draft__"

    // MARK: Derived UI state

    var autoContinueEnabled: Bool { autoContinueConfig.enabled }
    var autoContinuePrompt: String { autoContinueConfig.prompt }
    var autoContinueCountdown: Int { autoContinueCountdownStorage }
    var autoContinueArmed: Bool { autoContinueCountdownTask != nil }

    /// True once the backstop cap has been reached and the mode has parked.
    var autoContinueHitCap: Bool { autoContinueFiredCount >= Self.autoContinueMaxConsecutive }

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

    /// Write the current config back. Also drops the draft-scoped copy once a
    /// real session id exists, so a mode enabled before the first message
    /// carries over instead of being stranded under the draft key.
    func persistAutoContinueConfig() {
        let key = Self.autoContinueDefaultsKey(for: sessionId)
        if let data = try? JSONEncoder().encode(autoContinueConfig) {
            UserDefaults.standard.set(data, forKey: key)
        }
        if let sid = sessionId, sid != Self.autoContinueDraftKey {
            UserDefaults.standard.removeObject(forKey: Self.autoContinueDefaultsKey(for: nil))
        }
    }

    // MARK: User actions

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
        if cfg.enabled && !wasEnabled { autoContinueFiredCount = 0 }   // fresh goal
        persistAutoContinueConfig()

        if cfg.enabled {
            acLogger.info("[AutoContinue] enabled sid=\(self.sessionId?.prefix(8) ?? "draft") promptLen=\(cfg.prompt.count)")
            // If the session is ALREADY idle when the user flips the switch,
            // arm right away — otherwise they would have to poke the dead
            // session once before the mode took effect, which is the opposite
            // of the feature's purpose.
            if !isProcessing { armAutoContinue() }
        } else {
            acLogger.info("[AutoContinue] disabled sid=\(self.sessionId?.prefix(8) ?? "draft")")
            disarmAutoContinue(reason: "user disabled")
        }
    }

    /// Called from the `canResume` didSet. Records that the session paused.
    /// Does NOT arm here — every canResume setter runs while the loop is still
    /// in flight, so the arming happens on the `isProcessing` falling edge.
    func autoContinueOnCanResumeChanged(_ nowCanResume: Bool) {
        guard autoContinueConfig.enabled else { return }
        if !nowCanResume {
            disarmAutoContinue(reason: "canResume cleared")
        }
        // canResume == true intentionally does nothing here; see
        // autoContinueOnProcessingChanged.
    }

    /// Called from the `isProcessing` didSet on the falling edge — the moment
    /// the session actually goes idle. This is where the mode arms, and it
    /// covers BOTH stop shapes (interrupted and finished-normally).
    func autoContinueOnProcessingChanged(_ processing: Bool) {
        guard !processing else { return }
        guard autoContinueConfig.enabled else { return }
        guard canSendAutoContinueNow else { return }
        armAutoContinue()
    }

    /// Shared admission check for arming and firing.
    private var canSendAutoContinueNow: Bool {
        guard autoContinueConfig.enabled else { return false }
        guard !isProcessing else { return false }
        guard !userDidCancel else { return false }
        guard !autoContinueHitCap else { return false }
        // Nothing to continue from: a brand new empty session.
        guard !messages.isEmpty else { return false }
        return true
    }

    // MARK: Countdown

    /// Start the countdown that leads to the next automatic continuation.
    /// Tolerates being called while the loop is still unwinding: rather than
    /// bailing out (which is what made the first version a no-op), it waits
    /// for `isProcessing` to clear, then proceeds.
    func armAutoContinue() {
        guard autoContinueConfig.enabled else { return }
        guard autoContinueCountdownTask == nil else { return }  // already armed
        guard canSendAutoContinueNow else { return }

        let total = Self.autoContinueDelaySeconds
        autoContinueCountdownStorage = total

        acLogger.info("[AutoContinue] countdown=\(total)s sid=\(self.sessionId?.prefix(8) ?? "draft") canResume=\(self.canResume)")

        autoContinueCountdownTask = Task { @MainActor [weak self] in
            // Phase 0 — wait for the in-flight loop to unwind. canResume=true
            // fires while isProcessing is still true, so this is a normal path.
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
                if Task.isCancelled { return }
                self.autoContinueCountdownStorage = remaining
                do {
                    try await Task.sleep(nanoseconds: 1_000_000_000)
                } catch {
                    return  // cancelled
                }
            }
            if Task.isCancelled { return }
            self.autoContinueCountdownStorage = 0
            self.autoContinueCountdownTask = nil
            self.fireAutoContinue()
        }
    }

    /// Cancel the pending countdown. Safe to call when nothing is armed.
    func disarmAutoContinue(reason: String) {
        guard autoContinueCountdownTask != nil || autoContinueCountdownStorage != 0 else { return }
        acLogger.info("[AutoContinue] disarm (\(reason)) sid=\(self.sessionId?.prefix(8) ?? "draft")")
        autoContinueCountdownTask?.cancel()
        autoContinueCountdownTask = nil
        autoContinueCountdownStorage = 0
    }

    /// Send the continuation, picking the right path for the stop shape.
    ///
    ///  * INTERRUPTED (`canResume`) → `resume(continuationText:)`, which
    ///    repairs the partial tail and re-enters the loop exactly like a manual
    ///    Resume. Reusing it keeps history repair and badge handling identical.
    ///  * FINISHED NORMALLY → a plain new turn via the ordinary `send()` path:
    ///    `agentHistory` already ends on a complete assistant reply, which is
    ///    precisely the shape a fresh user message expects. Inventing a
    ///    resume here would inject a "you were interrupted" system-reminder
    ///    that is simply untrue.
    private func fireAutoContinue() {
        guard canSendAutoContinueNow else {
            acLogger.info("[AutoContinue] fire skipped — enabled=\(self.autoContinueConfig.enabled) processing=\(self.isProcessing) cancelled=\(self.userDidCancel) cap=\(self.autoContinueHitCap)")
            return
        }

        autoContinueFiredCount += 1
        let text = autoContinueConfig.prompt
        acLogger.info("[AutoContinue] FIRING #\(self.autoContinueFiredCount) path=\(self.canResume ? "resume" : "new-turn")")

        // Mark the send as mode-initiated so the user-input reset hook doesn't
        // clear the counter we just incremented.
        isAutoContinueSending = true
        defer { isAutoContinueSending = false }

        if canResume {
            resume(continuationText: text)
        } else {
            inputText = text
            send()
        }
    }

    /// Disarm the countdown when the user taps Stop.
    ///
    /// Does NOT clear `enabled`: Stop means "not right now", and leaving the
    /// mode armed is what lets the next stop pick up again. Turning the mode
    /// off is its own explicit action (the toolbar toggle).
    func autoContinueHandleUserStop() {
        disarmAutoContinue(reason: "user tapped Stop")
    }

    /// Called when the user sends their own message — a new goal, so the
    /// consecutive counter restarts.
    ///
    /// Skipped for the mode's OWN sends: `fireAutoContinue` goes through
    /// `send()`, which calls this, so counting those as user input would reset
    /// the cap on every iteration and make the backstop unreachable.
    func autoContinueNoteUserInput() {
        guard !isAutoContinueSending else { return }
        if autoContinueFiredCount != 0 {
            acLogger.info("[AutoContinue] user input — reset fired count (was \(self.autoContinueFiredCount))")
            autoContinueFiredCount = 0
        }
        disarmAutoContinue(reason: "user sent a message")
    }
}

// MARK: - Settings sheet

/// Editor for the per-session "Keep Going" mode.
///
/// Deliberately small: one switch, one message field, and a plain-language
/// note. The failure mode of a mis-set prompt here is an unattended loop, so
/// the sheet shows how many times it has fired rather than hiding that.
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
                    Text(AppLocalized("Every time the agent stops — whether it finished a step or was interrupted — Minis sends the message below and keeps working. Turn this off, or tap Stop, to cancel."))
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
                        LabeledContent(AppLocalized("Auto-continued so far"),
                                       value: "\(vm.autoContinueFiredCount) / \(AIChatViewModel.autoContinueMaxConsecutive)")
                        if vm.autoContinueArmed {
                            LabeledContent(AppLocalized("Next continuation in"), value: "\(vm.autoContinueCountdown)s")
                        }
                        if vm.autoContinueHitCap {
                            Text(AppLocalized("Backstop reached — the mode parked itself. Send a message to start a fresh run."))
                                .font(.footnote)
                                .foregroundStyle(.orange)
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

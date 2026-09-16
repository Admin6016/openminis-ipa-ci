#!/usr/bin/env python3
r"""
A + A1: make parallel sub-agents VISIBLE and NON-BLOCKING.

## Why

`SubAgentRunner` drove its stream with `headless: true`, and
`processStreamEvents`/`executeSingleToolUse` turned that into
`let msgIdx = headless ? Int.max : rawMsgIdx`. Every write is guarded by
`msgIdx < messages.count`, so a sub-agent's tool calls and text were written
nowhere and never rendered — the user saw a blank run. (`headless` was also
meant to suppress TTS, a separate concern that got fused into the same flag.)

Separately, `spawn_agents` awaited the whole task group before returning a
single aggregated tool_result, so the orchestrator's loop stalled for the
entire fan-out.

## What this does

**(A) Visible** — each sub-agent gets its own `ChatMessage` in `messages` and
writes into it through a real index. `messages` is the UI array; `agentHistory`
is what the provider sees. They are separate arrays, so these turns render as
ordinary tool capsules without ever leaking into the orchestrator's context.
`headless` is retained for what it should only ever have meant: no TTS, no
session-status writes, no viewport scrolling.

**(A1) Non-blocking** — `spawn_agents` returns an immediate ack; the fan-out
runs in a detached task; on completion the aggregated summary is appended to
`agentHistory` (and persisted) and the orchestrator's loop is restarted if idle.

Usage: python3 fix_subagent_async.py <repo-root>
"""
import sys
from pathlib import Path

root = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
CH = root / "src" / "ios" / "Agent" / "Chat"
MODELS = CH / "ChatModels.swift"
RUNNER = CH / "SubAgentRunner.swift"
SSE = CH / "AIChatViewModel+SSEStream.swift"
CT = CH / "AIChatViewModel+ConcurrentTools.swift"
VM = CH / "AIChatViewModel.swift"

files = (MODELS, RUNNER, SSE, CT, VM)
for p in files:
    if not p.exists():
        sys.exit(f"FATAL: missing {p}")

srcs = {p: p.read_text() for p in files}

# --------------------------------------------------------------------------
# GATE
# --------------------------------------------------------------------------
if "spawn_agents" not in srcs[CT] or "headless" not in srcs[SSE] or "headless" not in srcs[RUNNER]:
    print("[GATE   ] parallel sub-agent feature not present — nothing to patch")
    sys.exit(0)

MARKER = "[ci-fix-async]"
if any(MARKER in s for s in srcs.values()):
    print("[ALREADY] A+A1 sub-agent patch already present")
    sys.exit(0)

print("[GATE   ] parallel sub-agent feature detected — applying A + A1")

edits = []


def edit(path, desc, old, new, count=1):
    s = srcs[path]
    n = s.count(old)
    if n != count:
        sys.exit(f"FATAL: {desc}\n  {path.name}: expected {count} occurrence(s), found {n}\n  anchor: {old[:100]!r}")
    srcs[path] = s.replace(old, new, count)
    edits.append(desc)


# ==========================================================================
# 1. ChatMessage: mark a turn as belonging to a sub-agent.
# ==========================================================================
edit(MODELS, "ChatMessage: add sub-agent markers",
r"""    /// Links back to the QueuedPrompt so we can withdraw it.
    var queuedPromptId: UUID?""",
r"""    /// Links back to the QueuedPrompt so we can withdraw it.
    var queuedPromptId: UUID?
    /// [ci-fix-async] True when this turn was produced by a parallel sub-agent.
    /// Sub-agent turns are ordinary renderable messages — that is the whole
    /// point, the user watches the work happen — but they live only in
    /// `messages` (the UI array). They are never appended to `agentHistory`,
    /// which is the array the provider actually sees, so the orchestrator's
    /// context stays clean.
    var isSubAgentRun: Bool = false
    /// [ci-fix-async] Name of the sub-agent that owns this turn, for labelling.
    var subAgentName: String? = nil""")


# ==========================================================================
# 2. SubAgentRunner: carry a real message index and write into it.
# ==========================================================================
edit(RUNNER, "SubAgentRunner: add uiMsgIdx property",
r"""    private let entry: SubAgentRunStore.Entry?""",
r"""    private let entry: SubAgentRunStore.Entry?
    /// [ci-fix-async] Index into `AIChatViewModel.messages` for this agent's own
    /// visible turn. Previously the runner passed `headless: true`, which forced
    /// `msgIdx = Int.max` inside the shared executors so every guarded write was
    /// skipped — the agent's tool calls were invisible and its results dropped.
    /// Writing to a dedicated message keeps concurrent agents isolated while
    /// making the work observable.
    let uiMsgIdx: Int""")

edit(RUNNER, "SubAgentRunner: init parameter",
r"""        thinkingLevel: ThinkingLevel,
        entry: SubAgentRunStore.Entry?
    ) {""",
r"""        thinkingLevel: ThinkingLevel,
        entry: SubAgentRunStore.Entry?,
        uiMsgIdx: Int
    ) {""")

edit(RUNNER, "SubAgentRunner: assign uiMsgIdx",
r"""        self.entry = entry
    }""",
r"""        self.entry = entry
        self.uiMsgIdx = uiMsgIdx
    }""")

edit(RUNNER, "SubAgentRunner: stream into its own message",
r"""                    msgIdx: 0,
                    provider: provider,
                    headless: true
                )""",
r"""                    msgIdx: uiMsgIdx,
                    provider: provider,
                    headless: true
                )""")

edit(RUNNER, "SubAgentRunner: run tools against its own message",
r"""                                msgIdx: 0,
                                tools: toolsSnapshot,""",
r"""                                msgIdx: self.uiMsgIdx,
                                tools: toolsSnapshot,""")


# ==========================================================================
# 3. processStreamEvents: stop hijacking msgIdx.
# ==========================================================================
edit(SSE, "processStreamEvents: respect the real msgIdx",
r"""        let msgIdx = headless ? Int.max : rawMsgIdx""",
r"""        // [ci-fix-async] Was `headless ? Int.max : rawMsgIdx`. Pushing the index
        // out of range suppressed every guarded write, which silently discarded a
        // sub-agent's tool calls and text. `headless` now only means "this is not
        // the orchestrator's turn" — no TTS, no status-bar writes, no scrolling.
        let msgIdx = rawMsgIdx""")

edit(SSE, "processStreamEvents: keep status-bar writes off sub-agent turns",
r"""                            if let sid = self.sessionId {
                                SessionActivityTracker.shared.updateToolInfo(sessionId: sid, toolName: "text", toolStatus: "streaming")
                            }""",
r"""                            // [ci-fix-async] The session's status bar belongs to the
                            // orchestrator; a sub-agent must not drive it.
                            if !headless, let sid = self.sessionId {
                                SessionActivityTracker.shared.updateToolInfo(sessionId: sid, toolName: "text", toolStatus: "streaming")
                            }""")

edit(SSE, "processStreamEvents: don't scroll the viewport for sub-agent turns",
r"""                        // [T-ios-stream-publish-transition-gap]
                        publishUnlessTransitioning()
                        scrollToBottomSignal.send()""",
r"""                        // [T-ios-stream-publish-transition-gap]
                        // [ci-fix-async] Sub-agent turns publish (so their card
                        // updates) but must never scroll the user's viewport.
                        publishUnlessTransitioning()
                        if !headless { scrollToBottomSignal.send() }""")


# ==========================================================================
# 4. executeSingleToolUse: same treatment.
# ==========================================================================
edit(CT, "executeSingleToolUse: respect the real msgIdx",
r"""        let msgIdx = headless ? Int.max : rawMsgIdx""",
r"""        // [ci-fix-async] See processStreamEvents — the index is real now, so a
        // sub-agent's tool results land in its own message instead of nowhere.
        let msgIdx = rawMsgIdx""")


# ==========================================================================
# 5. spawn_agents: visible cards, async dispatch, immediate ack.
# ==========================================================================
START = "            var runners: [SubAgentRunner] = []\n"
END = '            saLogger.info("[SubAgent] spawn_agents done: \\(succeeded)/\\(finished.count) succeeded")\n'

ct = srcs[CT]
i0 = ct.find(START)
i1 = ct.find(END)
if i0 < 0 or i1 < 0:
    sys.exit("FATAL: could not locate the spawn_agents fan-out region")
i1 += len(END)

NEW_BLOCK = r'''            // [ci-fix-async] Give every sub-agent its own visible turn. These
            // live in `messages` (UI) only — `agentHistory` (provider context) is
            // a separate array and is not touched here, so concurrent agents are
            // both isolated from each other and invisible to the orchestrator's
            // next request.
            var uiIndices: [Int] = []
            for t in tasks {
                let card = ChatMessage(role: .assistant, content: "", blocks: [])
                card.isSubAgentRun = true
                card.subAgentName = t.name
                messages.append(card)
                uiIndices.append(messages.count - 1)
            }

            var runners: [SubAgentRunner] = []
            for (idx, t) in tasks.enumerated() {
                let entry = idx < run.entries.count ? run.entries[idx] : nil
                runners.append(SubAgentRunner(
                    name: t.name, task: t.task,
                    vm: self, provider: subProvider, tools: subTools,
                    systemPrompt: subSystemPrompt,
                    thinkingLevel: subThinking,
                    entry: entry,
                    uiMsgIdx: uiIndices[idx]
                ))
            }

            // [ci-fix-async] Deliberately detached: the orchestrator gets an
            // immediate ack and stays responsive while the fan-out runs. The
            // aggregated summary is injected into the orchestrator's history when
            // the run completes, which restarts its loop if it has gone idle.
            //
            // The detached task captures immutable snapshots only — a mutable
            // capture referenced from a concurrent closure does not compile.
            let runId = run.id
            let agentCount = tasks.count
            let runnersForTask = runners
            let concurrencyForTask = maxConcurrency
            Task { @MainActor [weak self] in
                guard let self else { return }
                var results = [SubAgentResult?](repeating: nil, count: agentCount)
                await withTaskGroup(of: (Int, SubAgentResult).self) { group in
                    var added = 0
                    var harvested = 0
                    for (idx, runner) in runnersForTask.enumerated() {
                        while added - harvested >= concurrencyForTask {
                            if let pair = await group.next() {
                                results[pair.0] = pair.1
                                harvested += 1
                            } else {
                                break
                            }
                        }
                        group.addTask {
                            await SubAgentBudget.shared.acquire()
                            let r = await runner.run()
                            await SubAgentBudget.shared.release()
                            return (idx, r)
                        }
                        added += 1
                    }
                    for await pair in group {
                        results[pair.0] = pair.1
                        harvested += 1
                    }
                }
                SubAgentRunStore.shared.finishRun(runId)
                await self.completeSubAgentRun(results.compactMap { $0 })
            }

            toolOutput = "Started \(tasks.count) sub-agent\(tasks.count == 1 ? "" : "s") in the background. They are running in parallel and you will receive their combined results in a follow-up message when they finish. Do not wait for them and do not call spawn_agents again for the same work — reply to the user now, or continue with anything else you can do in the meantime."
            toolSuccess = true
            if msgIdx < messages.count, blockIdx < messages[msgIdx].blocks.count {
                messages[msgIdx].blocks[blockIdx].content = toolOutput
            }
            saLogger.info("[SubAgent] spawn_agents: dispatched \(tasks.count) agent(s) asynchronously")
'''

srcs[CT] = ct[:i0] + NEW_BLOCK + ct[i1:]
edits.append("spawn_agents: per-agent visible cards + async dispatch + immediate ack")


# ==========================================================================
# 6. runAgentLoop is file-private; the completion handler restarts it from
#    another file, so the declaration must be at least internal.
# ==========================================================================
edit(VM, "runAgentLoop: widen from file-private to internal",
r"""    private func runAgentLoop(resumingAt existingMsgIdx: Int? = nil, committedBlocks: Int? = nil) async throws {""",
r"""    // [ci-fix-async] Was `private`. The sub-agent completion handler in
    // AIChatViewModel+ConcurrentTools.swift restarts the loop after a fan-out
    // finishes, and Swift `private` is file-scoped, so it must be at least
    // internal. Nothing is exposed outside the module.
    func runAgentLoop(resumingAt existingMsgIdx: Int? = nil, committedBlocks: Int? = nil) async throws {""")


# ==========================================================================
# 7. The completion handler, appended as its own extension.
# ==========================================================================
HELPER = r'''

// MARK: - [ci-fix-async] Sub-agent completion

extension AIChatViewModel {

    /// Land a finished fan-out into the orchestrator's context.
    ///
    /// Called from the detached task spawned by `spawn_agents`. Appends the
    /// aggregated summary as a user turn, persists it so it survives a reload,
    /// and restarts the agent loop when the VM has gone idle. If a turn is
    /// already in flight, that loop picks the new history up on its next
    /// iteration and the restart is skipped.
    func completeSubAgentRun(_ finished: [SubAgentResult]) async {
        guard !finished.isEmpty else { return }
        let succeeded = finished.filter { $0.state == .succeeded }.count

        var body = "Ran \(finished.count) sub-agent\(finished.count == 1 ? "" : "s") in parallel — \(succeeded) succeeded.\n\n"
        body += finished.enumerated()
            .map { $0.element.renderForModel(index: $0.offset) }
            .joined(separator: "\n\n")
        if finished.contains(where: { $0.state == .cancelled }) {
            body += "\n\n<system-reminder>At least one sub-agent was cancelled, so its result is incomplete.</system-reminder>"
        }

        saLogger.info("[SubAgent] fan-out complete: \(succeeded)/\(finished.count) succeeded")

        let msg = AgentMessage(role: .user, parts: [.text(body)])
        let idx = agentHistory.count
        agentHistory.append(msg)
        if let pid = await persistAgentMessage(msg), idx < agentHistory.count {
            agentHistory[idx].dbMessageId = pid
        }

        guard !isProcessing else {
            saLogger.info("[SubAgent] a turn is already running — summary appended to history")
            return
        }
        do {
            try await runAgentLoop()
        } catch {
            ctLogger.error("[SubAgent] restart after fan-out failed: \(error.localizedDescription)")
        }
    }
}
'''

srcs[CT] = srcs[CT].rstrip("\n") + HELPER
edits.append("completeSubAgentRun: inject summary + persist + restart loop if idle")


# ==========================================================================
# Apply
# ==========================================================================
for p, s in srcs.items():
    p.write_text(s)

print(f"[APPLY  ] {len(edits)} edit(s):")
for d in edits:
    print(f"          - {d}")
print("[fix] A+A1 applied: sub-agent turns render live; spawn_agents is non-blocking.")

#!/usr/bin/env python3
r"""
Aggregate the sub-agent fan-out into ONE in-chat panel with real controls.

## Problem

`fix_subagent_async.py` gave every sub-agent its own `ChatMessage`, so a 5-way
fan-out produced five separate assistant bubbles, each with its own tool
capsules. Two real defects, not just aesthetics:

1. **Unreadable.** Five interleaved half-conversations in one transcript.
2. **No controls.** Nothing to cancel a run, nothing to see it as a unit.

## Design

A fan-out is ONE event, so it gets ONE card.

* **Container messages stay, but are hidden.** Each agent still writes into its
  own `ChatMessage` — that is what keeps concurrent agents race-free (they
  never touch a shared `blocks` array) and preserves each agent's tool-capsule
  data. These are tagged and filtered out of the list.
* **One panel card per run.** A single `ChatMessage` carries `subAgentRunId`
  and renders `SubAgentPanelView`, which subscribes to `SubAgentRunStore` —
  which was already publishing `state` / `currentTool` / `toolCallCount` /
  `finalText` / `doneCount` / `summaryLine` per agent with no reader at all.
* Neither kind enters `agentHistory`, so the provider's context is untouched.

## Controls

Cancel a single agent, or cancel the whole run — both write through to the
detached task, which the store now holds.

Usage: python3 fix_subagent_panel.py <repo-root>
"""
import sys
from pathlib import Path

root = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
CH = root / "src" / "ios" / "Agent" / "Chat"
MODELS = CH / "ChatModels.swift"
CT = CH / "AIChatViewModel+ConcurrentTools.swift"
STORE = CH / "SubAgentModels.swift"
LIST = root / "src" / "ios" / "Agent" / "MessageList" / "CollectionViewMessageListV3.swift"

for p in (MODELS, CT, STORE, LIST):
    if not p.exists():
        sys.exit(f"FATAL: missing {p}")

MARKER = "[ci-fix-panel]"
if any(MARKER in p.read_text() for p in (MODELS, CT, STORE, LIST)):
    print("[ALREADY] sub-agent panel patch already present")
    sys.exit(0)

if "spawn_agents" not in CT.read_text():
    print("[GATE   ] sub-agent feature not present — nothing to patch")
    sys.exit(0)

print("[GATE   ] sub-agent feature detected — applying panel patch")

edits = []


def edit(path, desc, old, new, count=1):
    s = path.read_text()
    n = s.count(old)
    if n != count:
        sys.exit(f"FATAL: {desc}\n  {path.name}: expected {count}, found {n}\n  anchor: {old[:110]!r}")
    path.write_text(s.replace(old, new, count))
    edits.append(desc)


# ==========================================================================
# 1. ChatMessage: distinguish the panel card from the hidden agent containers.
# ==========================================================================
edit(MODELS, "ChatMessage: add panel vs container markers",
     r"""    /// [ci-fix-async] Name of the sub-agent that owns this turn, for labelling.
    var subAgentName: String? = nil""",
     r"""    /// [ci-fix-async] Name of the sub-agent that owns this turn, for labelling.
    var subAgentName: String? = nil
    /// [ci-fix-panel] Set on the ONE card that represents a whole fan-out. The
    /// row renders `SubAgentPanelView` for it, driven by `SubAgentRunStore`.
    var subAgentRunId: UUID? = nil
    /// [ci-fix-panel] Set on the per-agent CONTAINER messages. Agents write
    /// their tool capsules into these (which is what keeps concurrent agents
    /// race-free — they never share a `blocks` array), but the list hides them:
    /// the panel is the user-facing surface. Never rendered, never sent to the
    /// provider.
    var isSubAgentContainer: Bool = false""")

# ==========================================================================
# 2. Store: hold the run task so the panel's Cancel button has a target.
# ==========================================================================
edit(STORE, "SubAgentRunStore: cancellable run tasks",
     r"""    @Published private(set) var runs: [UUID: Run] = [:]

    private init() {}""",
     r"""    @Published private(set) var runs: [UUID: Run] = [:]

    /// [ci-fix-panel] The detached task driving each fan-out, so the panel can
    /// cancel it. Cleared when the run finishes.
    private var runTasks: [UUID: Task<Void, Never>] = [:]

    func attachTask(_ task: Task<Void, Never>, to runId: UUID) {
        runTasks[runId] = task
    }

    /// Cancel a whole fan-out. Runners check `Task.isCancelled` between turns,
    /// and the surrounding task cancellation tears down in-flight tool calls.
    func cancelRun(_ runId: UUID) {
        guard let task = runTasks[runId] else { return }
        saLogger.info("[SubAgent] cancelling run \(runId.uuidString.prefix(8))")
        task.cancel()
    }

    func cancelEntry(_ runId: UUID, entryId: UUID) {
        guard let run = runs[runId],
              let entry = run.entries.first(where: { $0.id == entryId }) else { return }
        saLogger.info("[SubAgent] cancelling agent '\(entry.name)'")
        entry.state = .cancelled
    }

    /// True while the run still has a live task.
    func isRunActive(_ runId: UUID) -> Bool {
        runTasks[runId]?.isCancelled == false
    }

    private init() {}""")

edit(STORE, "SubAgentRunStore: release the task when the run ends",
     r"""    func finishRun(_ runId: UUID) {
        runs[runId]?.isFinished = true
    }""",
     r"""    func finishRun(_ runId: UUID) {
        runs[runId]?.isFinished = true
        // [ci-fix-panel] Nothing left to cancel once the run is done.
        runTasks[runId] = nil
    }""")

# ==========================================================================
# 3. spawn_agents: emit ONE panel card, keep the per-agent containers hidden.
# ==========================================================================
edit(CT, "spawn_agents: one panel card + hidden per-agent containers",
     r"""            // [ci-fix-async] Give every sub-agent its own visible turn. These
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
""",
     r"""            // [ci-fix-panel] ONE panel card for the whole fan-out, plus one
            // HIDDEN container per agent.
            //
            // Why the containers still exist: each agent writes its tool calls
            // into `messages[idx].blocks`, and giving every agent its own array
            // is what makes concurrent agents race-free. They are flagged
            // `isSubAgentContainer` and filtered out of the list, so the user
            // sees the single panel rather than N interleaved bubbles.
            //
            // Both live only in `messages` (what the list reads) and never in
            // `agentHistory` (what the provider reads), so the orchestrator's
            // context is unaffected.
            let panelCard = ChatMessage(role: .assistant, content: "", blocks: [])
            panelCard.isSubAgentRun = true
            panelCard.subAgentRunId = run.id
            messages.append(panelCard)

            var uiIndices: [Int] = []
            for t in tasks {
                let container = ChatMessage(role: .assistant, content: "", blocks: [])
                container.isSubAgentRun = true
                container.isSubAgentContainer = true
                container.subAgentName = t.name
                container.subAgentRunId = run.id
                messages.append(container)
                uiIndices.append(messages.count - 1)
            }
""")

edit(CT, "spawn_agents: keep the run task so the panel can cancel it",
     r"""            let runnersForTask = runners
            let concurrencyForTask = maxConcurrency
            Task { @MainActor [weak self] in""",
     r"""            let runnersForTask = runners
            let concurrencyForTask = maxConcurrency
            let runTask = Task { @MainActor [weak self] in""")

edit(CT, "spawn_agents: register the task with the store",
     r"""                SubAgentRunStore.shared.finishRun(runId)
                await self.completeSubAgentRun(results.compactMap { $0 })
            }""",
     r"""                SubAgentRunStore.shared.finishRun(runId)
                await self.completeSubAgentRun(results.compactMap { $0 })
            }
            // [ci-fix-panel] Give the store the task so the panel's Cancel
            // button has something to cancel.
            SubAgentRunStore.shared.attachTask(runTask, to: runId)""")

# ==========================================================================
# 4. The list hides the agent containers (next to the existing bridge filter).
# ==========================================================================
edit(LIST, "list: hide sub-agent container messages",
     r"""            let messages = rawMessages.contains(where: { $0.isInternalBridge })
                ? rawMessages.filter { !$0.isInternalBridge }
                : rawMessages""",
     r"""            // [ci-fix-panel] Hide the per-agent container messages. They hold
            // each agent's tool capsules and exist so concurrent agents never
            // share a `blocks` array, but the user-facing surface is the single
            // aggregate panel card, not N interleaved bubbles.
            let messages = rawMessages.contains(where: { $0.isInternalBridge || $0.isSubAgentContainer })
                ? rawMessages.filter { !$0.isInternalBridge && !$0.isSubAgentContainer }
                : rawMessages""")

for d in edits:
    print(f"[APPLY  ] {d}")

# ==========================================================================
# 5. WIRE THE PANEL INTO RENDERING.
#
# The chat list does NOT render assistant messages through ChatMessageRow: it
# splits every assistant turn into `assistantHeader` + one `assistantBlock` per
# block + `assistantFooter`. ChatMessageRow is only used for `wholeMessage`
# items (user / compactDivider / systemInfo). Patching ChatMessageRow would
# therefore have produced a panel that never appears.
#
# The correct hook is the item-build switch: a card carrying `subAgentRunId`
# becomes ONE item rendered by the panel cell, instead of header+blocks+footer.
# ==========================================================================
edit(LIST, "list: render a fan-out card as a single panel item",
     r"""                case .assistant:
                    newItems.append(.assistantHeader(message.id))
                    for block in message.blocks {
                        newItems.append(.assistantBlock(message.id, block.id))
                    }""",
     r"""                case .assistant:
                    // [ci-fix-panel] A fan-out card is ONE aggregate panel, not
                    // a header + N tool blocks. Emitting a single item keeps the
                    // transcript readable (one card per fan-out) and avoids
                    // duplicating the per-agent tool capsules that already live
                    // in the hidden container messages.
                    if message.subAgentRunId != nil {
                        newItems.append(.assistantPanel(message.id))
                        break
                    }
                    newItems.append(.assistantHeader(message.id))
                    for block in message.blocks {
                        newItems.append(.assistantBlock(message.id, block.id))
                    }""")

# ==========================================================================
# 6. Supporting declarations the new item needs: the enum case, the cell class,
#    its registration, and its configureCell branch.
# ==========================================================================
INFRA = root / "src" / "ios" / "Agent" / "MessageList" / "MessageListInfrastructure.swift"
edit(INFRA, "MessageListItem: add the panel case",
     r"""    /// Footer area: typing indicator, error, resume, usage.
    case assistantFooter(UUID)""",
     r"""    /// Footer area: typing indicator, error, resume, usage.
    case assistantFooter(UUID)
    /// [ci-fix-panel] One whole sub-agent fan-out, rendered as a single
    /// aggregate panel instead of header + N tool blocks.
    case assistantPanel(UUID)""")

edit(INFRA, "MessageListItem.messageId: cover the panel case",
     r"""        case .wholeMessage(let id), .assistantHeader(let id),
             .assistantFooter(let id): return id""",
     r"""        case .wholeMessage(let id), .assistantHeader(let id),
             .assistantFooter(let id), .assistantPanel(let id): return id""")

LIST = root / "src" / "ios" / "Agent" / "MessageList" / "CollectionViewMessageListV3.swift"

edit(LIST, "add the panel cell class",
     r"""private final class AssistantFooterCellV3: SelfSizingCell {}""",
     r"""private final class AssistantFooterCellV3: SelfSizingCell {}

/// [ci-fix-panel] Hosts the aggregate card for one sub-agent fan-out.
private final class AssistantPanelCellV3: SelfSizingCell {}""")

edit(LIST, "register the panel cell",
     r"""            let footerReg = UICollectionView.CellRegistration<AssistantFooterCellV3, MessageListItem> {
                [weak self] cell, indexPath, item in
                self?.configureCell(cell, item: item, indexPath: indexPath)
            }""",
     r"""            let footerReg = UICollectionView.CellRegistration<AssistantFooterCellV3, MessageListItem> {
                [weak self] cell, indexPath, item in
                self?.configureCell(cell, item: item, indexPath: indexPath)
            }
            // [ci-fix-panel]
            let panelReg = UICollectionView.CellRegistration<AssistantPanelCellV3, MessageListItem> {
                [weak self] cell, indexPath, item in
                self?.configureCell(cell, item: item, indexPath: indexPath)
            }""")

edit(LIST, "dispatch the panel cell",
     r"""                case .assistantFooter:
                    return cv.dequeueConfiguredReusableCell(using: footerReg, for: indexPath, item: item)""",
     r"""                case .assistantFooter:
                    return cv.dequeueConfiguredReusableCell(using: footerReg, for: indexPath, item: item)
                case .assistantPanel:
                    return cv.dequeueConfiguredReusableCell(using: panelReg, for: indexPath, item: item)""")

# --- every other exhaustive switch over MessageListItem -------------------
edit(LIST, "messageId(of:): cover the panel case",
     r"""            case .assistantHeader(let id): return id
            case .assistantFooter(let id): return id
            case .assistantBlock(let mid, _): return mid
            }
        }""",
     r"""            case .assistantHeader(let id): return id
            case .assistantFooter(let id): return id
            case .assistantPanel(let id): return id
            case .assistantBlock(let mid, _): return mid
            }
        }""")

edit(LIST, "contentKey (DEBUG): cover the panel case",
     "            case .assistantFooter(let id):\n"
     "                let c = msg(id)?.blocks.first?.content ?? \"\"\n"
     "                return \"f#\\(digest(c))\"",
     "            case .assistantFooter(let id):\n"
     "                let c = msg(id)?.blocks.first?.content ?? \"\"\n"
     "                return \"f#\\(digest(c))\"\n"
     "            case .assistantPanel(let id):\n"
     "                // A fan-out card grows as agents report progress, so fold\n"
     "                // the completion count in to force a re-measure as it moves.\n"
     "                let done = msg(id)?.subAgentRunId\n"
     "                    .flatMap { SubAgentRunStore.shared.run(for: $0)?.doneCount } ?? 0\n"
     "                return \"p#\" + digest(\"\\(done)\")\n")

edit(LIST, "contentKey: cover the panel case",
     "            case .assistantFooter(let id):\n"
     "                return \"f:\\(id.uuidString)\"",
     "            case .assistantFooter(let id):\n"
     "                return \"f:\\(id.uuidString)\"\n"
     "            case .assistantPanel(let id):\n"
     "                // Include the progress counter so the memo invalidates as the\n"
     "                // fan-out advances instead of freezing at the first height.\n"
     "                let done = msg(id)?.subAgentRunId\n"
     "                    .flatMap { SubAgentRunStore.shared.run(for: $0)?.doneCount } ?? 0\n"
     "                return \"p:\" + id.uuidString + \":\" + String(done)\n")

edit(LIST, "belongsTo: cover the panel case",
     r"""            case .assistantBlock(let mid, _): return mid == messageId
            case .assistantFooter(let mid): return mid == messageId
            case .wholeMessage, .assistantHeader: return false""",
     r"""            case .assistantBlock(let mid, _): return mid == messageId
            case .assistantFooter(let mid): return mid == messageId
            case .assistantPanel(let mid): return mid == messageId
            case .wholeMessage, .assistantHeader: return false""")

edit(LIST, "seed-loop estimated height: cover the panel case",
     r"""                    case .assistantFooter:
                        // Prominent banners (error/resume/typing) measure
                        // ~44-56pt; the quiet meta footer is ~0-4pt. Seeding
                        // closer to the real height keeps the first-display
                        // correction (and its scroll shift) small.""",
     r"""                    case .assistantPanel:
                        // [ci-fix-panel] Seed only; the real measure wins once
                        // the card mounts. Header (~40pt) + one row per agent.
                        let agentCount = messages
                            .compactMap { $0.subAgentRunId }
                            .compactMap { SubAgentRunStore.shared.run(for: $0) }
                            .map(\.entries.count)
                            .max() ?? 1
                        layout.setEstimatedHeight(CGFloat(40 + 44 * max(1, agentCount)), at: i)

                    case .assistantFooter:
                        // Prominent banners (error/resume/typing) measure
                        // ~44-56pt; the quiet meta footer is ~0-4pt. Seeding
                        // closer to the real height keeps the first-display
                        // correction (and its scroll shift) small.""")

edit(LIST, "estimateItemHeight: cover the new case (switch is exhaustive)",
     r"""            case .assistantFooter:
                return 4
            }
        }""",
     r"""            case .assistantFooter:
                return 4
            case .assistantPanel:
                // Seed for the first layout pass only; the real measure wins
                // once the cell mounts. Header (~40pt) + one row per agent
                // (~44pt), collapsed to the header when finished.
                let n = messages
                    .compactMap { $0.subAgentRunId }
                    .compactMap { SubAgentRunStore.shared.run(for: $0) }
                    .map(\.entries.count)
                    .max() ?? 1
                return CGFloat(40 + 44 * max(1, n))
            }
        }""")

edit(LIST, "configureCell: build the panel hosting view",
     r"""            case .assistantHeader(let msgId):""",
     r"""            case .assistantPanel(let msgId):
                // [ci-fix-panel] The aggregate card for a fan-out. It reads the
                // store directly (which was already publishing per-agent state
                // with no reader) so it stays live without any new plumbing.
                // The vm is re-injected because UIHostingConfiguration does not
                // inherit EnvironmentObjects — the same trap the tool-capsule
                // crash documented.
                cell.backgroundColor = .clear
                let panelConfig = UIHostingConfiguration {
                    if let panelId = messages.first(where: { $0.id == msgId })?.subAgentRunId,
                       let storeRun = SubAgentRunStore.shared.run(for: panelId) {
                        SubAgentPanelView(
                            run: storeRun,
                            onCancelRun: { SubAgentRunStore.shared.cancelRun(panelId) },
                            onCancelEntry: { entryId in
                                SubAgentRunStore.shared.cancelEntry(panelId, entryId: entryId)
                            }
                        )
                    } else {
                        // The run has been pruned (turn ended). Render nothing
                        // rather than a stale card.
                        Color.clear.frame(height: 0)
                    }
                }.minSize(width: 0, height: 0).margins(.all, 0)
                cell.applyContentConfiguration(panelConfig)

            case .assistantHeader(let msgId):""")

# ==========================================================================
# 7. Persist the panel card so the transcript survives a reload, and prune it
#    when the run is gone.
# ==========================================================================
edit(MODELS, "ChatMessage: exempt panel cards from the provider context",
     r"""    var isSubAgentContainer: Bool = false""",
     r"""    var isSubAgentContainer: Bool = false

    /// [ci-fix-panel] True for either sub-agent card kind. Used by the reload
    /// and compaction paths to keep them out of `agentHistory`: they are UI
    /// surfaces for work the ORCHESTRATOR already received as a tool_result, so
    /// re-sending them would double-report every fan-out.
    var isSubAgentSurface: Bool { isSubAgentRun }""")

print(f"[fix] panel patch applied: {len(edits)} edit(s)")
print("[NOTE   ] the panel view ships as sources/SubAgentPanel.swift")

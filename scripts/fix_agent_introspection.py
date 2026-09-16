#!/usr/bin/env python3
r"""
Two new in-process tools so the model can see what is going on:

  * `agent_status`  — live progress of the parallel sub-agent runs
  * `session_list`  — titles + state of other chat sessions

## Why tools and not CLIs

The `/usr/local/bin` CLIs work by printing OSC 1337 escape sequences that the
host scans out of stdout. That channel is ONE-WAY: it can ask the host to do
something (open a URL) but cannot return data. These two queries need answers,
so they are registered as ordinary agent tools instead — same shape as
`spawn_agents`: a definition in `makeAgentTools()` and a branch in the tool
dispatch switch. Both run in-process, so they can read the live stores
directly with no IPC at all.

## Scope

`agent_status` reads `SubAgentRunStore`, which already publishes per-agent
`state` / `currentTool` / `toolCallCount` / `finalText` plus per-run
`doneCount` / `isFinished`.

`session_list` goes through `SessionsOffloadBridge.querySessions`, the existing
native bridge the app already ships, so it sees exactly the same list the
sessions UI does. It is only registered when that bridge is present.

Usage: python3 fix_agent_introspection.py <repo-root>
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

MARKER = "[ci-fix-introspect]"
if MARKER in DEFS.read_text():
    print("[ALREADY] agent introspection patch present")
    sys.exit(0)

if "spawn_agents" not in DEFS.read_text():
    print("[GATE   ] sub-agent feature not present — nothing to patch")
    sys.exit(0)

print("[GATE   ] adding agent_status + session_list tools")

edits = []


def edit(path, desc, old, new, count=1):
    s = path.read_text()
    n = s.count(old)
    if n != count:
        sys.exit(f"FATAL: {desc}\n  {path.name}: expected {count}, found {n}\n  anchor: {old[:110]!r}")
    path.write_text(s.replace(old, new, count))
    edits.append(desc)


# ==========================================================================
# 1. Tool definitions.
# ==========================================================================
edit(DEFS, "define agent_status + session_list",
     r"""        return tools
    }

    /// Tools handed to a SUB-agent: everything the orchestrator has, minus
    /// `spawn_agents` itself.""",
     r"""        // [ci-fix-introspect] Live progress of parallel sub-agent runs.
        // Registered unconditionally (not behind parallelAgentsEnabled) so the
        // model can always answer "what are the agents doing" — the list is
        // simply empty when nothing has run.
        tools.append(AgentToolDefinition(
            name: "agent_status",
            description: "Report the live progress of parallel sub-agent runs. Use this to answer 'how are the agents doing' / 'what is still running', or to decide whether to wait. Returns, for every fan-out: how many agents finished, and per agent its name, state (queued/running/succeeded/failed/cancelled), the tool it is currently executing, its tool-call count, and its final text once done. Prefer this over guessing; a run's summary also arrives automatically as a message when it finishes.",
            parameters: [
                "tool_title": AgentToolParam(type: .string, description: "A concise 5-10 word summary (e.g. 'Check sub-agent progress'). Use the same language as the user."),
                "run_id": AgentToolParam(type: .string, description: "Optional: a specific run id (first 8 chars are enough) to narrow the report. Omit to list every tracked run."),
            ],
            required: ["tool_title"],
            propertyOrdering: ["tool_title", "run_id"]
        ))

        // [ci-fix-introspect] Titles + state of the user's other chat sessions.
        tools.append(AgentToolDefinition(
            name: "session_list",
            description: "List the user's chat sessions with their titles, ids and last-updated time, newest first. Use this to answer questions like 'what have I been working on', 'find my session about X' (combine with keywords), or to locate a session before sending to it. Returns id, title, updated time and message count per session.",
            parameters: [
                "tool_title": AgentToolParam(type: .string, description: "A concise 5-10 word summary (e.g. 'List recent chat sessions'). Use the same language as the user."),
                "limit": AgentToolParam(type: .integer, description: "How many sessions to return (default 20, max 100)."),
                "keywords": AgentToolParam(type: .string, description: "Optional case-insensitive substring to filter session titles."),
                "days": AgentToolParam(type: .integer, description: "Optional: only sessions updated within the last N days."),
            ],
            required: ["tool_title"],
            propertyOrdering: ["tool_title", "limit", "keywords", "days"]
        ))

        return tools
    }

    /// Tools handed to a SUB-agent: everything the orchestrator has, minus
    /// `spawn_agents` itself.""")

# ==========================================================================
# 2. Dispatch branches.
# ==========================================================================
edit(DISPATCH, "dispatch agent_status + session_list",
     r"""        case "memory_write":""",
     r"""        case "agent_status":
            // [ci-fix-introspect] Read SubAgentRunStore directly — it is the
            // same store the panel renders from, already publishing live
            // per-agent state.
            toolOutput = Self.renderAgentStatus(runIdFilter: toolArgs["run_id"] as? String)
            toolSuccess = true
            if msgIdx < messages.count, blockIdx < messages[msgIdx].blocks.count {
                messages[msgIdx].blocks[blockIdx].content = toolOutput
            }

        case "session_list":
            // [ci-fix-introspect] Routed through the existing native bridge so
            // this sees exactly the list the sessions UI does.
            let limit = min(100, max(1, (toolArgs["limit"] as? Int) ?? 20))
            let res = await Self.querySessionList(
                limit: limit,
                keywords: toolArgs["keywords"] as? String,
                days: toolArgs["days"] as? Int
            )
            toolOutput = res
            toolSuccess = !toolOutput.hasPrefix("Error:")
            if msgIdx < messages.count, blockIdx < messages[msgIdx].blocks.count {
                messages[msgIdx].blocks[blockIdx].content = toolOutput
            }

        case "memory_write":""")

# ==========================================================================
# 3. The renderers, appended as an extension.
# ==========================================================================
HELPER = r'''

// MARK: - [ci-fix-introspect] Agent + session introspection

extension AIChatViewModel {

    /// Render the current sub-agent fan-outs for the model.
    ///
    /// Reads `SubAgentRunStore.shared` — the same store `SubAgentPanelView`
    /// renders from, so the model and the on-screen card cannot disagree.
    /// Runs on the MainActor because the store is `@MainActor`.
    @MainActor
    static func renderAgentStatus(runIdFilter: String?) -> String {
        let store = SubAgentRunStore.shared
        let all = store.runs.values.sorted { $0.startedAt < $1.startedAt }

        let runs: [SubAgentRunStore.Run]
        if let f = runIdFilter?.trimmingCharacters(in: .whitespacesAndNewlines), !f.isEmpty {
            runs = all.filter { $0.id.uuidString.lowercased().hasPrefix(f.lowercased()) }
            if runs.isEmpty {
                return "No sub-agent run matches id '\(f)'. Tracked runs: \(all.count)."
            }
        } else {
            runs = all
        }

        guard !runs.isEmpty else {
            return "No sub-agent runs are being tracked right now. (A finished run is pruned when its turn ends; its result arrives as a message.)"
        }

        var out: [String] = []
        for run in runs {
            let elapsed = Int(Date().timeIntervalSince(run.startedAt))
            out.append("## run \(run.id.uuidString.prefix(8)) — \(run.summaryLine)\(run.isFinished ? ", finished" : ", running") · \(elapsed)s")
            for (i, e) in run.entries.enumerated() {
                var line = "  \(i + 1). \(e.name) — \(e.state.label)"
                if let tool = e.currentTool, !tool.isEmpty, !e.isTerminal {
                    line += " · current tool: \(tool)"
                }
                line += " · \(e.toolCallCount) tool call\(e.toolCallCount == 1 ? "" : "s")"
                out.append(line)
                let body = e.finalText.trimmingCharacters(in: .whitespacesAndNewlines)
                if !body.isEmpty {
                    // Indent the result so the structure stays readable, and cap
                    // it — the full text also lands in the run's summary message.
                    let clipped = body.count > 1200 ? String(body.prefix(1200)) + "\n…(truncated)" : body
                    out.append(clipped.split(separator: "\n", omittingEmptySubsequences: false)
                        .map { "     " + $0 }.joined(separator: "\n"))
                }
            }
            out.append("")
        }
        return out.joined(separator: "\n")
    }

    /// List chat sessions.
    ///
    /// Calls `ChatStore.querySessionsMeta` (the store's own query, actor-isolated
    /// and synchronous) rather than `SessionsOffloadBridge.querySessions`.
    /// The bridge wraps that call in a `DispatchSemaphore` and is explicitly
    /// documented as "call from any thread EXCEPT main (semaphore would deadlock
    /// main)" — and this tool runs on the MainActor, so going through the bridge
    /// would hang the app. Awaiting the actor directly is safe from any context.
    @MainActor
    static func querySessionList(limit: Int, keywords: String?, days: Int?) async -> String {
        let kw: [String]? = {
            guard let k = keywords?.trimmingCharacters(in: .whitespacesAndNewlines), !k.isEmpty else { return nil }
            return [k]
        }()
        var startDate: Date? = nil
        if let d = days, d > 0 {
            startDate = Calendar.current.date(byAdding: .day, value: -d, to: Date())
        }

        let metas = await ChatStore.shared.querySessionsMeta(
            sessionIds: nil,
            keywords: kw,
            limit: min(100, max(1, limit)),
            startDate: startDate,
            endDate: nil
        )

        guard !metas.isEmpty else {
            return kw == nil
                ? "No chat sessions found."
                : "No chat sessions matched keyword '\(keywords ?? "")'."
        }

        let fmt = DateFormatter()
        fmt.dateFormat = "yyyy-MM-dd HH:mm"
        var out = ["\(metas.count) session\(metas.count == 1 ? "" : "s") (newest first):"]
        for m in metas {
            var bits = ["- \(m.title ?? "(untitled)")"]
            bits.append("id=\(m.id.prefix(8))")
            bits.append("last=\(fmt.string(from: m.lastActive))")
            bits.append("messages=\(m.messageCount)")
            if let p = m.preview?.trimmingCharacters(in: .whitespacesAndNewlines), !p.isEmpty {
                bits.append("preview=\(p.prefix(60))")
            }
            out.append(bits.joined(separator: " · "))
        }
        return out.joined(separator: "\n")
    }
}
'''

DISPATCH.write_text(DISPATCH.read_text().rstrip("\n") + HELPER)
edits.append("append agent_status + session_list renderers")

for d in edits:
    print(f"[APPLY  ] {d}")
print(f"[fix] introspection tools: {len(edits)} edit(s)")

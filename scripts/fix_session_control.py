#!/usr/bin/env python3
r"""
Retire the multi-agent feature and give the model session control instead.

## Multi-agent off

`parallelAgentsEnabled` gates `spawn_agents` out of the tool schema
(`if parallelAgentsEnabled { tools.append(spawn_agents) }`), so the feature is
retired by flipping that flag rather than by deleting code. The sub-agent
sources stay in the tree (they are part of the fork), still compile, and are
simply never offered to the model — so nothing can start them.

## The tools it replaces

`agent_status` is gone with the feature. `session_list` (read-only) becomes
`session_control`, which keeps the read path and adds the actions
`SessionsOffloadBridge` already exposes:

  list / read / status / send / retry / open

`send` is the important one: it posts a message to a session AS THE USER, so
the target session's agent acts on it exactly as if the user had typed it.

## The deadlock this must avoid

Every `SessionsOffloadBridge` method blocks its caller with
`DispatchSemaphore.wait()` and is documented "call from any thread EXCEPT main
(semaphore would deadlock main)". Agent tools run on the MainActor, so a direct
call freezes the app permanently.

Every call is therefore dispatched to a detached task and awaited. The semaphore
then blocks a BACKGROUND thread, and the bridge's internal `Task { @MainActor }`
still runs because the MainActor is *awaiting* at that moment — suspended, not
blocked. Keep every bridge call inside `Task.detached`; do not "simplify" one.

Usage: python3 fix_session_control.py <repo-root>
"""
import sys
from pathlib import Path

root = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
CH = root / "src" / "ios" / "Agent" / "Chat"
DEFS = CH / "AIChatViewModel+ToolDefinitions.swift"
DISPATCH = CH / "AIChatViewModel+ConcurrentTools.swift"
VM = CH / "AIChatViewModel.swift"

for p in (DEFS, DISPATCH, VM):
    if not p.exists():
        sys.exit(f"FATAL: missing {p}")

MARKER = "[T-retire-multiagent]"
if MARKER in DEFS.read_text():
    print("[ALREADY] session control patch present")
    sys.exit(0)

print("[GATE   ] retiring multi-agent; adding session control")

edits = []


def edit(path, desc, old, new, count=1):
    s = path.read_text()
    n = s.count(old)
    if n != count:
        sys.exit(f"FATAL: {desc}\n  {path.name}: expected {count}, found {n}\n  anchor: {old[:110]!r}")
    path.write_text(s.replace(old, new, count))
    edits.append(desc)


# ==========================================================================
# 1. Multi-agent off, using its own existing switch.
# ==========================================================================
# NOTE: an earlier revision merely flipped this flag to false. That left the
# registration one boolean away from coming back, and made "is the tool gone?"
# depend on a runtime value rather than on the shape of the code. The
# registration block is deleted below instead, so the tool cannot appear no
# matter what the flag says.
edit(VM, "parallelAgentsEnabled: off by default",
     r"""    @Published var parallelAgentsEnabled = true""",
     r"""    /// [T-retire-multiagent] OFF. `spawn_agents` is registered only when this is
    /// true, so the tool leaves the schema entirely and the model has no route to
    /// the sub-agent machinery. The sub-agent sources stay in the tree — they
    /// are part of the fork and still compile — they simply never run.
    ///
    /// For whoever finds this later: the fan-out produced N interleaved turns
    /// that were hard to read and, more to the point, could not be steered — a
    /// long unattended run spent budget with no way to see or stop it from the
    /// conversation that started it. The session tools below cover the same
    /// "work on something else while this runs" need with a model the user can
    /// actually control.
    @Published var parallelAgentsEnabled = false""")

# ==========================================================================
# 2. Register session_control.
# ==========================================================================
ANCHOR = """        return tools
    }

    /// Tools handed to a SUB-agent: everything the orchestrator has, minus
    /// `spawn_agents` itself."""

NEW_TOOL = r'''        // [T-retire-multiagent] Session control: inspect AND drive other
        // sessions. Replaces the read-only session_list and the multi-agent
        // agent_status.
        //
        // Every call routes through SessionsOffloadBridge — the same layer the
        // shells' minis-sessions-cli uses — so behaviour cannot drift between
        // the two. See runSessionControl for why each call is dispatched off
        // the main thread.
        tools.append(AgentToolDefinition(
            name: "session_control",
            description: """
            Inspect and control the user's chat sessions. `list` shows titles/ids/last-active; `read` returns a session's messages; `status` reports whether a session is running and what it is doing; `send` posts a message to a session AS THE USER; `retry` re-runs a session's last turn; `open` brings a session up in the UI.

            Use `send` when the user asks you to talk to another conversation, continue work there, or queue something for later — it is exactly equivalent to the user typing that message themselves, so the target session's agent will act on it. Pass no session_id to start a NEW session with that message.

            Prefer `send` over telling the user to go do it manually. Always `list` (or `status`) first when you need an id the user has not given you — ids are opaque, and read/status/send all need the real one, not a title.
            """,
            parameters: [
                "tool_title": AgentToolParam(type: .string, description: "A concise 5-10 word summary (e.g. 'List recent chat sessions', 'Send message to another session'). Use the same language as the user."),
                "action": AgentToolParam(type: .string, description: "One of: list, read, status, send, retry, open.", enumValues: ["list", "read", "status", "send", "retry", "open"]),
                "session_id": AgentToolParam(type: .string, description: "Target session id. Required for read/status/retry/open. For `send`, omit it to start a NEW session, or pass one to post into an existing session."),
                "message": AgentToolParam(type: .string, description: "For `send`: the message text, exactly as the user would type it."),
                "limit": AgentToolParam(type: .integer, description: "For list: how many sessions (default 20, max 100). For read: how many messages (default 30, max 200)."),
                "keywords": AgentToolParam(type: .string, description: "For list: optional case-insensitive filter on session titles."),
                "days": AgentToolParam(type: .integer, description: "For list: only sessions updated within the last N days."),
            ],
            required: ["tool_title", "action"],
            propertyOrdering: ["tool_title", "action", "session_id", "message", "limit", "keywords", "days"]
        ))

'''

edit(DEFS, "register the session_control tool",
     ANCHOR, NEW_TOOL + ANCHOR)

# ==========================================================================
# 2b. Delete the spawn_agents registration outright.
#
# Flipping `parallelAgentsEnabled` to false was not enough in practice: the
# tool still appeared at runtime, and because a disabled flag is invisible in
# the built binary there was no way to tell from the artifact whether the gate
# had taken. Deleting the registration makes the answer structural — the tool
# is absent because the code that adds it is gone.
# ==========================================================================
defs_src = DEFS.read_text()
_rs = defs_src.find("        // [T-parallel-subagents] Fan-out tool.")
_re = defs_src.find("        return tools", _rs)
if _rs < 0 or _re < 0:
    sys.exit("FATAL: could not locate the spawn_agents registration block")
_removed = defs_src[_re:] and defs_src[_rs:_re]
if "spawn_agents" not in _removed:
    sys.exit("FATAL: located block does not contain the registration")
DEFS.write_text(defs_src[:_rs] + defs_src[_re:])
print("[APPLY  ] deleted the spawn_agents registration block (unconditional)")
edits.append("spawn_agents registration deleted")

# ==========================================================================
# 3. Dispatch it.
# ==========================================================================
DISPATCH_ANCHOR = """        case "memory_write":"""

SESSION_BRANCH = r'''        case "session_control":
            // [T-retire-multiagent] Each branch is an await on a detached task;
            // see runSessionControl for the threading reason.
            let scResult = await Self.runSessionControl(
                action: (toolArgs["action"] as? String) ?? "",
                sessionId: toolArgs["session_id"] as? String,
                message: toolArgs["message"] as? String,
                limit: toolArgs["limit"] as? Int,
                keywords: toolArgs["keywords"] as? String,
                days: toolArgs["days"] as? Int
            )
            toolOutput = scResult
            toolSuccess = !toolOutput.hasPrefix("Error:")
            if msgIdx < messages.count, blockIdx < messages[msgIdx].blocks.count {
                messages[msgIdx].blocks[blockIdx].content = toolOutput
            }

'''

edit(DISPATCH, "dispatch session_control", DISPATCH_ANCHOR, SESSION_BRANCH + DISPATCH_ANCHOR)

# ==========================================================================
# 4. The runner.
# ==========================================================================
HELPER = r'''

// MARK: - [T-retire-multiagent] Session control

extension AIChatViewModel {

    /// Perform a `session_control` action.
    ///
    /// ## Threading — read before changing anything here
    ///
    /// Every `SessionsOffloadBridge` method blocks its caller with
    /// `DispatchSemaphore.wait()` while waiting on a `Task { @MainActor ... }`,
    /// and is documented "call from any thread EXCEPT main (semaphore would
    /// deadlock main)". Agent tools run on the MainActor, so calling a bridge
    /// method directly would block the main thread waiting for work that needs
    /// that same thread — an immediate, permanent freeze.
    ///
    /// So every call is dispatched to a detached task and awaited. The semaphore
    /// then blocks a BACKGROUND thread, and the bridge's own MainActor work can
    /// still run, because the MainActor is *awaiting* at that moment — suspended,
    /// not blocked. That distinction is the only reason this is safe rather than
    /// a deadlock relocated.
    ///
    /// Keep every bridge call inside `Task.detached`.
    @MainActor
    static func runSessionControl(
        action: String,
        sessionId: String?,
        message: String?,
        limit: Int?,
        keywords: String?,
        days: Int?
    ) async -> String {
        let sid = sessionId?.trimmingCharacters(in: .whitespacesAndNewlines)
        let kw: [String]? = {
            guard let k = keywords?.trimmingCharacters(in: .whitespacesAndNewlines), !k.isEmpty else { return nil }
            return [k]
        }()
        var startDate: Date? = nil
        if let d = days, d > 0 {
            startDate = Calendar.current.date(byAdding: .day, value: -d, to: Date())
        }

        switch action {
        case "list":
            let n = min(100, max(1, limit ?? 20))
            // ChatStore is an actor and its query is synchronous, so awaiting it
            // directly is safe from any context (unlike the semaphore bridges).
            let metas = await ChatStore.shared.querySessionsMeta(
                sessionIds: nil, keywords: kw, limit: n,
                startDate: startDate, endDate: nil
            )
            guard !metas.isEmpty else {
                return kw == nil ? "No chat sessions found."
                                 : "No chat sessions matched keyword '\(keywords ?? "")'."
            }
            let fmt = DateFormatter()
            fmt.dateFormat = "yyyy-MM-dd HH:mm"
            var out = ["\(metas.count) session\(metas.count == 1 ? "" : "s") (newest first):"]
            for m in metas {
                var bits = ["- \(m.title ?? "(untitled)")"]
                // FULL id, never a prefix: the bridge resolves sessions by exact
                // id, so a truncated one is unusable — you would read the list,
                // pass the id back to `status`, and get session_not_found.
                bits.append("id=\(m.id)")
                bits.append("last=\(fmt.string(from: m.lastActive))")
                bits.append("messages=\(m.messageCount)")
                if let p = m.preview?.trimmingCharacters(in: .whitespacesAndNewlines), !p.isEmpty {
                    bits.append("preview=\(p.prefix(60))")
                }
                out.append(bits.joined(separator: " · "))
            }
            return out.joined(separator: "\n")

        case "read":
            guard let id = sid, !id.isEmpty else { return "Error: 'session_id' is required for read." }
            let lim = min(200, max(1, limit ?? 30))
            let res = await Task.detached {
                SessionsOffloadBridge.loadMessages(
                    sessionId: id, offset: 0, limit: lim, full: false,
                    startDate: nil, endDate: nil
                )
            }.value
            return Self.renderBridgeResult(res, action: "read",
                                           emptyText: "That session has no messages.")

        case "status":
            guard let id = sid, !id.isEmpty else { return "Error: 'session_id' is required for status." }
            let res = await Task.detached {
                SessionsOffloadBridge.getSessionStatus(sessionId: id)
            }.value
            return Self.renderBridgeResult(res, action: "status",
                                           emptyText: "No status available.")

        case "send":
            let text = message?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
            guard !text.isEmpty else { return "Error: 'message' is required for send." }
            // A nil sessionId makes the bridge create a NEW session — that is its
            // documented behaviour, so pass it through unchanged.
            let target: String? = (sid?.isEmpty == false) ? sid : nil
            let res = await Task.detached {
                SessionsOffloadBridge.sendPrompt(
                    sessionId: target,
                    prompt: text,
                    attachmentPaths: [],
                    modelEntryId: nil,
                    source: "session_control"
                )
            }.value
            let rendered = Self.renderBridgeResult(res, action: "send", emptyText: "Message sent.")
            return target == nil ? "Started a new session with that message.\n" + rendered : rendered

        case "retry":
            guard let id = sid, !id.isEmpty else { return "Error: 'session_id' is required for retry." }
            let res = await Task.detached {
                SessionsOffloadBridge.retryMessage(sessionId: id, messageId: nil, attachmentPaths: [])
            }.value
            return Self.renderBridgeResult(res, action: "retry", emptyText: "Retry requested.")

        case "open":
            guard let id = sid, !id.isEmpty else { return "Error: 'session_id' is required for open." }
            let res = await Task.detached {
                SessionsOffloadBridge.openSession(sessionId: id)
            }.value
            return Self.renderBridgeResult(res, action: "open", emptyText: "Opened.")

        default:
            return "Error: unknown action '\(action)'. Use list, read, status, send, retry or open."
        }
    }

    /// Flatten a bridge NSDictionary into readable text.
    ///
    /// The bridge returns `["ok": Bool, "error": String?, ...]` plus a payload key
    /// that varies by call (`sessions`, `messages`, ...), so this prints the
    /// scalar fields and then whatever payload is present rather than assuming
    /// one shape — which would break the moment the bridge adds a key.
    nonisolated static func renderBridgeResult(
        _ res: NSDictionary, action: String, emptyText: String
    ) -> String {
        let dict = res as? [String: Any] ?? [:]
        if let ok = dict["ok"] as? Bool, ok == false {
            let err = (dict["error"] as? String) ?? (dict["message"] as? String) ?? "unknown error"
            return "Error: \(action) failed — \(err)"
        }

        var lines: [String] = []
        for key in ["status", "session_id", "sessionId", "message_id", "title"] {
            if let v = dict[key] { lines.append("\(key): \(v)") }
        }
        if let running = dict["isRunning"] as? Bool {
            lines.append("running: \(running)")
        }

        var payload: [[String: Any]] = []
        for key in ["messages", "sessions", "items", "results"] {
            if let arr = dict[key] as? [[String: Any]], !arr.isEmpty { payload = arr; break }
        }
        if payload.isEmpty {
            return lines.isEmpty ? emptyText : lines.joined(separator: "\n")
        }
        lines.append("\(payload.count) item\(payload.count == 1 ? "" : "s"):")
        for row in payload {
            let role = (row["role"] as? String) ?? (row["sender"] as? String) ?? "?"
            let text = (row["content"] as? String)
                ?? (row["text"] as? String)
                ?? (row["title"] as? String)
                ?? ""
            let flat = text.replacingOccurrences(of: "\n", with: " ")
            let clipped = flat.count > 300 ? String(flat.prefix(300)) + "…" : flat
            lines.append("  [\(role)] \(clipped)")
        }
        return lines.joined(separator: "\n")
    }
}
'''

DISPATCH.write_text(DISPATCH.read_text().rstrip("\n") + HELPER)
edits.append("append the session-control runner + bridge result renderer")

for d in edits:
    print(f"[APPLY  ] {d}")
print(f"[fix] session control: {len(edits)} edit(s)")

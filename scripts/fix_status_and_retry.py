#!/usr/bin/env python3
r"""
Four scheduling / status changes.

**(1) A "Keep Going" badge in the session list.**
`AutoContinueConfig.enabled` is persisted per session in UserDefaults but nothing
in the session list reads it, so a session quietly re-driving itself looks
identical to an idle one. Adds a `.autoContinue` badge state.

**(2) Network errors must not be auto-retried.**
`LLMError.isRetryable` currently returns true for `.networkError` and
`.transientError` alike, so a dropped connection burns the full
`[3,5,10,15,30]` countdown before failing — and, worse, re-issues the request
that already failed. A network fault is not something the same request retries
into working; it should surface immediately.

**(3) Transient server errors keep retrying** — that is what `transientError`
(HTTP 500/502/503/504/529) is for, and the distinction is already in the type.

**(4) At most one retry per 10 seconds.** Floor the countdown schedule so a
burst of transient failures cannot produce a tight retry loop.

Usage: python3 fix_status_and_retry.py <repo-root>
"""
import sys
from pathlib import Path

root = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
CH = root / "src" / "ios" / "Agent" / "Chat"
SHARED = root / "src" / "ios" / "Shared"
LLMERR = root / "src" / "ios" / "Providers" / "LLMError.swift"
FALLBACK = CH / "AIChatViewModel+Fallback.swift"
BADGE = SHARED / "SessionBadgeStore.swift"

for p in (LLMERR, FALLBACK, BADGE):
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
# (2) Network errors are NOT retryable.
# ==========================================================================
edit(LLMERR, "isRetryable: exclude network errors",
     r"""    /// Errors that should be retried with countdown on the same provider.
    /// Includes both network errors and transient server-side errors (5xx).
    var isRetryable: Bool {
        switch self {
        case .networkError, .transientError:
            return true
        case .invalidAPIKey, .providerError, .decodingError, .rateLimited, .cancelled, .unknown:
            return false
        }
    }""",
     r"""    /// Errors that should be retried with countdown on the same provider.
    ///
    /// [T-retry-transient-only] ONLY transient server-side faults (HTTP
    /// 500/502/503/504/529) qualify. A network error deliberately does NOT:
    /// re-issuing a request that just failed at the transport layer does not
    /// make it succeed, it only reproduces the failure after a countdown, so
    /// the user waits ~60s to see an error they could have had immediately.
    /// The distinction is the whole point of having both cases — keep it.
    var isRetryable: Bool {
        switch self {
        case .transientError:
            return true
        case .networkError, .invalidAPIKey, .providerError, .decodingError, .rateLimited, .cancelled, .unknown:
            return false
        }
    }

    /// [T-retry-transient-only] True for transport-level failures, which are
    /// surfaced at once rather than retried. Kept as its own predicate so the
    /// call sites read as intent rather than as a negated switch.
    var isTransportFailure: Bool {
        if case .networkError = self { return true }
        return false
    }""")

# ==========================================================================
# (3)(4) Countdown schedule: 10s floor, and no network retry path.
# ==========================================================================
edit(FALLBACK, "retry schedule: 10s floor",
     r"""    static let retryDelays = [3, 5, 10, 15, 30]""",
     r"""    /// [T-retry-transient-only] Countdown between transient-error retries.
    ///
    /// Every entry is >= 10s: a shorter gap let a burst of 5xx responses
    /// produce back-to-back retries with almost no spacing, which reads as a
    /// hammering loop on both the UI and the provider. The floor enforces
    /// "at most one retry per 10 seconds" regardless of which attempt we are on.
    static let retryDelays = [10, 15, 30, 60]

    /// [T-retry-transient-only] Minimum spacing between retries, enforced
    /// independently of the schedule above so a future edit to `retryDelays`
    /// cannot silently reintroduce a tight loop.
    static let minimumRetryInterval: TimeInterval = 10""")

edit(FALLBACK, "retry loop: clamp each delay to the floor",
     r"""            if attempt > 0 {
                let delay = Self.retryDelays[attempt - 1]""",
     r"""            if attempt > 0 {
                // [T-retry-transient-only] Clamp rather than trust the table —
                // the floor is the contract, the table is just a ramp.
                let delay = max(Int(Self.minimumRetryInterval), Self.retryDelays[attempt - 1])""")

# ==========================================================================
# (5) A session-list badge for sessions with Keep Going armed.
# ==========================================================================
edit(BADGE, "add the autoContinue badge state",
     r"""    case unread
}""",
     r"""    case unread

    /// [T-keepgoing-badge] "Keep Going" is armed for this session: when the
    /// agent next stops, a continuation prompt is sent automatically. Without a
    /// badge such a session is indistinguishable from an idle one, which is
    /// exactly when the user most needs to know — the mode can run unattended
    /// for a long time and spends tokens the whole while.
    ///
    /// Derived from `AutoContinueConfig.enabled` rather than pushed, because it
    /// is persisted state, not an event. See `SessionBadgeStore.autoContinueState(for:)`.
    case autoContinue
}""")

# The badge store needs to expose the state derived from persisted config.
edit(BADGE, "merge the derived autoContinue state into topCornerBadge",
     r"""    func topCornerBadge(for sessionId: String) -> SessionBadgeState? {
        badgeStates[sessionId]?.first { $0 != .unread }
    }""",
     r"""    func topCornerBadge(for sessionId: String) -> SessionBadgeState? {
        // [T-keepgoing-badge] Keep Going is PERSISTED CONFIG, not an event, so
        // it never enters the queue — the queue only carries things that happen.
        // Merge the derived state in at read time instead of pushing it, which
        // keeps a single source of truth (the mode's own UserDefaults entry) and
        // means the badge cannot go stale if the mode is changed from any path.
        //
        // Priority: an event badge (paused / syncing) wins, because it describes
        // something the user must act on, while Keep Going is a standing
        // condition that is still true a moment later.
        if let queued = badgeStates[sessionId]?.first(where: { $0 != .unread }) {
            return queued
        }
        return autoContinueState(for: sessionId)
    }""")

edit(BADGE, "expose the autoContinue derivation",
     r"""    /// sessionId → ordered badge states (index 0 = highest priority, shown).""",
     r"""    /// [T-keepgoing-badge] Whether Keep Going is armed for a session.
    ///
    /// Reads the same UserDefaults key the mode itself persists to, so the list
    /// and the chat cannot disagree. Deliberately a pure read: the mode writes
    /// the value, this only reports it, and there is no second source of truth
    /// to keep in sync.
    func autoContinueState(for sessionId: String) -> SessionBadgeState? {
        let key = "autoContinue.\(sessionId)"
        guard let data = UserDefaults.standard.data(forKey: key),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              (obj["enabled"] as? Bool) == true else { return nil }
        return .autoContinue
    }

    /// sessionId → ordered badge states (index 0 = highest priority, shown).""")

# ==========================================================================
# (5b) Render the badge. `badgeCircle(for:)` is an exhaustive switch over
#      SessionBadgeState, so the new case must be covered or nothing compiles.
# ==========================================================================
CONTENT = root / "src" / "ios" / "Views" / "ContentView.swift"
if not CONTENT.exists():
    sys.exit(f"FATAL: missing {CONTENT}")

edit(CONTENT, "badgeCircle: render the autoContinue badge",
     r"""        case .unread:
            // [T-ios-session-unread-badge] `.unread` renders as a separate
            // top-trailing red dot (see the .topTrailing overlay), never through
            // this bottom-trailing corner path. `topCornerBadge` filters it out
            // upstream, so this case is unreachable — render nothing defensively.
            EmptyView()
        }""",
     r"""        case .unread:
            // [T-ios-session-unread-badge] `.unread` renders as a separate
            // top-trailing red dot (see the .topTrailing overlay), never through
            // this bottom-trailing corner path. `topCornerBadge` filters it out
            // upstream, so this case is unreachable — render nothing defensively.
            EmptyView()
        case .autoContinue:
            // [T-keepgoing-badge] Keep Going armed: a green "infinity" glyph,
            // the same symbol the mode uses in its own settings row, on the
            // bottom-trailing corner alongside the other status badges.
            badgeCircle(icon: "infinity", color: .green, iconSize: 8)
        }""")

for d in edits:
    print(f"[APPLY  ] {d}")
print(f"[fix] status + retry: {len(edits)} edit(s)")

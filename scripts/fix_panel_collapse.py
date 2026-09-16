#!/usr/bin/env python3
r"""
Fix the collapsed sub-agent panel leaving its expanded height behind.

## Bug

`SubAgentPanelView` keeps its expand/collapse state in local `@State`, so
toggling it changes the hosting view's content without changing any item
identity or any anchor the list watches. `SelfSizingCell` caches the height it
measured (`lastComputedHeight`, reused while the width matches) and
`MessageListLayout` keeps its own `heightCache`. Nothing cleared either, so a
collapsed panel kept reserving its expanded height — the chat area above it
stayed short and a tall empty gap remained where the panel used to be.

This is the exact shape of the `.thinkingBlockToggled` bug, and the codebase
already has the fix pattern: post a notification from the view, and have the
list clear BOTH caches plus reconfigure the item so UIHostingConfiguration
re-measures from scratch.

## Fix

Mirror that pattern: the panel posts `.subAgentPanelToggled` on collapse and
expand, and the list handles it by clearing the cell height cache, the layout
height cache, and reconfiguring.

Usage: python3 fix_panel_collapse.py <repo-root>
"""
import sys
from pathlib import Path

root = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
VIEWS = root / "src" / "ios" / "Views" / "Chat" / "SubAgentPanel.swift"
LIST = root / "src" / "ios" / "Agent" / "MessageList" / "CollectionViewMessageListV3.swift"

for p in (VIEWS, LIST):
    if not p.exists():
        sys.exit(f"FATAL: missing {p}")

MARKER = "[ci-fix-collapse]"
if MARKER in VIEWS.read_text():
    print("[ALREADY] panel collapse patch present")
    sys.exit(0)

print("[GATE   ] applying panel collapse fix")

edits = []


def edit(path, desc, old, new, count=1):
    s = path.read_text()
    n = s.count(old)
    if n != count:
        sys.exit(f"FATAL: {desc}\n  {path.name}: expected {count}, found {n}\n  anchor: {old[:110]!r}")
    path.write_text(s.replace(old, new, count))
    edits.append(desc)


# ==========================================================================
# 1. The view posts a notification whenever its height changes.
# ==========================================================================
edit(VIEWS, "panel: declare the height-changed notification",
     r"""// MARK: - Panel""",
     r"""// MARK: - Panel

extension Notification.Name {
    /// [ci-fix-collapse] Posted by `SubAgentPanelView` when it expands or
    /// collapses. The panel's expansion is LOCAL `@State`, so toggling it
    /// changes the cell's content without changing any item identity or any
    /// anchor the list watches. `SelfSizingCell.lastComputedHeight` and
    /// `MessageListLayout.heightCache` both keep the old height, and the cell
    /// goes on reserving the expanded size — a tall blank gap that never
    /// shrinks. The list needs this signal to clear both caches and
    /// re-measure, exactly as `.thinkingBlockToggled` does for thinking pills.
    static let subAgentPanelToggled = Notification.Name("subAgentPanelToggled")
}""")

# Post on the expand/collapse transitions (header chevron).
edit(VIEWS, "panel: post on expand/collapse",
     r"""            Button {
                withAnimation(.easeInOut(duration: 0.18)) { expanded.toggle() }
            } label: {""",
     r"""            Button {
                toggleExpanded()
            } label: {""")

edit(VIEWS, "panel: post on header tap",
     r"""        .contentShape(Rectangle())
        .onTapGesture {
            withAnimation(.easeInOut(duration: 0.18)) { expanded.toggle() }
        }
    }""",
     r"""        .contentShape(Rectangle())
        .onTapGesture { toggleExpanded() }
    }

    /// [ci-fix-collapse] Flip `expanded` and tell the list, so it can drop the
    /// cached height. Without the post the card keeps its expanded footprint
    /// after collapsing (see `.subAgentPanelToggled`).
    private func toggleExpanded() {
        withAnimation(.easeInOut(duration: 0.18)) { expanded.toggle() }
        NotificationCenter.default.post(name: .subAgentPanelToggled, object: nil)
    }""")

# Per-agent row expansion changes height too.
edit(VIEWS, "panel: post on agent-row expand",
     r"""    private func toggleEntry(_ id: UUID) {
        if expandedEntryIds.contains(id) { expandedEntryIds.remove(id) }
        else { expandedEntryIds.insert(id) }
    }""",
     r"""    private func toggleEntry(_ id: UUID) {
        if expandedEntryIds.contains(id) { expandedEntryIds.remove(id) }
        else { expandedEntryIds.insert(id) }
        // [ci-fix-collapse] Expanding one agent's detail changes the card's
        // height as well, so the list has to re-measure for this too.
        NotificationCenter.default.post(name: .subAgentPanelToggled, object: nil)
    }""")

# ==========================================================================
# 2. The list handles it, mirroring the thinkingBlockToggled handler.
# ==========================================================================
edit(LIST, "list: store the panel-toggle subscription",
     r"""        private var bridgeSheetSubs: [UUID: AnyCancellable] = [:]""",
     r"""        private var bridgeSheetSubs: [UUID: AnyCancellable] = [:]

        /// [ci-fix-collapse] Subscription for the aggregate panel's
        /// expand/collapse. Mirrors `thinkingToggleSub`.
        private var panelToggleSub: AnyCancellable?""")

edit(LIST, "list: subscribe to the panel toggle",
     r"""                thinkingToggleSub = NotificationCenter.default.publisher(for: .thinkingBlockToggled)""",
     r"""                // [ci-fix-collapse] The panel's expand state is local @State, so
                // no item identity changes when it toggles. Clear BOTH caches and
                // reconfigure every panel item so the hosting view re-measures —
                // otherwise a collapsed card keeps reserving its expanded height
                // and leaves a blank gap in the transcript. Same shape as the
                // thinking-pill handler directly below.
                if panelToggleSub == nil {
                    panelToggleSub = NotificationCenter.default.publisher(for: .subAgentPanelToggled)
                        .receive(on: DispatchQueue.main)
                        .sink { [weak self] _ in
                            let plog = AppLogger(category: "SubAgentPanel")
                            guard let self,
                                  let cv = self.viewController?.collectionView,
                                  let layout = cv.collectionViewLayout as? MessageListLayout,
                                  var snapshot = self.dataSource?.snapshot() else {
                                plog.warning("[PanelCollapse] infra missing")
                                return
                            }
                            var hit = 0
                            for (i, item) in snapshot.itemIdentifiers.enumerated() {
                                guard case .assistantPanel = item else { continue }
                                hit += 1
                                // Cell-side cache: reused while the width matches, and
                                // it is the FIRST thing PLAF checks, so a stale entry
                                // wins over any layout-side invalidation.
                                let ip = IndexPath(item: i, section: 0)
                                (cv.cellForItem(at: ip) as? SelfSizingCell)?.clearCachedHeight()
                                // Layout-side caches (heightCache + precalc).
                                layout.invalidateHeight(at: i)
                            }
                            guard hit > 0 else { return }
                            snapshot.reconfigureItems(snapshot.itemIdentifiers.filter {
                                if case .assistantPanel = $0 { return true }
                                return false
                            })
                            self.dataSource?.apply(snapshot, animatingDifferences: false)
                            layout.invalidateLayout()
                            plog.info("[PanelCollapse] re-measured \(hit) panel item(s)")
                        }
                }

                thinkingToggleSub = NotificationCenter.default.publisher(for: .thinkingBlockToggled)""")

for d in edits:
    print(f"[APPLY  ] {d}")
print(f"[fix] panel collapse: {len(edits)} edit(s)")

//
//  SubAgentPanel.swift
//  MinisApp
//
//  [ci-fix-panel] The in-chat card for a parallel sub-agent fan-out.
//
//  ## Why one card, not N bubbles
//
//  A fan-out is a single event in the conversation. The first cut gave every
//  agent its own assistant bubble, which turned a 5-way fan-out into five
//  interleaved half-conversations and gave the user nothing to control. The
//  agents still write into their own hidden container messages (that is what
//  keeps concurrent agents race-free), and THIS card is what the user sees.
//
//  ## Where the data comes from
//
//  `SubAgentRunStore` was already publishing everything needed — per agent
//  `state`, `currentTool`, `toolCallCount`, `finalText`, and per run
//  `doneCount`, `summaryLine`, `isFinished` — with no reader anywhere in the
//  app. The card simply subscribes to it, so the display is live with no new
//  plumbing and no polling.
//

import SwiftUI

// MARK: - Panel

/// Aggregate card for one `spawn_agents` fan-out.
struct SubAgentPanelView: View {
    /// The run this card represents.
    @ObservedObject var run: SubAgentRunStore.Run
    /// Called when the user taps Cancel on the whole run.
    var onCancelRun: () -> Void
    /// Called when the user taps Cancel on one agent.
    var onCancelEntry: (UUID) -> Void

    @State private var expanded = true
    @State private var expandedEntryIds: Set<UUID> = []

    /// `n/m` progress plus elapsed seconds, for the header.
    private var progressText: String {
        "\(run.doneCount)/\(run.entries.count)"
    }

    private var elapsedText: String {
        let secs = Int(Date().timeIntervalSince(run.startedAt))
        if secs < 60 { return "\(secs)s" }
        return "\(secs / 60)m \(secs % 60)s"
    }

    private var headerIcon: String {
        if run.isFinished { return "checkmark.circle.fill" }
        return "circle.dotted"
    }

    private var headerTint: Color {
        run.isFinished ? .green : .accentColor
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            header
            if expanded {
                Divider().opacity(0.5)
                VStack(alignment: .leading, spacing: 0) {
                    ForEach(Array(run.entries.enumerated()), id: \.element.id) { idx, entry in
                        SubAgentEntryRow(
                            index: idx + 1,
                            entry: entry,
                            isExpanded: expandedEntryIds.contains(entry.id),
                            onToggle: { toggleEntry(entry.id) },
                            onCancel: { onCancelEntry(entry.id) }
                        )
                        if entry.id != run.entries.last?.id {
                            Divider().opacity(0.35).padding(.leading, 44)
                        }
                    }
                }
            }
        }
        .background(
            RoundedRectangle(cornerRadius: 14, style: .continuous)
                .fill(Color(uiColor: .secondarySystemBackground))
        )
        .overlay(
            RoundedRectangle(cornerRadius: 14, style: .continuous)
                .strokeBorder(headerTint.opacity(0.25), lineWidth: 1)
        )
        .padding(.vertical, 4)
    }

    private func toggleEntry(_ id: UUID) {
        if expandedEntryIds.contains(id) { expandedEntryIds.remove(id) }
        else { expandedEntryIds.insert(id) }
    }

    // MARK: Header

    private var header: some View {
        HStack(spacing: 10) {
            Image(systemName: headerIcon)
                .foregroundStyle(headerTint)
                .font(.system(size: 17, weight: .medium))
                // A slow pulse while running, so a long fan-out reads as alive
                // rather than frozen. Deployment target is iOS 16, so the
                // iOS 17 symbolEffect gets an opacity-breath fallback — the
                // same pattern BrowserSheetView uses.
                .modifier(PulseWhenActive(active: !run.isFinished))

            VStack(alignment: .leading, spacing: 1) {
                Text(run.isFinished
                     ? AppLocalized("Sub-agents finished")
                     : AppLocalized("Sub-agents running"))
                    .font(.system(size: 15, weight: .semibold))
                    .foregroundStyle(ChatColors.primaryText)
                Text("\(progressText) · \(elapsedText)")
                    .font(.system(size: 12))
                    .foregroundStyle(ChatColors.secondaryText)
                    .monospacedDigit()
            }

            Spacer(minLength: 8)

            if !run.isFinished {
                Button(action: onCancelRun) {
                    Image(systemName: "stop.circle.fill")
                        .font(.system(size: 20))
                        .foregroundStyle(.red)
                }
                .buttonStyle(.plain)
                .accessibilityLabel(AppLocalized("Cancel all sub-agents"))
            }

            Button {
                withAnimation(.easeInOut(duration: 0.18)) { expanded.toggle() }
            } label: {
                Image(systemName: expanded ? "chevron.up" : "chevron.down")
                    .font(.system(size: 13, weight: .semibold))
                    .foregroundStyle(ChatColors.secondaryText)
                    .frame(width: 28, height: 28)
                    .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityLabel(expanded ? AppLocalized("Collapse") : AppLocalized("Expand"))
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 11)
        .contentShape(Rectangle())
        .onTapGesture {
            withAnimation(.easeInOut(duration: 0.18)) { expanded.toggle() }
        }
    }
}

// MARK: - One agent row

private struct SubAgentEntryRow: View {
    let index: Int
    @ObservedObject var entry: SubAgentRunStore.Entry
    let isExpanded: Bool
    let onToggle: () -> Void
    let onCancel: () -> Void

    private var statusIcon: String {
        switch entry.state {
        case .queued:    return "clock"
        case .running:   return "circle.dotted"
        case .succeeded: return "checkmark.circle.fill"
        case .failed:    return "exclamationmark.circle.fill"
        case .cancelled: return "slash.circle.fill"
        }
    }

    private var statusTint: Color {
        switch entry.state {
        case .queued:    return .secondary
        case .running:   return .accentColor
        case .succeeded: return .green
        case .failed:    return .orange
        case .cancelled: return .secondary
        }
    }

    private var hasDetail: Bool {
        !entry.finalText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 10) {
                Image(systemName: statusIcon)
                    .font(.system(size: 14, weight: .medium))
                    .foregroundStyle(statusTint)
                    .frame(width: 18)
                    .modifier(PulseWhenActive(active: entry.state == .running))

                VStack(alignment: .leading, spacing: 1) {
                    HStack(spacing: 6) {
                        Text("\(index).")
                            .font(.system(size: 13, weight: .medium))
                            .foregroundStyle(ChatColors.secondaryText)
                            .monospacedDigit()
                        Text(entry.name)
                            .font(.system(size: 14, weight: .medium))
                            .foregroundStyle(ChatColors.primaryText)
                            .lineLimit(1)
                    }
                    // The live tool line — this is what makes the panel useful
                    // while it runs: you can see which agent is doing what.
                    Text(entry.rowSummary)
                        .font(.system(size: 12))
                        .foregroundStyle(ChatColors.secondaryText)
                        .lineLimit(1)
                        .truncationMode(.middle)
                }

                Spacer(minLength: 8)

                if entry.toolCallCount > 0 {
                    Text("\(entry.toolCallCount)")
                        .font(.system(size: 11, weight: .medium))
                        .foregroundStyle(ChatColors.secondaryText)
                        .padding(.horizontal, 6)
                        .padding(.vertical, 2)
                        .background(
                            Capsule().fill(Color(uiColor: .tertiarySystemFill))
                        )
                        .monospacedDigit()
                }

                if !entry.isTerminal {
                    Button(action: onCancel) {
                        Image(systemName: "xmark.circle.fill")
                            .font(.system(size: 17))
                            .foregroundStyle(ChatColors.secondaryText)
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel(AppLocalized("Cancel this sub-agent"))
                }

                if hasDetail {
                    Image(systemName: isExpanded ? "chevron.up" : "chevron.down")
                        .font(.system(size: 11, weight: .semibold))
                        .foregroundStyle(ChatColors.secondaryText)
                }
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 9)
            .contentShape(Rectangle())
            .onTapGesture { if hasDetail { onToggle() } }

            if isExpanded && hasDetail {
                Text(entry.finalText)
                    .font(.system(size: 13))
                    .foregroundStyle(ChatColors.primaryText)
                    .textSelection(.enabled)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.horizontal, 14)
                    .padding(.bottom, 10)
            }
        }
    }
}


// MARK: - iOS 16 fallback

/// `symbolEffect(.pulse, isActive:)` is iOS 17+; the app deploys to iOS 16.
/// Mirrors the availability pattern in BrowserSheetView's DownloadPulseModifier.
private struct PulseWhenActive: ViewModifier {
    let active: Bool
    @State private var legacyPulse = false

    func body(content: Content) -> some View {
        if #available(iOS 17, *) {
            content.symbolEffect(.pulse, options: .repeating, isActive: active)
        } else {
            content
                .opacity(active && legacyPulse ? 0.45 : 1.0)
                .animation(active
                           ? .easeInOut(duration: 0.9).repeatForever(autoreverses: true)
                           : .default,
                           value: legacyPulse)
                .onAppear { if active { legacyPulse = true } }
                .onDisappear { legacyPulse = false }
        }
    }
}

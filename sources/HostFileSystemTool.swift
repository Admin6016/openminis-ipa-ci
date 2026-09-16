//
//  HostFileSystemTool.swift
//  MinisApp
//
//  [T-trollstore-capabilities] Real-iOS-filesystem access, gated on the
//  `no-container` capability.
//
//  ## What this is for
//
//  Normally the model can only touch paths inside this app's container plus
//  whatever the user mounted. With `com.apple.private.security.no-container`
//  the whole iOS filesystem is reachable: `/var/mobile/Containers/...` (other
//  apps' data), `/Applications` (installed app bundles), `/var/mobile/Media`
//  (the photo library's on-disk store), system plists, and so on. This tool
//  exposes that, and NOTHING else — the sandbox is what provides the rest.
//
//  ## Registration contract
//
//  `makeHostFilesystemTool()` returns a definition ONLY when the capability is
//  present. An App Store or ordinary-sideload build holds no `no-container`
//  entitlement, so the tool is absent from the schema the model sees and can
//  never be called. That is deliberately stronger than answering "not
//  permitted" at call time: the model has no reason to plan around a tool it
//  cannot see, and the tool's own description (which promises filesystem
//  access) never appears on a build where it would be a lie.
//
//  ## Safety
//
//  Reads are unrestricted within what the entitlement grants. WRITES require an
//  explicit `confirm: true` argument, because this is the one tool that can
//  modify files outside the app's own container — including other apps' data.
//

import Foundation

extension AIChatViewModel {

    /// Tool definition for real-iOS-filesystem access.
    ///
    /// Returns nil when the process holds no filesystem-escaping entitlement,
    /// which is the case for every non-TrollStore build.
    static func makeHostFilesystemTool() -> AgentToolDefinition? {
        guard HostCapabilities.canReachRealFilesystem else { return nil }

        return AgentToolDefinition(
            name: "ios_fs",
            description: """
            Access the REAL iOS filesystem (not the Linux sandbox). Available only on builds signed with a filesystem-escaping entitlement.

            Unlike shell_execute — which runs inside iSH's emulated Alpine filesystem and can only see the app's own container — this reads the actual device paths, so you can inspect other apps' data, installed app bundles, system configuration files and the media store.

            Useful real paths: /var/mobile/Containers/Data/Application/<uuid>/ ; /var/mobile/Containers/Bundle/Application/<uuid>/ ; /Applications ; /var/mobile/Media/ ; /System/Library ; /var/mobile/Library/Preferences.

            Actions: `ls` (list a directory), `stat` (size/type/timestamps), `read` (text or base64 for binary, byte-capped), `find` (name search under a path, depth- and count-capped), `write` (requires confirm:true), `mkdir` (requires confirm:true), `rm` (requires confirm:true).

            Prefer this over shell_execute whenever the target path starts with /var/mobile, /Applications or /System — the shell cannot see those.
            """,
            parameters: [
                "tool_title": AgentToolParam(type: .string, description: "A concise 5-10 word summary of what this call does (e.g. 'List another app's Documents', 'Read system plist'). Use the same language as the user."),
                "action": AgentToolParam(type: .string, description: "One of: ls, stat, read, find, write, mkdir, rm."),
                "path": AgentToolParam(type: .string, description: "Absolute iOS path, e.g. /var/mobile/Containers/Data/Application or /Applications."),
                "content": AgentToolParam(type: .string, description: "For write: the text to write. For read of binary files, the result is returned base64-encoded."),
                "max_bytes": AgentToolParam(type: .integer, description: "For read: cap on bytes returned (default 65536, max 1048576). Large files are truncated."),
                "max_depth": AgentToolParam(type: .integer, description: "For find: maximum recursion depth (default 3, max 8)."),
                "limit": AgentToolParam(type: .integer, description: "For ls/find: maximum entries returned (default 200, max 2000)."),
                "confirm": AgentToolParam(type: .boolean, description: "REQUIRED for write/mkdir/rm. Set true only when the user has asked for this change; these actions modify files outside the app."),
            ],
            required: ["tool_title", "action", "path"],
            propertyOrdering: ["tool_title", "action", "path", "content", "max_bytes", "max_depth", "limit", "confirm"]
        )
    }

    /// Execute an `ios_fs` call. Returns the tool result text.
    nonisolated static func runHostFilesystem(action: String, path: String, args: [String: Any]) -> String {
        // Defence in depth: even if the schema somehow advertised this tool on
        // a build without the entitlement, refuse here rather than probing.
        guard HostCapabilities.canReachRealFilesystem else {
            return "Error: this build holds no filesystem-escaping entitlement, so real iOS paths are not reachable."
        }
        guard path.hasPrefix("/") else {
            return "Error: 'path' must be absolute (it is resolved against the real iOS root, not the sandbox)."
        }

        let fm = FileManager.default
        let url = URL(fileURLWithPath: path)
        let maxBytes = min(1_048_576, max(1024, (args["max_bytes"] as? Int) ?? 65_536))
        let limit = min(2000, max(1, (args["limit"] as? Int) ?? 200))
        let depth = min(8, max(1, (args["max_depth"] as? Int) ?? 3))
        let confirm = (args["confirm"] as? Bool) ?? false

        switch action {
        case "ls":
            guard let entries = try? fm.contentsOfDirectory(
                at: url,
                includingPropertiesForKeys: [.isDirectoryKey, .fileSizeKey, .isSymbolicLinkKey],
                options: []
            ) else {
                return "Error: cannot list '\(path)' (missing, or not a directory)."
            }
            var lines = ["\(entries.count) entr\(entries.count == 1 ? "y" : "ies") in \(path)"]
            for e in entries.prefix(limit) {
                let v = try? e.resourceValues(forKeys: [.isDirectoryKey, .fileSizeKey, .isSymbolicLinkKey])
                let kind = (v?.isSymbolicLink ?? false) ? "link" : (v?.isDirectory ?? false) ? "dir " : "file"
                let size = v?.fileSize.map { String($0) } ?? "-"
                lines.append(String(format: "  %@ %10@  %@", kind, size as NSString, e.lastPathComponent))
            }
            if entries.count > limit { lines.append("  … (+\(entries.count - limit) more)") }
            return lines.joined(separator: "\n")

        case "stat":
            guard let attrs = try? fm.attributesOfItem(atPath: path) else {
                return "Error: cannot stat '\(path)'."
            }
            let type = (attrs[.type] as? FileAttributeType)?.rawValue ?? "?"
            let size = (attrs[.size] as? NSNumber)?.intValue ?? -1
            var out = ["path: \(path)", "type: \(type)", "size: \(size) bytes"]
            if let m = attrs[.modificationDate] as? Date { out.append("modified: \(m)") }
            if let o = attrs[.ownerAccountName] as? String { out.append("owner: \(o)") }
            if let p = attrs[.posixPermissions] as? NSNumber {
                out.append("mode: \(String(p.intValue, radix: 8))")
            }
            return out.joined(separator: "\n")

        case "read":
            guard let data = try? Data(contentsOf: url) else {
                return "Error: cannot read '\(path)'."
            }
            let slice = data.prefix(maxBytes)
            let truncated = data.count > slice.count
            // Text when it decodes cleanly, base64 otherwise — the model cannot
            // do anything useful with raw binary in a string.
            if let text = String(data: slice, encoding: .utf8) {
                return truncated
                    ? "\(text)\n\n…(truncated at \(slice.count) of \(data.count) bytes)"
                    : text
            }
            return "base64 (\(slice.count) of \(data.count) bytes):\n\(slice.base64EncodedString())"

        case "find":
            guard fm.fileExists(atPath: path) else { return "Error: '\(path)' does not exist." }
            var hits: [String] = []
            let needle = (args["name"] as? String)?.lowercased()
            func walk(_ dir: URL, _ level: Int) {
                guard level <= depth, hits.count < limit else { return }
                guard let kids = try? fm.contentsOfDirectory(
                    at: dir, includingPropertiesForKeys: [.isDirectoryKey], options: []) else { return }
                for k in kids {
                    guard hits.count < limit else { return }
                    let name = k.lastPathComponent
                    if needle == nil || name.lowercased().contains(needle!) {
                        hits.append(k.path)
                    }
                    let isDir = (try? k.resourceValues(forKeys: [.isDirectoryKey]))?.isDirectory ?? false
                    if isDir { walk(k, level + 1) }
                }
            }
            walk(url, 1)
            guard !hits.isEmpty else {
                return needle == nil ? "No entries found under '\(path)' (depth \(depth))."
                                     : "No name matching '\(needle!)' under '\(path)' (depth \(depth))."
            }
            return "\(hits.count) match(es)\(hits.count >= limit ? " (capped at \(limit))" : ""):\n"
                + hits.joined(separator: "\n")

        case "write":
            guard confirm else { return "Refused: 'write' modifies files outside the app. Pass confirm:true to proceed." }
            guard let content = args["content"] as? String else { return "Error: 'content' is required for write." }
            do {
                try content.data(using: .utf8)?.write(to: url, options: .atomic)
                return "Wrote \(content.utf8.count) bytes to \(path)"
            } catch {
                return "Error: write failed — \(error.localizedDescription)"
            }

        case "mkdir":
            guard confirm else { return "Refused: pass confirm:true to create directories outside the app." }
            do {
                try fm.createDirectory(at: url, withIntermediateDirectories: true)
                return "Created \(path)"
            } catch {
                return "Error: mkdir failed — \(error.localizedDescription)"
            }

        case "rm":
            guard confirm else { return "Refused: 'rm' deletes files outside the app. Pass confirm:true to proceed." }
            do {
                try fm.removeItem(at: url)
                return "Removed \(path)"
            } catch {
                return "Error: rm failed — \(error.localizedDescription)"
            }

        default:
            return "Error: unknown action '\(action)'. Use ls, stat, read, find, write, mkdir or rm."
        }
    }
}

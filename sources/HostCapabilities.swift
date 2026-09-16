//
//  HostCapabilities.swift
//  MinisApp
//
//  [T-trollstore-capabilities] Runtime capability detection for jailbreak-only
//  features.
//
//  ## Why detect CAPABILITIES, not "is this TrollStore"
//
//  TrollStore is an INSTALL METHOD, not a runtime property. Once installed, the
//  app is an ordinary app and nothing in-process says "TrollStore put me here".
//  What actually differs is the ENTITLEMENT SET the binary was signed with:
//  TrollStore re-signs with whatever entitlements are embedded in the Mach-O,
//  including private ones the App Store build can never obtain.
//
//  So the honest question is "which entitlements do I actually hold right now",
//  and that is what this answers.
//
//  ## Why this matters for tool registration
//
//  A tool that needs `no-container` must NOT be advertised on a build without
//  it: the model would call it and get an error it cannot reason about, and the
//  tool's very existence would describe filesystem access the process does not
//  have. Registration is therefore gated on the capability itself, so an
//  App Store / ordinary-sideload build simply never sees those tools.
//

import Foundation
import Security

/// Entitlements this build may or may not hold, and the feature each unlocks.
enum HostCapability: String, CaseIterable {
    /// `com.apple.private.security.no-container` — escapes the app container so
    /// the whole iOS filesystem is reachable, not just this app's Documents.
    case noContainer = "com.apple.private.security.no-container"

    /// `com.apple.private.security.storage.AppDataContainers` — read/write other
    /// apps' data containers.
    case appDataContainers = "com.apple.private.security.storage.AppDataContainers"

    /// `com.apple.private.security.platform-application` — run as a platform
    /// application, which relaxes several further restrictions.
    case platformApplication = "com.apple.private.security.platform-application"

    /// `com.apple.private.task_for_pid` — inspect other processes.
    case taskForPid = "com.apple.private.task_for_pid"

    /// `get-task-allow` — debuggable; also relaxes a few runtime guards.
    case getTaskAllow = "get-task-allow"

    /// `com.apple.private.dynamic-codesigning` — runtime code-page modification.
    case dynamicCodeSigning = "com.apple.private.dynamic-codesigning"

    /// Plain-English name for logs / the capabilities readout.
    var displayName: String {
        switch self {
        case .noContainer:          return "filesystem escape (no-container)"
        case .appDataContainers:    return "other apps' containers"
        case .platformApplication:  return "platform application"
        case .taskForPid:           return "process inspection (task_for_pid)"
        case .getTaskAllow:         return "debuggable (get-task-allow)"
        case .dynamicCodeSigning:   return "dynamic code signing"
        }
    }
}

/// What this process is actually allowed to do.
///
/// Computed once and cached: entitlements are fixed for the lifetime of the
/// process, and `SecTaskCopyValueForEntitlement` is not free.
enum HostCapabilities {

    /// Entitlements held by the running binary.
    private static let held: Set<HostCapability> = {
        let found = Set(HostCapability.allCases.filter { hasEntitlement($0.rawValue) })
        let names = found.map(\.displayName).sorted().joined(separator: ", ")
        AppLogger(category: "HostCap").warning(
            "[HostCap] detected \(found.count) private entitlement(s): \(found.isEmpty ? "none (sandboxed build)" : names)")
        return found
    }()

    /// True when this binary holds `capability`.
    static func has(_ capability: HostCapability) -> Bool {
        held.contains(capability)
    }

    /// True when the sandbox container is escaped, i.e. arbitrary iOS paths are
    /// reachable. This is the gate for every real-filesystem tool.
    static var canReachRealFilesystem: Bool {
        has(.noContainer) || has(.platformApplication)
    }

    /// True when other apps' data containers are readable.
    static var canReachOtherApps: Bool {
        has(.appDataContainers) || canReachRealFilesystem
    }

    /// Human-readable summary, for a settings readout or tool output.
    static var summary: String {
        if held.isEmpty { return "sandboxed (no private entitlements)" }
        return held.map(\.displayName).sorted().joined(separator: ", ")
    }

    // MARK: - Entitlement lookup

    /// Read one entitlement off this process's own code signature.
    ///
    /// `SecTaskCreateFromSelf` + `SecTaskCopyValueForEntitlement` is the
    /// documented way to ask "what am I allowed to do" without shelling out to
    /// codesign (which the sandbox forbids). Returns false on any failure —
    /// absence of proof is treated as absence of capability, which is the safe
    /// direction: a tool is hidden rather than offered and then broken.
    private static func hasEntitlement(_ key: String) -> Bool {
        guard let task = SecTaskCreateFromSelf(nil) else { return false }
        defer { CFRelease(task) }
        guard let value = SecTaskCopyValueForEntitlement(task, key as CFString, nil) else {
            return false
        }
        // Capabilities are booleans; tolerate the string "true" too, since a
        // hand-written entitlements plist can produce either.
        if let b = value as? Bool { return b }
        if let n = value as? NSNumber { return n.boolValue }
        if let s = value as? String { return s == "true" || s == "1" }
        return false
    }
}

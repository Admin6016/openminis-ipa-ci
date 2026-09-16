//
//  HostCapabilities.swift
//  MinisApp
//
//  [T-trollstore-capabilities] Runtime capability detection for
//  jailbreak-only features.
//
//  ## Why detect CAPABILITIES, not "is this TrollStore"
//
//  TrollStore is an INSTALL METHOD, not a runtime property. Once installed, the
//  app is an ordinary app and nothing in-process says "TrollStore put me here".
//  What actually differs is what the process is ALLOWED to do.
//
//  ## Why a functional probe instead of reading the signature
//
//  The obvious approach is to read the entitlements off our own code signature.
//  Two problems with that on iOS:
//
//    * the tidy `SecTaskCreateFromSelf` pair is in the PRIVATE SecTask.h and
//      does not compile against a stock toolchain;
//    * `SecCodeCopySigningInformation` is public but its availability for a
//      *running* process is not something to bet a feature gate on.
//
//  Neither is necessary. The question we actually care about is
//
//      "can this process reach a path outside its own container?"
//
//  which can be answered by TRYING it. A probe is more honest than paperwork:
//  it reports the capability that exists, not the one the signature claims, and
//  it needs no private API, no extra framework, and no special-casing. It also
//  degrades correctly — if a future iOS build starts blocking these paths
//  despite the entitlement, the probe reports "no" and the gated tools stay
//  hidden, which is exactly the safe direction.
//
//  ## Why this matters for tool registration
//
//  A tool that needs container escape must NOT be advertised on a build without
//  it: the model would call it and get an error it cannot reason about, and the
//  tool's own description would promise filesystem access the process does not
//  have. Registration is therefore gated here, so an App Store / ordinary
//  sideload build simply never sees those tools.
//

import Foundation

/// What this process is actually able to reach.
enum HostCapabilities {

    // MARK: - Probes

    /// Paths that are only readable from OUTSIDE the app container, i.e. only
    /// when `com.apple.private.security.no-container` (or platform-application)
    /// is in effect. Each is a well-known, stable, read-only location.
    ///
    /// `/Applications` is the primary canary: on a normal sandboxed app even
    /// `fileExists` there is refused, while an escaped process can list it. The
    /// others are corroborating signals for the same privilege.
    private static let escapeCanaries: [String] = [
        "/Applications",
        "/var/mobile/Containers/Data/Application",
        "/var/mobile/Media",
    ]

    /// True when a path outside the container can actually be enumerated.
    ///
    /// `contentsOfDirectory` is used rather than `fileExists` because existence
    /// checks can succeed through a symlink or a stale cache, whereas an actual
    /// directory listing has to pass the sandbox check for real.
    private static func canList(_ path: String) -> Bool {
        guard FileManager.default.fileExists(atPath: path) else { return false }
        return (try? FileManager.default.contentsOfDirectory(atPath: path)) != nil
    }

    /// The first canary that is reachable, or nil when none is. Kept so the
    /// log says WHICH path proved the escape, which is what makes a surprising
    /// result diagnosable later.
    private static let reachableCanary: String? = {
        escapeCanaries.first(where: canList)
    }()

    // MARK: - Results

    /// True when the sandbox container is escaped, i.e. arbitrary iOS paths are
    /// reachable. This is the gate for every real-filesystem tool.
    static let canReachRealFilesystem: Bool = {
        let ok = reachableCanary != nil
        AppLogger(category: "HostCap").warning(
            ok
            ? "[HostCap] container escape CONFIRMED (probe reached \(reachableCanary!)) — real-filesystem tools enabled"
            : "[HostCap] no container escape (probed \(escapeCanaries.count) path(s), all refused) — sandboxed build")
        return ok
    }()

    /// True when other apps' data containers are readable.
    ///
    /// Same privilege in practice: `no-container` grants the whole filesystem,
    /// and `AppDataContainers` is the narrower entitlement that grants exactly
    /// this. Either one lights it up, so it reduces to the escape probe.
    static var canReachOtherApps: Bool { canReachRealFilesystem }

    /// Human-readable summary, for logs or a settings readout.
    static var summary: String {
        canReachRealFilesystem
            ? "container escape active (real iOS filesystem reachable)"
            : "sandboxed (app container only)"
    }
}

#!/usr/bin/env python3
r"""
Make `session_control send` work on a session that is already running.

## The bug

`SessionsOffloadBridge.sendPrompt` does:

    vm.inputText = prompt
    vm.send()

and `send()` opens with

    guard !text.isEmpty || !pendingAttachments.isEmpty, !isProcessing else { return }

So on a busy session `isProcessing == true`, the guard fires, and the text is
left sitting in `inputText`. Nothing is sent — which is exactly what the user
sees: the message appears in the composer and never leaves it.

## Why this is not simply "make the bridge queue it"

`enqueuePrompt()` is the app's existing mechanism for exactly this case: it
appends a `QueuedPrompt` to `promptQueue`, shows the text in the chat with
queued styling, and the running turn drains it when it finishes. That is the
behaviour a user expects when they type into a busy session, and it is what the
composer's own send button does.

The bridge never used it because the bridge was written for the CLI path, where
callers talk to *idle* sessions. `session_control` is different: it targets
whatever session the user names, which is very often the one already working.

## The fix

Route through the same two-path decision the composer makes, instead of
unconditionally calling `send()`.

Usage: python3 fix_send_when_busy.py <repo-root>
"""
import sys
from pathlib import Path

root = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
BRIDGE = root / "src" / "ios" / "NativeOffloads" / "SessionsOffloadBridge.swift"

if not BRIDGE.exists():
    sys.exit(f"FATAL: missing {BRIDGE}")

MARKER = "[T-send-when-busy]"
if MARKER in BRIDGE.read_text():
    print("[ALREADY] send-when-busy patch present")
    sys.exit(0)

print("[GATE   ] patching sendPrompt to handle busy sessions")

OLD = '''            // Send.
            vm.inputText = prompt
            vm.send()'''

NEW = '''            // [T-send-when-busy] Send, or QUEUE if this session is already
            // working.
            //
            // `send()` opens with
            //     guard ... , !isProcessing else { return }
            // so calling it on a busy session returns immediately and leaves
            // the text in `inputText` — the message visibly sits in the
            // composer and is never sent, which is the bug this fixes.
            //
            // `enqueuePrompt()` is the app's own answer to that case: it
            // appends to `promptQueue`, renders the text in the chat with
            // queued styling, and the in-flight turn drains it on completion.
            // That is exactly what the composer's send button does when the
            // session is busy, so a remote send now behaves like a local one
            // instead of silently no-oping.
            //
            // `enqueuePrompt()` also guards on `isProcessing` being TRUE, so the
            // two paths are mutually exclusive by construction — there is no
            // window where both could run.
            vm.inputText = prompt
            if vm.isProcessing {
                vm.enqueuePrompt()
                outputWasQueued = true
            } else {
                vm.send()
            }'''

s = BRIDGE.read_text()
if s.count(OLD) != 1:
    sys.exit(f"FATAL: sendPrompt anchor found {s.count(OLD)}x (expected 1)")
s = s.replace(OLD, NEW, 1)

# Declare the flag next to the other locals.
OLD2 = '''        let sem = DispatchSemaphore(value: 0)
        var output: [String: Any] = [:]

        Task { @MainActor in
            // Resolve / create the VM.
            let vm: AIChatViewModel
            let isNew: Bool'''
NEW2 = '''        let sem = DispatchSemaphore(value: 0)
        var output: [String: Any] = [:]
        // [T-send-when-busy] Set when the session was busy and the prompt was
        // queued rather than sent; reported back so a caller can tell the two
        // apart instead of assuming the message went out.
        var outputWasQueued = false

        Task { @MainActor in
            // Resolve / create the VM.
            let vm: AIChatViewModel
            let isNew: Bool'''
if s.count(OLD2) != 1:
    sys.exit(f"FATAL: sendPrompt locals anchor found {s.count(OLD2)}x (expected 1)")
s = s.replace(OLD2, NEW2, 1)

# Surface it in the reply.
OLD3 = '''            output = [
                "ok": true,
                "action": "send",
                "session_id": sid,
                "is_new_session": isNew,
                "model_name": modelName,
                "status": "Running",
                "prompt": prompt,
                "response_text": "",
            ]'''
NEW3 = '''            output = [
                "ok": true,
                "action": "send",
                "session_id": sid,
                "is_new_session": isNew,
                "model_name": modelName,
                // [T-send-when-busy] "Queued" when the session was busy: the
                // message is in the queue and will be injected when the current
                // turn finishes, rather than being lost.
                "status": outputWasQueued ? "Queued" : "Running",
                "queued": outputWasQueued,
                "prompt": prompt,
                "response_text": "",
            ]'''
if s.count(OLD3) != 1:
    sys.exit(f"FATAL: sendPrompt reply anchor found {s.count(OLD3)}x (expected 1)")
s = s.replace(OLD3, NEW3, 1)

BRIDGE.write_text(s)
print("[APPLY  ] sendPrompt: queue instead of no-op when the session is busy")
print(f"[fix] send-when-busy applied")

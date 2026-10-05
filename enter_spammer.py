#!/usr/bin/env python3
"""
Enter Spammer for macOS
Simulates rapid Enter (Return) keystrokes using macOS CoreGraphics (low latency, zero external pip dependencies).
"""

import sys
import os
import time
import signal
import argparse
import subprocess
import ctypes
from ctypes import c_void_p, c_uint16, c_uint32, c_bool, c_long

# macOS Key Codes
KEY_RETURN = 36         # Standard Return / Enter
KEY_NUMPAD_ENTER = 76   # Numeric Keypad Enter
kCGHIDEventTap = 0


class MacOSCoreGraphics:
    """Wrapper around macOS CoreGraphics and ApplicationServices for synthetic keyboard events."""

    def __init__(self):
        try:
            self.cg = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
            self.cf = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
            self.app_services = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
        except Exception as e:
            print(f"[Error] Failed to load macOS frameworks: {e}", file=sys.stderr)
            sys.exit(1)

        # Setup CGEventCreateKeyboardEvent
        self.cg.CGEventCreateKeyboardEvent.restype = c_void_p
        self.cg.CGEventCreateKeyboardEvent.argtypes = [c_void_p, c_uint16, c_bool]

        # Setup CGEventPost
        self.cg.CGEventPost.restype = None
        self.cg.CGEventPost.argtypes = [c_uint32, c_void_p]

        # Setup CFRelease
        self.cf.CFRelease.restype = None
        self.cf.CFRelease.argtypes = [c_void_p]

        # Setup AXIsProcessTrusted
        self.app_services.AXIsProcessTrusted.restype = c_bool
        self.app_services.AXIsProcessTrusted.argtypes = []

        # Setup AXIsProcessTrustedWithOptions
        self.app_services.AXIsProcessTrustedWithOptions.restype = c_bool
        self.app_services.AXIsProcessTrustedWithOptions.argtypes = [c_void_p]

        self.cf.CFDictionaryCreate.restype = c_void_p
        self.cf.CFDictionaryCreate.argtypes = [
            c_void_p,
            ctypes.POINTER(c_void_p),
            ctypes.POINTER(c_void_p),
            c_long,
            c_void_p,
            c_void_p,
        ]

    def is_accessibility_trusted(self) -> bool:
        """Check if current process has macOS Accessibility permissions."""
        return bool(self.app_services.AXIsProcessTrusted())

    def request_accessibility_permission(self):
        """Prompt macOS system dialog to request accessibility permission."""
        try:
            prompt_key = c_void_p.in_dll(self.app_services, "kAXTrustedCheckOptionPrompt").value
            k_cf_boolean_true = c_void_p.in_dll(self.cf, "kCFBooleanTrue").value
            keys = (c_void_p * 1)(prompt_key)
            values = (c_void_p * 1)(k_cf_boolean_true)
            options = self.cf.CFDictionaryCreate(None, keys, values, 1, None, None)
            self.app_services.AXIsProcessTrustedWithOptions(options)
            if options:
                self.cf.CFRelease(options)
        except Exception:
            pass

    def send_enter(self, key_code: int = KEY_RETURN, hold_time: float = 0.005):
        """Send a single key down and key up event."""
        down_evt = self.cg.CGEventCreateKeyboardEvent(None, key_code, True)
        up_evt = self.cg.CGEventCreateKeyboardEvent(None, key_code, False)
        try:
            self.cg.CGEventPost(kCGHIDEventTap, down_evt)
            if hold_time > 0:
                time.sleep(hold_time)
            self.cg.CGEventPost(kCGHIDEventTap, up_evt)
        finally:
            if down_evt:
                self.cf.CFRelease(down_evt)
            if up_evt:
                self.cf.CFRelease(up_evt)


def check_and_guide_permissions(cg_helper: MacOSCoreGraphics):
    """Check permissions and display guidance if needed."""
    if cg_helper.is_accessibility_trusted():
        return

    print("\n" + "=" * 60)
    print("⚠️  MACOS ACCESSIBILITY PERMISSION REQUIRED")
    print("=" * 60)
    print("macOS security requires permission to simulate keystrokes.")
    print("Without it, the key events will be silently blocked by macOS.")
    print("\nTo enable:")
    print("  1. Open System Settings -> Privacy & Security -> Accessibility")
    print("  2. Enable the toggle for your Terminal app (e.g. Terminal, iTerm, VS Code, Cursor)")
    print("=" * 60 + "\n")

    # Ask macOS to show the prompt or open settings
    cg_helper.request_accessibility_permission()

    # Try opening the settings pane directly
    try:
        subprocess.run(
            ["open", "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
    except Exception:
        pass


def run_countdown(seconds: int):
    """Visual countdown before spamming begins."""
    if seconds <= 0:
        return
    print("\n⏱️  Prepare your target window!")
    for remaining in range(seconds, 0, -1):
        print(f"   Starting in {remaining}... (switch focus now)", end="\r", flush=True)
        time.sleep(1.0)
    print("   🚀 SPAMMING ENTER NOW!                     \n")


def main():
    parser = argparse.ArgumentParser(
        description="Fast & lightweight Enter key spammer for macOS (using CoreGraphics).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  %(prog)s                    # Spam Enter at 10 presses/sec until Ctrl+C (3s countdown)
  %(prog)s -r 20              # Spam Enter at 20 presses/sec
  %(prog)s -d 0.5             # Delay 0.5s between Enters (2 presses/sec)
  %(prog)s -n 50              # Press Enter exactly 50 times
  %(prog)s -s 5               # 5 seconds countdown before starting
  %(prog)s --check-perms      # Test if macOS Accessibility permission is granted
        """
    )
    parser.add_argument("-d", "--delay", type=float, default=None,
                        help="Delay in seconds between keystrokes (default: 0.1s = 10 presses/sec)")
    parser.add_argument("-r", "--rate", type=float, default=None,
                        help="Rate in presses per second (e.g. 10 = 10 presses/sec). Overrides --delay.")
    parser.add_argument("-n", "--count", type=int, default=0,
                        help="Number of Enters to send (default: 0 = infinite until stopped with Ctrl+C)")
    parser.add_argument("-s", "--start-delay", type=int, default=3,
                        help="Initial countdown delay in seconds to allow switching focus (default: 3)")
    parser.add_argument("--keypad", action="store_true",
                        help="Use numeric keypad Enter (key code 76) instead of standard Return (36)")
    parser.add_argument("--hold", type=float, default=0.005,
                        help="Keystroke down-time in seconds before release (default: 0.005s)")
    parser.add_argument("--max-duration", type=float, default=0,
                        help="Maximum duration in seconds before auto-stopping (default: 0 = no time limit)")
    parser.add_argument("--check-perms", action="store_true",
                        help="Check accessibility permission status and exit")

    args = parser.parse_args()

    cg_helper = MacOSCoreGraphics()

    if args.check_perms:
        trusted = cg_helper.is_accessibility_trusted()
        if trusted:
            print("✅ macOS Accessibility permission is GRANTED.")
            sys.exit(0)
        else:
            print("❌ macOS Accessibility permission is NOT granted.")
            check_and_guide_permissions(cg_helper)
            sys.exit(1)

    # Determine delay
    if args.rate is not None:
        if args.rate <= 0:
            print("[Error] Rate must be greater than 0.", file=sys.stderr)
            sys.exit(1)
        delay = 1.0 / args.rate
    elif args.delay is not None:
        if args.delay < 0:
            print("[Error] Delay must be non-negative.", file=sys.stderr)
            sys.exit(1)
        delay = args.delay
    else:
        delay = 0.1  # default 10 per second

    target_rate = 1.0 / delay if delay > 0 else float("inf")
    key_code = KEY_NUMPAD_ENTER if args.keypad else KEY_RETURN
    key_name = "Numpad Enter" if args.keypad else "Return / Enter"

    print("=" * 50)
    print("⌨️   macOS ENTER KEY SPAMMER")
    print("=" * 50)
    print(f"  Key:           {key_name} (code {key_code})")
    print(f"  Interval:      {delay:.4f}s ({target_rate:.1f} presses/sec)")
    print(f"  Count:         {'Infinite (Ctrl+C to stop)' if args.count <= 0 else f'{args.count} presses'}")
    print(f"  Countdown:     {args.start_delay}s")
    if args.max_duration > 0:
        print(f"  Max Duration:  {args.max_duration}s")
    print("=" * 50)

    # Permission check
    if not cg_helper.is_accessibility_trusted():
        check_and_guide_permissions(cg_helper)
        try:
            input("Press [Enter] to continue anyway (or Ctrl+C to abort)... ")
        except KeyboardInterrupt:
            print("\nAborted.")
            sys.exit(0)

    # Countdown
    run_countdown(args.start_delay)

    # Run Loop
    count = 0
    start_time = time.time()
    interrupted = False

    def handle_sigint(signum, frame):
        nonlocal interrupted
        interrupted = True

    prev_handler = signal.signal(signal.SIGINT, handle_sigint)

    try:
        while not interrupted:
            cg_helper.send_enter(key_code=key_code, hold_time=args.hold)
            count += 1
            now = time.time()
            elapsed = now - start_time
            curr_rate = count / elapsed if elapsed > 0 else 0

            # Live status update
            target_str = f"/{args.count}" if args.count > 0 else ""
            print(f"\r  ⚡ Presses: {count}{target_str} | Elapsed: {elapsed:5.1f}s | Speed: {curr_rate:5.1f}/s  [Ctrl+C to stop]", end="", flush=True)

            if args.count > 0 and count >= args.count:
                break

            if args.max_duration > 0 and elapsed >= args.max_duration:
                print(f"\n[Info] Max duration reached ({args.max_duration}s).")
                break

            if delay > 0:
                time.sleep(delay)

    finally:
        signal.signal(signal.SIGINT, prev_handler)
        total_time = time.time() - start_time
        avg_rate = count / total_time if total_time > 0 else 0

        print("\n\n" + "=" * 50)
        print("🛑 SPAMMER STOPPED")
        print("=" * 50)
        print(f"  Total Enters sent: {count}")
        print(f"  Total time:        {total_time:.2f} seconds")
        print(f"  Average rate:      {avg_rate:.2f} presses/second")
        print("=" * 50)


if __name__ == "__main__":
    main()

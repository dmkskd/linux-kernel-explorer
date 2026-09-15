"""System clipboard integration for kexplore.

Supports:
1. Terminal OSC 52 escape sequences (works across SSH, tmux, and modern terminal emulators).
2. Native Textual clipboard driver (app.copy_to_clipboard).
3. Local clipboard CLI utilities (pbcopy, wl-copy, xclip, xsel).
"""

from __future__ import annotations

import base64
import os
import shutil
import subprocess
import sys


def copy_to_system_clipboard(text: str, app: object | None = None) -> bool:
    """Copy text to the system clipboard.

    Uses Textual's built-in clipboard support, emits OSC 52 escape sequences
    directly (with tmux/screen passthrough when applicable), and falls back
    to local clipboard utilities (pbcopy, wl-copy, xclip, xsel) if present.
    """
    if not text:
        return False

    success = False

    # Store on app for testability and intra-app access
    if app is not None:
        try:
            setattr(app, "_last_copied", text)
        except Exception:
            pass

    # 1. Textual App built-in method
    if app is not None and hasattr(app, "copy_to_clipboard"):
        try:
            app.copy_to_clipboard(text)
            success = True
        except Exception:
            pass

    # 2. OSC 52 escape sequences across terminal
    try:
        b64_bytes = base64.b64encode(text.encode("utf-8"))
        b64_str = b64_bytes.decode("ascii")

        term = os.environ.get("TERM", "")
        in_tmux = "TMUX" in os.environ or term.startswith("tmux")
        in_screen = term.startswith("screen")

        # BEL-terminated OSC 52
        osc_bel = f"\x1b]52;c;{b64_str}\x07"

        if in_tmux:
            # tmux DCS passthrough: ESC P tmux; [ESC ESC ... \x07] ESC \
            tmux_payload = osc_bel.replace("\x1b", "\x1b\x1b")
            sequences = [f"\x1bPtmux;{tmux_payload}\x1b\\"]
        elif in_screen:
            sequences = [f"\x1bP{osc_bel}\x1b\\"]
        else:
            # Both BEL and ST (String Terminator: ESC \) for maximum terminal compatibility
            osc_st = f"\x1b]52;c;{b64_str}\x1b\\"
            sequences = [osc_bel, osc_st]

        driver = getattr(app, "_driver", None) if app is not None else None
        for seq in sequences:
            if driver is not None and hasattr(driver, "write"):
                driver.write(seq)
                if hasattr(driver, "flush"):
                    driver.flush()
                success = True
            elif sys.stdout and sys.stdout.isatty():
                sys.stdout.write(seq)
                sys.stdout.flush()
                success = True
    except Exception:
        pass

    # 3. Local CLI utilities (when running on host or desktop environment)
    for cmd in (
        ["pbcopy"] if shutil.which("pbcopy") else None,
        ["wl-copy"] if shutil.which("wl-copy") else None,
        ["xclip", "-selection", "clipboard"] if shutil.which("xclip") else None,
        ["xsel", "--clipboard", "--input"] if shutil.which("xsel") else None,
    ):
        if not cmd:
            continue
        try:
            subprocess.run(
                cmd,
                input=text.encode("utf-8"),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=True,
                timeout=0.3,
            )
            success = True
            break
        except Exception:
            continue

    return success

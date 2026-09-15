#!/usr/bin/env python3
"""Scripted asciinema driver for kexplore.

Automates an interactive terminal session of kexplore by spawning it inside
a pseudo-terminal (PTY), sending timed keystrokes to navigate structures and
operations, displaying the live UI to the terminal, and recording the session
in asciicast v3 format.
"""

from __future__ import annotations

import fcntl
import json
import os
import pty
import select
import shutil
import struct
import subprocess
import sys
import termios
import time
import tty
from pathlib import Path


def get_terminal_dimensions() -> tuple[int, int]:
    """Detect current terminal dimensions, falling back to 150x38."""
    try:
        ts = shutil.get_terminal_size((150, 38))
        return max(80, ts.columns), max(24, ts.lines)
    except Exception:
        return 150, 38


def record(
    output_path: str,
    actions: list[tuple[float, bytes, str]],
    extra_args: list[str] | None = None,
    cols: int | None = None,
    rows: int | None = None,
    live_display: bool = True,
) -> None:
    """Run kexplore in a PTY, execute scripted actions, and save as asciicast v3."""
    repo_root = str(Path(__file__).resolve().parent.parent)

    if cols is None or rows is None:
        detected_cols, detected_rows = get_terminal_dimensions()
        cols = cols or detected_cols
        rows = rows or detected_rows

    cmd = [
        "limactl",
        "shell",
        "kernel-lab",
        "sudo",
        "env",
        "PYTHONDONTWRITEBYTECODE=1",
        f"PYTHONPATH={repo_root}",
        "DEBUGINFOD_URLS=https://debuginfod.fedoraproject.org/",
        "KEXPLORE_OFFLINE=",
        "TERM=xterm-256color",
        "COLORTERM=truecolor",
        "python3",
        "-m",
        "kexplore",
    ]
    if extra_args:
        cmd.extend(extra_args)

    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    start_wall = time.time()
    last_event_time = start_wall

    header = {
        "version": 3,
        "term": {
            "cols": cols,
            "rows": rows,
            "type": "xterm-256color",
        },
        "timestamp": int(start_wall),
        "command": "kexplore",
        "env": {"SHELL": "/bin/zsh", "TERM": "xterm-256color"},
        "title": "kexplore live kernel walkthrough",
    }

    cast_file = open(output_path, "w", encoding="utf-8")
    cast_file.write(json.dumps(header) + "\n")
    cast_file.flush()

    proc = subprocess.Popen(
        cmd,
        stdin=slave,
        stdout=slave,
        stderr=slave,
        close_fds=True,
    )
    os.close(slave)

    action_idx = 0
    last_action_time = start_wall

    # Put host stdin into raw mode so mouse tracking escapes do not echo to screen
    stdin_fd = sys.stdin.fileno() if sys.stdin.isatty() else None
    old_stdin_term = None
    if stdin_fd is not None:
        try:
            old_stdin_term = termios.tcgetattr(stdin_fd)
            tty.setraw(stdin_fd)
        except Exception:
            pass

    try:
        while proc.poll() is None:
            watch_read = [master]
            if stdin_fd is not None:
                watch_read.append(stdin_fd)

            r, _, _ = select.select(watch_read, [], [], 0.05)
            now = time.time()

            # Read from child PTY
            if master in r:
                try:
                    data = os.read(master, 4096)
                    if data:
                        delta = round(max(0.0, now - last_event_time), 4)
                        last_event_time = now
                        text = data.decode("utf-8", "replace")
                        event = [delta, "o", text]
                        cast_file.write(json.dumps(event) + "\n")
                        cast_file.flush()

                        if live_display:
                            sys.stdout.buffer.write(data)
                            sys.stdout.buffer.flush()
                except OSError:
                    break

            # Consume any mouse or keyboard input from host terminal to prevent echo
            if stdin_fd is not None and stdin_fd in r:
                try:
                    os.read(stdin_fd, 1024)
                except OSError:
                    pass

            # Execute scripted actions
            if action_idx < len(actions):
                delay, key_bytes, _desc = actions[action_idx]
                if now - last_action_time >= delay:
                    try:
                        os.write(master, key_bytes)
                    except OSError:
                        break
                    last_action_time = now
                    action_idx += 1
            else:
                if now - last_action_time > 3.0:
                    break

    finally:
        cast_file.close()
        if old_stdin_term is not None and stdin_fd is not None:
            try:
                termios.tcsetattr(stdin_fd, termios.TCSADRAIN, old_stdin_term)
            except Exception:
                pass

        try:
            os.close(master)
        except OSError:
            pass
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                proc.kill()

    if live_display:
        sys.stdout.write("\n\033[0m")
        sys.stdout.flush()

    print(f"\n[asciinema] Recording saved: {output_path}")
    print(f"[asciinema] Replay with: asciinema play {output_path}\n")


def build_tour_script(tour: str) -> list[tuple[float, bytes, str]]:
    t = tour.lower()
    if "mem" in t:
        return [
            # Startup: Step 1 (Dynamic Heap / mm_struct.start_brk) with TourBanner
            (4.0, b"n", "Advance to Step 2: Executable Code (Text Segment)"),
            (3.5, b"n", "Advance to Step 3: Read-write Data & BSS"),
            (3.5, b"n", "Advance to Step 4: Memory Mappings (mmap & shared libraries)"),
            (3.5, b"n", "Advance to Step 5: Main Thread User Stack (VM_GROWSDOWN)"),
            (3.5, b"p", "Step back with 'p' to Step 4"),
            (2.0, b"n", "Step forward with 'n' to Step 5"),
            (3.0, b"q", "Quit with 'q'"),
        ]
    if "sched" in t or "eevdf" in t:
        return [
            # Startup: Step 1 (CPU Runqueue & struct rq)
            (4.0, b"n", "Advance to Step 2: CFS Runqueue & Virtual Timeline"),
            (3.5, b"n", "Advance to Step 3: Current Running Task (rq.curr)"),
            (3.5, b"n", "Advance to Step 4: Scheduling Policy & Attributes"),
            (3.5, b"p", "Step back with 'p' to Step 3"),
            (2.0, b"n", "Step forward with 'n' to Step 4"),
            (3.0, b"q", "Quit with 'q'"),
        ]
    if "page" in t:
        return [
            # Startup: Step 1 (Page Global Directory / mm.pgd)
            (4.0, b"n", "Advance to Step 2: Virtual Memory Area Boundary"),
            (3.5, b"n", "Advance to Step 3: Resident Physical Pages"),
            (3.5, b"n", "Advance to Step 4: Memory Zone & NUMA Node"),
            (3.5, b"q", "Quit with 'q'"),
        ]
    if "life" in t or "clone" in t:
        return [
            # Startup: Step 1 (Process Descriptor & State)
            (4.0, b"n", "Advance to Step 2: Credentials & Security Context"),
            (3.5, b"n", "Advance to Step 3: Namespace Proxies (nsproxy)"),
            (3.5, b"n", "Advance to Step 4: File Descriptor Table (files_struct)"),
            (3.5, b"q", "Quit with 'q'"),
        ]
    # Default process architecture tour
    return [
        # Startup: Step 1 (Thread Group Leader)
        (4.0, b"n", "Advance to Step 2: Shared Address Space (mm_struct)"),
        (3.5, b"n", "Advance to Step 3: Thread Group Tasks"),
        (3.5, b"n", "Advance to Step 4: File Descriptors (files_struct)"),
        (3.5, b"p", "Step back with 'p' to Step 3"),
        (2.0, b"n", "Step forward with 'n' to Step 4"),
        (3.0, b"q", "Quit with 'q'"),
    ]


def build_scenario_script(scenario: str) -> list[tuple[float, bytes, str]]:
    if scenario in ("tutorials", "tours", "recordings", "tutorials_overview", "tours_overview"):
        return [
            # 1. Startup on landing page
            (4.0, b"v", "Cycle view to operations"),
            (1.0, b"v", "Cycle view to tutorials"),
            (1.2, b"\t", "Focus NavTree from Tabs"),
            (0.5, b"\x1b[B", "Down to process category"),
            (0.4, b"\x1b[B", "Down to Multi-threaded Process Architecture"),
            (0.4, b"\x1b[B", "Down to memory category"),
            (0.4, b"\x1b[B", "Down to User Memory Types & VMAs (Deep Linux)"),
            (1.2, b"\r", "Enter: Start live guided tutorial driver"),
            (3.5, b"n", "Advance to Step 2 with 'n'"),
            (3.0, b"n", "Advance to Step 3 with 'n'"),
            (3.0, b"\x7f", "Step backward with Backspace"),
            (2.5, b" ", "Advance to Step 3 with Space"),
            (2.5, b"\x1b", "Exit tutorial with Escape"),
            (2.0, b"q", "Quit with 'q'"),
        ]

    # Default VMA direct navigation
    return [
        (3.5, b"\t", "Focus NavTree from Tabs"),
        (0.3, b"\x1b[B", "Down to system"),
        (0.2, b"\x1b[B", "Down to overview"),
        (0.2, b"\x1b[B", "Down to scheduler"),
        (0.2, b"\x1b[B", "Down to memory"),
        (0.2, b"\x1b[B", "Down to process"),
        (0.2, b"\x1b[B", "Down to init (pid 1)"),
        (0.8, b"\r", "Open init (pid 1)"),
        (1.2, b"\t", "Focus fields table"),
        (0.8, b"\r", "Follow into task_struct"),
        (1.5, b"\x1b[B", "Down to user"),
        (0.2, b"\x1b[B", "Down to group"),
        (0.2, b"\x1b[B", "Down to user_ns"),
        (0.2, b"\x1b[B", "Down to threads"),
        (0.2, b"\x1b[B", "Down to mm"),
        (0.2, b"\x1b[B", "Down to VMAs"),
        (1.0, b"\r", "Open VMAs (User Memory Types)"),
        (5.0, b"\x7f", "Backspace to return to task_struct"),
        (2.5, b"q", "Quit with 'q'"),
    ]


def main() -> None:
    tutorial: str | None = None
    scenario = "tutorials"
    output = "/tmp/scripted_demo.cast"

    args = sys.argv[1:]
    if args and not args[0].startswith("-"):
        output = args.pop(0)

    for i, arg in enumerate(args):
        if arg in ("--tutorial", "--tour") and i + 1 < len(args):
            tutorial = args[i + 1]
        elif arg.startswith(("--tutorial=", "--tour=")):
            tutorial = arg.split("=", 1)[1]
        elif arg == "--scenario" and i + 1 < len(args):
            scenario = args[i + 1]
        elif arg.startswith("--scenario="):
            scenario = arg.split("=", 1)[1]

    extra_args: list[str] = []
    if tutorial:
        extra_args = ["--tutorial", tutorial]
        script = build_tour_script(tutorial)
    else:
        script = build_scenario_script(scenario)

    record(output, script, extra_args=extra_args, live_display=sys.stdout.isatty())


if __name__ == "__main__":
    main()

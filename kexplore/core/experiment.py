"""Run a controlled program, then inspect the kernel state it produced.

Observation alone cannot attribute cause. A pthread_create passes five CLONE_*
flags at once, so watching an arbitrary process shows five structures differing
and no way to say which flag did what. The only way to isolate a flag is to
pass it ourselves.

An experiment therefore compiles and runs a small helper, holds it alive while
drgn reads the tasks it created, and then kills it. Nothing survives the call.

``perf_stat`` is the other half: run any command under perf and get its
counters back as numbers. Note that hardware counters are unavailable inside
this VM -- the hypervisor does not pass through the PMU, so ``cycles`` and
``instructions`` report "<not supported>". Software events (task-clock,
context-switches, page-faults, minor-faults, major-faults) and tracepoints do
work, and those are the ones worth counting for kernel behaviour anyway.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Running:
    """A helper process that is alive and holding kernel state in place."""

    process: subprocess.Popen
    lines: list[str] = field(default_factory=list)
    error: str = ""

    def stop(self) -> None:
        if self.process.poll() is None:
            try:
                # The helper kills its children on SIGTERM; killing the group
                # catches any it created with CLONE_THREAD.
                os.killpg(os.getpgid(self.process.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()


def build(source: Path, binary: Path) -> str:
    """Compile the helper if the binary is missing or older than the source."""
    if not source.exists():
        return f"missing {source}"
    if binary.exists() and binary.stat().st_mtime >= source.stat().st_mtime:
        return ""
    try:
        result = subprocess.run(
            ["gcc", "-O2", "-Wall", "-o", str(binary), str(source)],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except OSError as exc:
        return f"could not run gcc: {exc}"
    if result.returncode:
        return result.stderr.strip().splitlines()[-1] if result.stderr else "compile failed"
    return ""


def start(binary: Path, args: list[str], ready: str, timeout: float = 30.0) -> Running:
    """Start the helper and read its output until ``ready`` appears.

    The helper prints what it created and then sleeps, so the state it set up
    is still there when drgn looks.
    """
    try:
        process = subprocess.Popen(
            [str(binary), *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
    except OSError as exc:
        return Running(process=None, error=f"could not start {binary}: {exc}")  # type: ignore[arg-type]

    running = Running(process=process)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        line = process.stdout.readline() if process.stdout else ""
        if not line:
            if process.poll() is not None:
                running.error = "helper exited before it was ready"
                return running
            continue
        running.lines.append(line.rstrip())
        if ready in line:
            return running
    running.error = f"helper did not print {ready!r} within {timeout:.0f}s"
    running.stop()
    return running


# Events that work without a PMU. Hardware counters are not available in a VM.
SOFTWARE_EVENTS = (
    "task-clock",
    "context-switches",
    "page-faults",
    "minor-faults",
    "major-faults",
)


@dataclass
class Counters:
    """Parsed output of one perf stat run."""

    values: dict[str, float] = field(default_factory=dict)
    unsupported: list[str] = field(default_factory=list)
    elapsed_seconds: float = 0.0
    error: str = ""

    def get(self, event: str) -> float | None:
        return self.values.get(event)


def perf_stat(
    argv: list[str],
    events: tuple[str, ...] = SOFTWARE_EVENTS,
    repeat: int = 1,
    timeout: float = 120.0,
) -> Counters:
    """Run a command under perf stat and return its counters.

    Uses CSV output (``-x,``) rather than the human format, which changes
    between perf versions and is padded for display.
    """
    if not Path("/usr/bin/perf").exists() and not _which("perf"):
        return Counters(error="perf is not installed (dnf install perf)")

    command = ["perf", "stat", "-x,", "-e", ",".join(events)]
    if repeat > 1:
        command += ["-r", str(repeat)]
    command += ["--", *argv]

    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=timeout
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return Counters(error=f"perf failed: {exc}")

    counters = Counters()
    for line in result.stderr.splitlines():
        parts = line.split(",")
        if len(parts) < 3:
            continue
        raw, event = parts[0], parts[2]
        if raw.startswith("<"):
            counters.unsupported.append(event)
            continue
        try:
            counters.values[event] = float(raw)
        except ValueError:
            continue

    if not counters.values and result.returncode:
        tail = result.stderr.strip().splitlines()
        counters.error = tail[-1] if tail else f"perf exited {result.returncode}"
    return counters


def _which(name: str) -> str | None:
    import shutil

    return shutil.which(name)

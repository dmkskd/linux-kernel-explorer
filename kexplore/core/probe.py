"""Run short foreground measurements and parse their output.

drgn reads state: what the kernel looks like right now. It cannot answer "how
long did tasks wait to be scheduled in the last second", because that is a
property of events over an interval, not of memory at an instant.

A Probe fills that gap by running a real tracer for a couple of seconds and
returning the result. Deliberately synchronous and short-lived: nothing is
accumulated in the background, nothing keeps running after the answer is on
screen.
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

# Histogram row: "[4, 8)    97 |@@@@@@@@@|" or "[1]  2 |@|"
_BUCKET = re.compile(r"^(\[[^\]]+[\])])\s+(\d+)\s*\|(.*)\|\s*$")
# Map header: "@runq_wait_us:" or a scalar/keyed entry "@switches[3]: 93"
_MAP_HEADER = re.compile(r"^@([\w]*):\s*$")
_MAP_ENTRY = re.compile(r"^@([\w]*)(?:\[(.*)\])?:\s*(.+)$")


@dataclass
class Section:
    """One named result from a probe: a histogram or a set of counts."""

    name: str
    rows: list[tuple[str, str, str]] = field(default_factory=list)  # label, value, bar


@dataclass
class ProbeResult:
    sections: list[Section] = field(default_factory=list)
    error: str = ""
    raw: str = ""
    # True when the traced command had to be killed because it does not exit on
    # its own. The maps still hold what it did in the seconds it ran.
    stopped: bool = False


def tool_available(name: str) -> bool:
    return shutil.which(name) is not None


def run_bpftrace(script: str, duration: int) -> ProbeResult:
    """Run a bpftrace program for ``duration`` seconds and parse its output.

    The script must terminate itself; the timeout is only a backstop for a
    program that fails to. bpftrace prints all maps at exit, which is what gets
    parsed.
    """
    if not tool_available("bpftrace"):
        return ProbeResult(error="bpftrace is not installed (dnf install bpftrace)")

    program = script.replace("{duration}", str(duration))
    try:
        completed = subprocess.run(
            ["bpftrace", "-e", program],
            capture_output=True,
            text=True,
            timeout=duration + 25,
        )
    except subprocess.TimeoutExpired:
        return ProbeResult(error=f"bpftrace did not exit within {duration + 25}s")
    except OSError as exc:
        return ProbeResult(error=f"could not run bpftrace: {exc}")

    output = completed.stdout
    if not output.strip():
        detail = completed.stderr.strip().splitlines()
        hint = detail[-1] if detail else "no events recorded in the interval"
        return ProbeResult(error=hint, raw=completed.stderr)

    return ProbeResult(sections=parse_bpftrace(output), raw=output)


def parse_bpftrace(text: str) -> list[Section]:
    """Turn bpftrace's map output into sections of labelled rows.

    Handles both shapes it emits: a histogram (a ``@name:`` header followed by
    bucket lines) and keyed counts (``@name[key]: value``).
    """
    sections: list[Section] = []
    current: Section | None = None

    for line in text.splitlines():
        if not line.strip():
            continue

        bucket = _BUCKET.match(line.strip())
        if bucket and current is not None:
            label, count, bar = bucket.groups()
            current.rows.append((label, count, bar.rstrip()))
            continue

        header = _MAP_HEADER.match(line.strip())
        if header:
            current = Section(header.group(1))
            sections.append(current)
            continue

        entry = _MAP_ENTRY.match(line.strip())
        if entry:
            name, key, value = entry.groups()
            # A keyed count belongs to its own named section, grouped by name.
            section = next((s for s in sections if s.name == name), None)
            if section is None:
                section = Section(name)
                sections.append(section)
            section.rows.append((key if key else name, value.strip(), ""))
            current = section
            continue

    # Counts come out of bpftrace in hash order; sort them by value descending.
    for section in sections:
        if all(row[2] == "" for row in section.rows) and len(section.rows) > 1:
            section.rows.sort(key=lambda r: _as_int(r[1]), reverse=True)
    return sections


def _as_int(value: str) -> int:
    try:
        return int(value)
    except ValueError:
        return 0


def trace_command(
    script: str, command: str, seconds: int = 5, attach: int = 40
) -> ProbeResult:
    """Trace one run of ``command`` and return the maps bpftrace printed.

    ``run_bpftrace`` watches whatever the machine happens to be doing for a few
    seconds, which is right for rates and latencies. This traces a single run of
    one program instead, so every event recorded belongs to that run.

    bpftrace's own ``-c`` cannot be used for it. It waits for the command to
    exit, and several commands in the catalog never do: ``vmstat -n 1`` prints a
    line a second forever, and the frame would sit on a placeholder until a
    timeout. So the command is started here instead, once bpftrace says it has
    attached, in its own process group, and the whole group is killed after
    ``seconds``. Killing the group and not the child is what stops ``vmstat``
    surviving as an orphan when the shell around it is killed.
    """
    if not tool_available("bpftrace"):
        return ProbeResult(error="bpftrace is not installed (dnf install bpftrace)")

    with (
        tempfile.NamedTemporaryFile("r", suffix=".trace") as sink,
        tempfile.NamedTemporaryFile("w", suffix=".sh") as runner,
    ):
        runner.write(f"{command}\n")
        runner.flush()
        try:
            tracer = subprocess.Popen(
                ["bpftrace", "-o", sink.name, "-e", script],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
        except OSError as exc:
            return ProbeResult(error=f"could not run bpftrace: {exc}")

        noise: list[str] = []
        ready = threading.Event()

        def watch() -> None:
            # Drains the pipe as well as watching it: a full stderr would stop
            # bpftrace mid-trace.
            for line in tracer.stderr:  # type: ignore[union-attr]
                noise.append(line)
                if "Attached" in line:
                    ready.set()

        threading.Thread(target=watch, daemon=True).start()

        deadline = time.monotonic() + attach
        while not ready.is_set() and tracer.poll() is None:
            if time.monotonic() > deadline:
                break
            time.sleep(0.05)

        if not ready.is_set():
            tracer.kill()
            tracer.wait(timeout=5)
            detail = [line.strip() for line in noise if line.strip()]
            return ProbeResult(
                error=detail[-1] if detail else "bpftrace did not attach",
                raw="".join(noise),
            )

        stopped = _run_briefly(runner.name, seconds)
        tracer.send_signal(signal.SIGINT)
        try:
            tracer.wait(timeout=20)
        except subprocess.TimeoutExpired:
            tracer.kill()
            tracer.wait(timeout=5)

        output = Path(sink.name).read_text(errors="replace")

    if "@" not in output:
        detail = [line.strip() for line in noise if line.strip()]
        hint = detail[-1] if detail else f"{command} triggered none of the probes"
        return ProbeResult(error=hint, raw="".join(noise))

    return ProbeResult(
        sections=parse_bpftrace(output), raw=output, stopped=stopped
    )


def _run_briefly(path: str, seconds: int) -> bool:
    """Run a shell script, killing its whole process group after ``seconds``.

    True if it had to be killed, which is how a command that never exits on its
    own is reported rather than waited on.
    """
    child = subprocess.Popen(
        ["/bin/sh", path],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        child.wait(timeout=seconds)
        return False
    except subprocess.TimeoutExpired:
        for sig in (signal.SIGINT, signal.SIGKILL):
            try:
                os.killpg(os.getpgid(child.pid), sig)
            except (ProcessLookupError, PermissionError):
                break
            try:
                child.wait(timeout=2)
                break
            except subprocess.TimeoutExpired:
                continue
        return True


def parse_stacks(text: str) -> dict[str, list[tuple[list[str], int]]]:
    """Pull bpftrace's multi-line stack maps out of raw output.

    A stack key spans several lines::

        @where[
                do_task_stat+0
                proc_single_show+100
        ]: 157

    ``parse_bpftrace`` reads one line per row and cannot hold a key like that,
    so stacks are parsed separately rather than by making the row parser
    stateful for the one case that needs it. Frames keep their ``+offset``
    suffix: it is what distinguishes a call site from the function's entry.
    """
    stacks: dict[str, list[tuple[list[str], int]]] = {}
    name = ""
    frames: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not name:
            if stripped.startswith("@") and stripped.endswith("["):
                name = stripped[1:-1]
                frames = []
            continue
        if stripped.startswith("]:"):
            count = _as_int(stripped[2:].strip())
            stacks.setdefault(name, []).append((frames, count))
            name = ""
            continue
        if stripped:
            frames.append(stripped)
    return stacks

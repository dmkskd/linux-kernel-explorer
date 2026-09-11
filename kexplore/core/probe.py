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
            check=False,
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


# The caller uses this predicate in every event probe. The map follows thread
# IDs across fork/clone and removes them at exit, avoiding comm collisions and
# stale membership when the kernel reuses an ID.
TRACE_FILTER = "@trace_tasks[tid]"


def _tracking_script(root_pid: int) -> str:
    return f"""
BEGIN {{ @trace_tasks[(uint64){root_pid}] = 1; }}
tracepoint:sched:sched_process_fork /@trace_tasks[(uint64)args->parent_pid]/ {{
    @trace_tasks[(uint64)args->child_pid] = 1;
}}
tracepoint:sched:sched_process_exec /@trace_tasks[(uint64)args->old_pid]/ {{
    delete(@trace_tasks[(uint64)args->old_pid]);
    @trace_tasks[tid] = 1;
}}
tracepoint:sched:sched_process_exit /@trace_tasks[tid]/ {{
    delete(@trace_tasks[tid]);
}}
"""


def _start_stopped(path: str) -> subprocess.Popen:
    """Obtain a PID before attaching probes without executing the command."""
    child = subprocess.Popen(
        ["/bin/sh", "-c", 'kill -STOP $$; exec /bin/sh "$1"', "trace-runner", path],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            pid, status = os.waitpid(child.pid, os.WUNTRACED | os.WNOHANG)
            if pid:
                if os.WIFSTOPPED(status):
                    return child
                child.returncode = os.waitstatus_to_exitcode(status)
                raise OSError("command launcher exited before stopping")
            time.sleep(0.01)
        raise OSError("command launcher did not stop within 5s")
    except BaseException:
        _kill_group(child)
        raise


def _kill_group(child: subprocess.Popen) -> None:
    try:
        os.killpg(child.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    child.wait(timeout=5)


def trace_command(
    script: str, command: str, seconds: int = 5, attach: int = 40
) -> ProbeResult:
    """Run a command with probes attached to its stopped launcher and descendants.

    Event probes must use TRACE_FILTER. BEGIN seeds membership before attachment;
    the launcher resumes only after bpftrace reports Attached, not at BEGIN.
    The process group is cleaned up on success, timeout, and attachment failure.
    """
    if not tool_available("bpftrace"):
        return ProbeResult(error="bpftrace is not installed (dnf install bpftrace)")

    with (
        tempfile.NamedTemporaryFile("r", suffix=".trace") as sink,
        tempfile.NamedTemporaryFile("w", suffix=".sh") as runner,
    ):
        runner.write(f"{command}\n")
        runner.flush()
        child = None
        tracer = None
        watcher = None
        noise: list[str] = []
        ready = threading.Event()
        try:
            child = _start_stopped(runner.name)
            maps = sorted({"trace_tasks"} | set(re.findall(r"@(trace_\w+)", script)))
            cleanup = "END { " + " ".join(f"clear(@{name});" for name in maps) + " }"
            tracer = subprocess.Popen(
                [
                    "bpftrace",
                    "-o",
                    sink.name,
                    "-e",
                    _tracking_script(child.pid) + script + cleanup,
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )

            def watch() -> None:
                for line in tracer.stderr:
                    noise.append(line)
                    if "Attached" in line:
                        ready.set()

            watcher = threading.Thread(target=watch, daemon=True)
            watcher.start()
            deadline = time.monotonic() + attach
            while not ready.is_set() and tracer.poll() is None:
                if time.monotonic() > deadline:
                    break
                time.sleep(0.05)
            if not ready.is_set() or tracer.poll() is not None:
                detail = [line.strip() for line in noise if line.strip()]
                return ProbeResult(
                    error=next(
                        (line for line in detail if "ERROR:" in line),
                        detail[-1] if detail else "bpftrace did not attach",
                    ),
                    raw="".join(noise),
                )
            child.send_signal(signal.SIGCONT)
            stopped = _wait_briefly(child, seconds)
        except OSError as exc:
            return ProbeResult(error=f"could not trace command: {exc}")
        finally:
            if child is not None:
                _kill_group(child)
            if tracer is not None:
                if tracer.poll() is None:
                    tracer.send_signal(signal.SIGINT)
                try:
                    tracer.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    tracer.kill()
                    tracer.wait(timeout=5)
                if watcher is not None:
                    watcher.join(timeout=2)
                if tracer.stderr is not None:
                    tracer.stderr.close()

        output = Path(sink.name).read_text(errors="replace")
    if "@" not in output:
        detail = [line.strip() for line in noise if line.strip()]
        return ProbeResult(
            error=detail[-1] if detail else "no events recorded",
            raw="".join(noise),
            stopped=stopped,
        )
    return ProbeResult(sections=parse_bpftrace(output), raw=output, stopped=stopped)


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
        return _wait_briefly(child, seconds)
    finally:
        _kill_group(child)


def _wait_briefly(child: subprocess.Popen, seconds: int) -> bool:
    try:
        child.wait(timeout=seconds)
        return False
    except subprocess.TimeoutExpired:
        try:
            os.killpg(child.pid, signal.SIGINT)
        except ProcessLookupError:
            pass
        try:
            child.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
        return True


def parse_keyed_stacks(text: str) -> dict[str, list[tuple[str, list[str], int]]]:
    """Parse stack maps, retaining any path or interface key before the stack."""
    stacks: dict[str, list[tuple[str, list[str], int]]] = {}
    name = ""
    key = ""
    frames: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not name:
            header = re.match(r"^@(\w+)\[(.*)$", stripped)
            if header and "]:" not in stripped:
                name, key = header.groups()
                key = key.rstrip(", ")
                frames = []
            continue
        if stripped.startswith("]:"):
            stacks.setdefault(name, []).append(
                (key, frames, _as_int(stripped[2:].strip()))
            )
            name = ""
        elif stripped:
            frames.append(stripped)
    return stacks


def parse_stacks(text: str) -> dict[str, list[tuple[list[str], int]]]:
    """Parse maps with only a stack key, preserving instruction offsets."""
    return {
        name: [(frames, count) for key, frames, count in rows if not key]
        for name, rows in parse_keyed_stacks(text).items()
        if any(not key for key, _frames, _count in rows)
    }

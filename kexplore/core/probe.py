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

import re
import shutil
import subprocess
from dataclasses import dataclass, field

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

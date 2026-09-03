"""The catalog: which structures matter, and where to start looking.

``core`` knows how to render and navigate any struct but nothing about Linux.
This package supplies the Linux part: entry points per subsystem, the links
between structures, and what raw field values mean. Self-contained views of an
operation live in ``operations`` instead.

There are three kinds of entry -- objects to browse, computed facts, and
measurements over time -- and they share one interface: ``check(prog)`` says
whether this entry works against this kernel and, for the browsable kind, hands
back what it resolved. That is what lets ``--check`` and the crawl test loop
over every entry without asking what type each one is.

Providers are called lazily and their failures are captured rather than raised,
because helper availability varies with kernel version and config -- a missing
``for_each_vmap_area`` should grey out one entry, not break sched.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Callable, Iterator

from drgn import Object, Program

from ..core.nav import Collection, collect, rows_for

# A provider returns either a single object or an iterable of labelled ones.
Provider = Callable[[Program], "Object | Iterator[tuple[str, Object]]"]


@dataclass(frozen=True)
class CheckResult:
    """Whether one entry works against this kernel, and what it found.

    ``collection`` is set only by entries that resolve to objects, and is what
    the crawl test navigates into -- so checking and crawling resolve once
    between them rather than once each.
    """

    ok: bool
    detail: str
    collection: Collection | None = None


@dataclass(frozen=True)
class Entry:
    key: str
    label: str
    doc: str
    provider: Provider
    # Optional submenu within the subsystem; entries without one sit at the top.
    group: str = ""

    def resolve(self, prog: Program) -> Collection:
        return collect(self.label, lambda: self.provider(prog))

    def check(self, prog: Program) -> CheckResult:
        collection = self.resolve(prog)
        if collection.error:
            return CheckResult(False, collection.error, collection)
        count = len(collection.items)
        depth = len(rows_for(collection.items[0][1])) if count else 0
        note = " (truncated)" if collection.truncated else ""
        return CheckResult(
            True, f"{count} item(s){note}, {depth} fields", collection
        )


@dataclass(frozen=True)
class Fact:
    """One answer on an info page.

    ``evidence`` records how the value was determined, so the claim can be
    checked against the kernel rather than taken on trust.
    """

    label: str
    value: str
    evidence: str = ""
    # Set when this fact is a trapped failure rather than an answer.
    failed: bool = False


@dataclass(frozen=True)
class FactEntry:
    """An entry that reports computed facts rather than kernel objects."""

    key: str
    label: str
    doc: str
    facts: Callable[[Program], "Iterator[Fact]"]
    group: str = ""

    def resolve(self, prog: Program) -> list[Fact]:
        """Collect facts, turning a failure into a visible row rather than a crash."""
        collected: list[Fact] = []
        try:
            for fact in self.facts(prog):
                collected.append(fact)
        except Exception as exc:  # noqa: BLE001 - report, don't abort the page
            collected.append(Fact("error", f"{type(exc).__name__}: {exc}", failed=True))
        return collected

    def check(self, prog: Program) -> CheckResult:
        facts = self.resolve(prog)
        bad = [fact for fact in facts if fact.failed]
        if bad:
            return CheckResult(False, bad[0].value)
        return CheckResult(True, f"{len(facts)} fact(s)")


@dataclass(frozen=True)
class Measurement:
    """An entry that measures behaviour rather than reading state.

    Runs a tracer in the foreground for a few seconds and returns the result.
    Nothing runs in the background and nothing is accumulated between runs.
    """

    key: str
    label: str
    doc: str
    measures: str
    script: str
    group: str = ""
    blind_spot: str = ""
    duration: int = 2
    per_second: bool = False
    # Per-section maps for turning raw keys into names, e.g. softirq vectors.
    key_labels: dict[str, dict[str, str]] = field(default_factory=dict)

    def run(self, duration: int | None = None):
        from ..core.probe import run_bpftrace

        return run_bpftrace(self.script, duration or self.duration)

    def check(self, prog: Program) -> CheckResult:
        """Report whether the tracer is present, without running it.

        Probes measure over time; running every one of them to check the
        catalog would take minutes and tell you nothing about this kernel that
        the backend's presence does not.
        """
        from ..core.probe import tool_available

        if tool_available("bpftrace"):
            return CheckResult(True, f"bpftrace available ({self.duration}s)")
        return CheckResult(False, "bpftrace not installed")


@dataclass(frozen=True)
class Subsystem:
    key: str
    label: str
    doc: str
    entries: list[Entry] = field(default_factory=list)


_SUBSYSTEMS: list[Subsystem] = []
_ATTACHED: dict[str, list] = {}


def register(subsystem: Subsystem) -> Subsystem:
    _SUBSYSTEMS.append(subsystem)
    return subsystem


def attach(subsystem_key: str, *entries) -> None:
    """Add entries to a subsystem defined elsewhere.

    Measurements are written together, because they share bpftrace idiom, but
    belong under the subsystem they describe. This is how they get there --
    once, at import, rather than by mutating the subsystem every time someone
    asks for the list.
    """
    _ATTACHED.setdefault(subsystem_key, []).extend(entries)


def subsystems() -> list[Subsystem]:
    """Every subsystem, in the order the modules are imported below.

    A pure accessor: the subsystems as registered, plus whatever was attached
    to them. Importing is the side effect, and Python's module cache makes it
    happen exactly once however often this is called.
    """
    from . import (  # noqa: F401,E401
        system, process, sched, mm, page, vfs, socket, net, skb, slab, device,
        measure,
    )

    return [
        replace(subsystem, entries=[*subsystem.entries, *_ATTACHED.get(subsystem.key, ())])
        for subsystem in _SUBSYSTEMS
    ]

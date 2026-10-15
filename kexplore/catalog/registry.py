"""The catalog: the structures each subsystem lists, and where browsing starts.

``core`` renders and navigates any struct and contains no Linux-specific code.
This package supplies the Linux-specific part: entry points per subsystem, the
links between structures, and the meanings of raw field values. Self-contained
views of an operation are in ``operations``.

There are three kinds of entry (objects to browse, computed facts, and
measurements over time), and all three share one interface: ``check(prog)``
reports whether the entry resolves on the running kernel and, for a browsable
entry, returns what it resolved. ``--check`` and the crawl test therefore iterate over
every entry without dispatching on its type.

Providers are called lazily and their failures are captured, not raised,
because helper availability varies with kernel version and configuration: a
missing ``for_each_vmap_area`` disables only the entry that calls it.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, replace

from drgn import Object, Program

from ..core.nav import Collection, collect, rows_for

# A provider returns either a single object or an iterable of labelled ones.
Provider = Callable[[Program], "Object | Iterator[tuple[str, Object]]"]


@dataclass(frozen=True)
class CheckResult:
    """Whether one entry resolves on this kernel, and what it resolved.

    ``collection`` is set only by entries that resolve to objects, and the
    crawl test navigates into it, so the check and the crawl share one
    resolution.
    """

    ok: bool
    detail: str
    collection: Collection | None = None
    supported: bool = True


@dataclass(frozen=True)
class Entry:
    key: str
    label: str
    doc: str
    provider: Provider
    # Optional submenu within the subsystem; entries without one sit at the top.
    group: str = ""
    # Header for a provider that yields cells instead of one label. Empty for an
    # entry whose rows show only type and address; those rows get the field,
    # type and value columns.
    columns: tuple[str, ...] = ()
    # None means no special requirement. A callback returns None when the
    # kernel supports this view, or a human-readable reason when it cannot.
    # Unexpected exceptions remain errors; they are not capability absence.
    capability: Callable[[Program], str | None] | None = None

    def resolve(self, prog: Program) -> Collection:
        if self.capability is not None:
            try:
                reason = self.capability(prog)
            except Exception as exc:  # noqa: BLE001 - probing bugs are failures
                return Collection(self.label, error=f"{type(exc).__name__}: {exc}")
            if reason:
                return Collection(self.label, unavailable=reason)
        return collect(self.label, lambda: self.provider(prog))

    def check(self, prog: Program) -> CheckResult:
        collection = self.resolve(prog)
        if collection.unavailable:
            return CheckResult(False, collection.unavailable, collection, supported=False)
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

    ``evidence`` records how the value was determined, so that it can be
    checked against the kernel.
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
    facts: Callable[[Program], Iterator[Fact]]
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
    It leaves no tracer running and keeps no data between runs.
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

        Probes measure over an interval, so running every probe to check the
        catalog would take minutes, and would show only that the tracer runs,
        which its presence already indicates.
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
    # The key of the subsystem this one is listed under in the sidebar, or ""
    # for a top-level one. Only the sidebar reads it: ``subsystems()`` stays
    # flat, so --check, the crawl and userspace commands keep their keys.
    parent: str = ""


_SUBSYSTEMS: list[Subsystem] = []
_ATTACHED: dict[str, list] = {}


def register(subsystem: Subsystem) -> Subsystem:
    _SUBSYSTEMS.append(subsystem)
    return subsystem


def attach(subsystem_key: str, *entries) -> None:
    """Add entries to a subsystem defined elsewhere.

    Measurements are defined in one module, because they share bpftrace
    idioms, and are listed under the subsystem they describe. This attaches
    them once, at import, instead of modifying the subsystem each time the list
    is requested.
    """
    _ATTACHED.setdefault(subsystem_key, []).extend(entries)


def subsystems() -> list[Subsystem]:
    """Every subsystem, in the order the modules are imported below.

    An accessor with no side effects: it returns the subsystems as registered,
    with the entries attached to them. Registration happens when the modules are
    imported, which Python does once per process.
    """
    # I001 is disabled for this block because the import order is the order of
    # the subsystems in the sidebar, and sorting it alphabetically would place
    # "device" first instead of "system". A parent is imported before the
    # subsystems listed under it, so that the sidebar branch exists for them.
    #
    # The order: the system and its tasks (system, process, sched), what
    # interrupts a task (irq, timers), then memory and the I/O paths, ending at
    # the hardware. block is between vfs and net because a write leaves a
    # filesystem as a bio and reaches a driver as a request. ipc follows sync
    # because both list what one task waits on another for: sync through
    # kernel locks, ipc through objects userspace names.
    from . import (  # noqa: F401,I001
        system, process, sched, sync, ipc, irq, timers, mm, page, slab, vfs,
        block, net, socket, skb, device, measure,
    )

    return [
        replace(subsystem, entries=[*subsystem.entries, *_ATTACHED.get(subsystem.key, ())])
        for subsystem in _SUBSYSTEMS
    ]


def children(subsystem_key: str) -> list[Subsystem]:
    """The subsystems listed under ``subsystem_key``, in registration order."""
    return [sub for sub in subsystems() if sub.parent == subsystem_key]

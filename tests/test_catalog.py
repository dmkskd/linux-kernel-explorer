"""What the catalog registers, without attaching to anything.

Registration, the link tables and plan dispatch are all decided at import time
from static data, so they can be checked anywhere -- which is the point. The
mistakes this catches are the quiet ones: a subsystem that stops registering, a
measurement that lands under the wrong subsystem or twice, a link that forgets
to say where it comes from, an entry kind nothing knows how to open.

drgn is only needed here to satisfy imports; nothing calls into it. Where it is
installed the real thing is used, and where it is not -- a laptop, CI -- a stub
stands in, because refusing to check any of this without a VM is how it rots.
"""

from __future__ import annotations

import sys
import types

ok = True


def check(condition: bool, message: str) -> None:
    global ok
    ok &= bool(condition)
    print(("  ok   " if condition else "  FAIL ") + message)


def install_stubs() -> None:
    """Stand in for drgn, textual and rich, for imports only."""

    class Stub(types.ModuleType):
        def __getattr__(self, name):
            if name.startswith("__"):
                raise AttributeError(name)
            # A class, so it can be subclassed (App, Screen, Static),
            # called, or used as a value -- and it swallows its arguments.
            value = type(name, (object,), {
                "__module__": self.__name__,
                "__init__": lambda self, *a, **k: None,
                "__getattr__": lambda self, n: None,
            })
            setattr(self, name, value)
            return value

    names = [
        "drgn", "drgn.helpers", "drgn.helpers.common", "drgn.helpers.common.format",
        "drgn.helpers.linux", "rich", "rich.syntax", "rich.text", "textual",
        "textual.app",
        "textual.binding", "textual.color", "textual.containers", "textual.screen",
        "textual.widgets",
    ] + [
        f"drgn.helpers.linux.{name}" for name in (
            "block", "cpumask", "device", "fs", "kthread", "list", "mm", "mmzone",
            "module", "net", "pci", "percpu", "pid", "rbtree", "sched", "slab",
            "timekeeping",
        )
    ]
    for name in names:
        module = Stub(name)
        module.__path__ = []  # so submodules can be imported from it
        sys.modules[name] = module
        parent, _, leaf = name.rpartition(".")
        if parent:
            setattr(sys.modules[parent], leaf, module)

    # TypeKind members have to be distinct and hashable: they go in a frozenset.
    sys.modules["drgn"].TypeKind = types.SimpleNamespace(
        **{kind: kind for kind in (
            "STRUCT", "UNION", "CLASS", "POINTER", "ARRAY", "TYPEDEF", "INT",
            "BOOL", "FLOAT", "ENUM", "FUNCTION", "VOID")}
    )
    for name in ("FaultError", "OutOfBoundsError"):
        setattr(sys.modules["drgn"], name, type(name, (Exception,), {}))


def main() -> int:
    try:
        import drgn  # noqa: F401
        import textual  # noqa: F401

        print("  (using the real drgn and textual)")
    except ImportError:
        install_stubs()
        print("  (drgn/textual not installed here: importing against stubs)")

    import kexplore.tui.app  # noqa: F401  - the frontend must still import
    from kexplore.catalog.links import LINKS
    from kexplore.catalog.registry import Entry, Measurement, subsystems
    from kexplore.operations.algorithm import algorithms
    from kexplore.view import frames

    check(True, "every module imports")

    # --- registration ---------------------------------------------------
    subs = subsystems()
    keys = [s.key for s in subs]
    check(
        keys == ["system", "process", "sched", "mm", "page", "vfs", "socket",
                 "net", "skb", "slab", "device", "measure"],
        f"subsystems register in module order: {keys}",
    )
    check(len(keys) == len(set(keys)), "no subsystem registers twice")

    # subsystems() is an accessor, not a mutation: asking twice cannot grow it.
    once = {s.key: len(s.entries) for s in subs}
    twice = {s.key: len(s.entries) for s in subsystems()}
    check(once == twice, f"asking twice gives the same catalog: {once == twice}")

    sched = next(s for s in subs if s.key == "sched")
    attached = [e for e in sched.entries if isinstance(e, Measurement)]
    check(len(attached) == 5, f"sched's {len(attached)} measurements are attached to it")
    check(isinstance(sched.entries[0], Entry),
          "attached measurements come after the browsable entries")
    check(all(e.group == "measure" for e in attached),
          "attached measurements land in the 'measure' group")

    measure = next(s for s in subs if s.key == "measure")
    check(
        len(measure.entries) == 2
        and all(isinstance(e, Measurement) for e in measure.entries),
        f"the cross-cutting subsystem keeps only its own {len(measure.entries)}",
    )

    everything = [(s.key, e) for s in subs for e in s.entries]
    ids = [(key, e.key) for key, e in everything]
    check(len(ids) == len(set(ids)), "no entry appears twice in a subsystem")

    # --- one interface --------------------------------------------------
    check(
        all(hasattr(e, "check") for _, e in everything),
        "every entry kind answers check(), so --check needs no isinstance",
    )
    check(
        all(frames.plan_for(e, None) is not None for _, e in everything),
        "every entry kind can be turned into a plan",
    )
    check(
        all(
            frames.plan_for(e, None).deferred == isinstance(e, Measurement)
            for _, e in everything
        ),
        "measurements are the only entries built off the UI thread",
    )
    check(frames.plan_for(object(), None) is None, "an unknown item plans nothing")

    # A preview is what the sidebar builds for the entry the cursor rests on,
    # so nothing in it may attach a tracer or otherwise take seconds.
    check(
        all(not frames.plan_for(e, None, preview=True).deferred
            for _, e in everything),
        "no preview defers, so scrolling the tree starts no bpftrace",
    )
    measurement = next(e for _, e in everything if isinstance(e, Measurement))
    frame = frames.plan_for(measurement, None, preview=True).build()
    frame.load()
    rows = frame.rows
    check(
        any(r.name == "measures" for r in rows)
        and any("enter" in r.name for r in rows),
        "a previewed measurement states what it measures and how to run it",
    )

    analyses = algorithms()
    check(len(analyses) >= 3, f"{len(analyses)} analyses register themselves")
    check(len(algorithms()) == len(analyses), "asking twice does not duplicate them")

    # --- links ----------------------------------------------------------
    links = [(tag, link) for tag, group in LINKS.items() for link in group]
    check(
        all(link.origin for _, link in links),
        "every link states its origin: "
        f"{[l.label for _, l in links if not l.origin] or 'all do'}",
    )
    labels = [(tag, link.label) for tag, link in links]
    check(len(labels) == len(set(labels)), "no type has two links with one label")
    commands = [link for _, link in links if link.userspace]
    check(len(commands) > 30, f"{len(commands)} links carry a userspace command")
    check(
        all("<pid>" not in link.userspace or "/proc/" in link.userspace
            or "ps " in link.userspace or "pgrep" in link.userspace
            or "ss " in link.userspace
            for link in commands),
        "every <pid> placeholder sits in a command that takes one",
    )

    print("\nPASS" if ok else "\nFAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

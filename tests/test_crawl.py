"""Breadth-first crash hunt across every subsystem.

The bugs that actually reached the user were all the same shape: some type
reached by navigation violated an assumption held elsewhere (a struct has no
truth value; a pointer has no tag; an array has no tag). None were caught by
targeted tests, because the targeted tests only visited types we'd thought
about.

So this visits everything reachable in two hops from every map entry and
reports anything that raises. It's a net, not an assertion.

It drives ``view.frames`` rather than the TUI: the frames are what the bugs
live in, and building them needs no terminal.
"""

from __future__ import annotations

import sys

import drgn

from kexplore.catalog.links import links_for, userspace_for
from kexplore.catalog.registry import subsystems
from kexplore.core.nav import follow
from kexplore.core.source import KernelSource
from kexplore.view.frames import Context, object_frame

ROWS_PER_FRAME = 60
ITEMS_PER_ENTRY = 2


def deref(obj):
    """Follow a pointer if it is one, else use the object as-is."""
    target = follow(obj)
    return obj if target is None else target


def visit(label, obj, ctx, failures, depth):
    """Build the frame for obj and follow each of its rows one level."""
    try:
        frame = object_frame(label, obj, ctx)
        frame.load()
    except Exception as exc:  # noqa: BLE001
        failures.append((label, f"{type(exc).__name__}: {exc}"))
        return

    # Link expansion traps its own errors and reports them as an error row.
    for row in frame.rows:
        if row.kind == "error":
            failures.append((f"{label} › {row.name}", "link raised"))

    # The same frame in userspace mode: substituting a pid and looking up a
    # field's command must work for every type, not just tasks -- obj.pid
    # raises AttributeError on a struct file or mount, and ct.safe
    # deliberately does not catch that.
    try:
        userspace = Context(ctx.prog, ctx.source, userspace=True)
        object_frame(label, obj, userspace).load()
        for link in links_for(obj):
            userspace_for(link, obj)
    except Exception as exc:  # noqa: BLE001
        failures.append((f"{label} (userspace)", f"{type(exc).__name__}: {exc}"))

    if depth <= 0:
        return

    for row in frame.rows[:ROWS_PER_FRAME]:
        if not row.followable:
            continue
        try:
            if row.expand is not None:
                sub = row.expand()
                for item in sub[:1]:
                    if item.obj is not None:
                        visit(f"{label} › {row.name}", deref(item.obj), ctx,
                              failures, depth - 1)
            elif row.obj is not None:
                target = follow(row.obj)
                if target is not None:
                    visit(f"{label} › {row.name}", target, ctx, failures, depth - 1)
        except Exception as exc:  # noqa: BLE001 - the whole point
            failures.append((f"{label} › {row.name}", f"{type(exc).__name__}: {exc}"))


def main() -> int:
    prog = drgn.program_from_kernel()
    ctx = Context(prog, KernelSource())
    failures: list[tuple[str, str]] = []
    entries = 0

    for subsystem in subsystems():
        for entry in subsystem.entries:
            # Every kind of entry answers check(); the browsable ones hand back
            # what they resolved, and the rest (facts, measurements) report
            # themselves and have nothing to navigate into.
            result = entry.check(prog)
            where = f"{subsystem.label}/{entry.label}"
            if not result.ok:
                failures.append((where, result.detail))
                continue
            entries += 1
            if result.collection is None:
                continue
            for label, obj in result.collection.items[:ITEMS_PER_ENTRY]:
                visit(f"{where}", deref(obj), ctx, failures, depth=1)
        print(f"  crawled {subsystem.label}")

    print(f"\nvisited {entries} entries")
    if failures:
        print(f"{len(failures)} failure(s):")
        for path, error in failures[:40]:
            print(f"  FAIL {path}: {error}")
        return 1
    print("no failures")
    return 0


if __name__ == "__main__":
    sys.exit(main())

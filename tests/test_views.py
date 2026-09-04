"""Two views over the same kernel: the structure browser and walkthroughs.

The structure browser must keep working exactly as before when the walkthrough
view is added, so this checks both and the switch between them.
"""

from __future__ import annotations

import asyncio
import sys

import drgn
from rich.text import Text
from textual.widgets import DataTable, Input, Tabs, Tree

from harness import settle
from kexplore.catalog.registry import Entry
from kexplore.operations.algorithm import Algorithm
from kexplore.operations.walkthrough import Walkthrough
from kexplore.catalog.registry import Measurement
from kexplore.core.nav import Row
from kexplore.tui.app import (
    MAX_CELL,
    PREVIEW_DELAY,
    SOURCE_FIXED_COLUMNS,
    Explorer,
    _highlight_source,
    _paint_value,
)

ok = True


def check(condition: bool, message: str) -> None:
    global ok
    ok &= bool(condition)
    print(("  ok   " if condition else "  FAIL ") + message)


async def main() -> int:
    prog = drgn.program_from_kernel()
    app = Explorer(prog)

    async with app.run_test(size=(150, 45)) as pilot:
        tree = app.query_one("#nav", Tree)
        table = app.query_one("#fields", DataTable)
        tabs = app.query_one("#views", Tabs)

        # Landing page. Built in a worker (it probes pahole, debuginfod and
        # bpftrace), so the first thing on screen is a placeholder.
        await settle(app, pilot)
        rows = app.stack[-1].rows
        check(app.stack[-1].label == "kexplore", "lands on an overview page")
        check(any(r.name == "kernel release" for r in rows), "shows the kernel release")
        caps = {r.name: r.value for r in rows}
        check("struct docs and source" in caps, f"docs: {caps.get('struct docs and source')}")
        check("measurements" in caps, f"measurements: {caps.get('measurements')}")
        check(any(r.name == "what is running right now" for r in rows), "suggests a start")

        # Structure view still works.
        subsystem_names = [str(n.label) for n in tree.root.children]
        check("sched" in subsystem_names and "mm" in subsystem_names,
              f"structure view lists {len(subsystem_names)} subsystems")
        node = next(
            n for b in tree.root.children for n in b.children
            if isinstance(n.data, Entry) and n.data.key == "runqueues"
        )
        tree.select_node(node)
        await pilot.pause()
        check(len(app.stack[-1].rows) >= 1, "structure entry still opens")
        headers = tuple(str(c.label) for c in table.columns.values())
        check(headers == ("field", "type", "value", "offset+size"),
              f"struct columns intact: {headers}")

        # Offsets exist on struct fields, not on a list of objects, so follow
        # into one of the runqueues first.
        table.focus()
        app.action_follow()
        await pilot.pause()
        placed = [r for r in app.stack[-1].rows if r.offset is not None]
        check(len(placed) > 5, f"{len(placed)} fields carry an offset")
        if placed:
            sample = placed[0]
            check("+" in sample.placement and "L" in sample.placement,
                  f"placement reads as offset+size and cache line: {sample.placement}")
            check(all(r.offset is None or r.offset >= 0 for r in app.stack[-1].rows),
                  "offsets are non-negative")
        app.action_back()
        await pilot.pause()

        # 'u' swaps the kernel origin for the userspace equivalent, with the
        # pid of the object substituted so the command is copy-pasteable.
        node = next(n for b in tree.root.children for n in b.children
                    if isinstance(n.data, Entry) and n.data.key == "init")
        tree.select_node(node)
        await pilot.pause()
        table.focus()
        app.action_follow()
        await pilot.pause()
        origins = {r.name: r.type_name for r in app.stack[-1].rows if r.kind == "link"}
        app.action_userspace()
        await pilot.pause()
        commands = {r.name: r.type_name for r in app.stack[-1].rows if r.kind == "link"}
        check(origins["VMAs"].startswith("walks"), f"origin: {origins['VMAs']}")
        check(commands["VMAs"] == "cat /proc/1/maps", f"userspace: {commands['VMAs']}")
        check("/proc/1/" in commands["open files"], "pid substituted into the command")
        app.action_userspace()
        await pilot.pause()
        back = {r.name: r.type_name for r in app.stack[-1].rows if r.kind == "link"}
        check(back == origins, "toggling back restores the kernel origins")

        # Switch to walkthroughs.
        tabs.active = "view-operations"
        await pilot.pause()
        walks = [n for b in tree.root.children for n in b.children]
        check(len(walks) >= 4, f"operations tab lists {len(walks)} entries")

        wakeup = next(n for n in walks if isinstance(n.data, Walkthrough)
                      and n.data.key == "wakeup")
        tree.select_node(wakeup)
        await settle(app, pilot)
        steps = app.stack[-1].rows
        check(len(steps) == 7, f"wakeup has {len(steps)} steps")
        check(steps[0].name.startswith("1. try_to_wake_up"), f"first step: {steps[0].name}")

        located = [r for r in steps if ":" in r.type_name]
        check(len(located) >= 4,
              f"{len(located)}/{len(steps)} steps resolved to source, e.g. {located[0].type_name}")

        headers = tuple(str(c.label) for c in table.columns.values())
        check(headers == ("step", "source", "what happens"), f"step columns: {headers}")

        # A step with structures opens them in the structure browser.
        index = next(i for i, r in enumerate(steps) if r.followable)
        table.move_cursor(row=index)
        app.action_follow()
        await pilot.pause()
        check(len(app.stack) == 2, f"step {steps[index].name!r} opened its structures")
        check(app.stack[-1].rows[0].obj is not None, "structures are real objects")

        # 's' on a walkthrough step shows that function's source inline.
        app.action_back()
        await pilot.pause()
        step = next(i for i, r in enumerate(app.stack[-1].rows) if ":" in r.type_name)
        wanted = app.stack[-1].rows[step].type_name
        table.move_cursor(row=step)
        app.action_source()
        await settle(app, pilot)
        check(app.stack[-1].label == wanted, f"source frame for {wanted}")
        headers = tuple(str(c.label) for c in table.columns.values())
        check(headers == ("line", "", "source"), f"source columns: {headers}")
        marked = [r for r in app.stack[-1].rows if r.type_name == "\u25b8"]
        check(len(marked) == 1, "exactly one line is marked")
        if marked:
            print(f"         {marked[0].name}: {marked[0].value.strip()[:60]}")

        # A source line reaches the screen whole. MAX_CELL exists to stop a long
        # value pushing later columns off a field view; here the wide column is
        # last, so the table scrolls sideways instead, with the line number and
        # the marker pinned so they survive that scroll.
        source_rows = app.stack[-1].rows
        longest = max(source_rows, key=lambda r: len(r.value))
        on_screen = table.get_row_at(source_rows.index(longest))[2].plain
        check(len(longest.value) > MAX_CELL,
              f"the widest line is {len(longest.value)} chars, past the {MAX_CELL} cap")
        check(on_screen == longest.value,
              f"and is shown whole: {on_screen[-20:]!r}")
        check(table.fixed_columns == SOURCE_FIXED_COLUMNS,
              "the line number stays put when the table scrolls sideways")

        app.action_back()
        await pilot.pause()

        # Analyses live in the same tab as step sequences.
        algos = [n for b in tree.root.children for n in b.children
                 if isinstance(n.data, Algorithm)]
        check(len(algos) >= 1, f"operations tab also lists {len(algos)} analyses")
        tree.select_node(algos[0])
        await pilot.pause()
        # The rule now sits above the table rather than as a row, so the
        # columns can be used for the comparison itself.
        rows = app.stack[-1].rows
        check("rule" not in [r.name for r in rows], "rule is not a table row")
        check(len(app.stack[-1].doc) > 40, "rule shown above the table")
        check(any(r.name.startswith("cpu") for r in rows), "lists per-CPU inputs")
        check(any("nothing to pick" in r.type_name or "would pick" in r.name
                  or "tree empty" in r.type_name for r in rows),
              "reaches an outcome on every runqueue")
        headers = tuple(str(c.label) for c in table.columns.values())
        check(headers == ("input", "value", "why"), f"algorithm columns: {headers}")

        # Every analysis must emit as many cells as it declares columns:
        # a mismatch silently shifts every value into the wrong column.
        from kexplore.operations.algorithm import algorithms
        from kexplore.view.frames import algorithm_frame

        for algorithm in algorithms():
            frame = algorithm_frame(app.context, algorithm)
            frame.load()
            expected = len(algorithm.columns)
            bad = [
                (r.name, len(r.cells))
                for r in frame.rows
                if r.cells is not None and len(r.cells) != expected
            ]
            check(
                not bad,
                f"{algorithm.key}: all rows have {expected} cells"
                + (f" (bad: {bad[:2]})" if bad else ""),
            )

        # Highlighting a tree node fills the pane without enter, after the
        # debounce. Measurements are the exception: the preview must state what
        # they do rather than attach a tracer for seconds.
        tabs.active = "view-structures"
        await pilot.pause()
        tree.focus()
        entry = next(
            n for b in tree.root.children for n in b.children
            if isinstance(n.data, Entry) and n.data.key == "runqueues"
        )
        app.stack.clear()
        tree.cursor_line = entry.line
        await pilot.pause(PREVIEW_DELAY * 2)
        await settle(app, pilot)
        check(bool(app.stack) and len(app.stack[-1].rows) >= 1,
              "highlighting an entry fills the pane with no enter")

        # Measurements sit in a collapsed "measure" group, and a node inside a
        # collapsed branch occupies no line for the cursor to move to.
        group = next(
            g for b in tree.root.children for g in b.children
            if g.children and isinstance(g.children[0].data, Measurement)
        )
        group.expand()
        await pilot.pause()
        measurement = group.children[0]
        app.stack.clear()
        tree.cursor_line = measurement.line
        await pilot.pause(PREVIEW_DELAY * 2)
        await settle(app, pilot)
        names = [r.name for r in app.stack[-1].rows]
        check("measures" in names and any("enter" in n for n in names),
              f"a highlighted measurement is described, not run: {names[:3]}")

        # Headings are not dead ends: a branch opens what it contains, and an
        # index row opens the item it names.
        app.stack.clear()
        tree.cursor_line = 0
        await pilot.pause(PREVIEW_DELAY * 2)
        await settle(app, pilot)
        index = app.stack[-1].rows
        check(app.stack[-1].label == "subsystems",
              f"the root heading opens a listing: {app.stack[-1].label}")
        check(len(index) == 12 and all(r.item is not None for r in index),
              f"{len(index)} subsystems listed, each openable")
        check(all(r.doc for r in index), "every subsystem row says what it is")

        table.move_cursor(row=[r.name for r in index].index("sched"))
        app.action_follow()
        await settle(app, pilot)
        entries = app.stack[-1].rows
        check(app.stack[-1].label == "sched",
              f"following an index row opens it: {app.stack[-1].label}")
        check(any(r.name == "runqueues (per-cpu)" for r in entries),
              "the subsystem listing names its entries")

        check(tree.cursor_node is not None
              and str(tree.cursor_node.label) == "sched",
              f"the sidebar cursor follows the pane: {tree.cursor_node.label}")

        app.action_follow()
        await settle(app, pilot)
        check(app.stack[-1].label.startswith("runqueues"),
              f"and its entries open too: {app.stack[-1].label}")
        check(str(tree.cursor_node.label) == "runqueues (per-cpu)",
              f"and follows it down: {tree.cursor_node.label}")

        # The sync moved the cursor, which must not fire a preview that would
        # rebuild the frame and throw the history away.
        depth = len(app.stack)
        await pilot.pause(PREVIEW_DELAY * 2)
        await settle(app, pilot)
        check(len(app.stack) == depth,
              f"syncing the cursor does not reopen the frame ({depth} deep)")

        # Escape goes back, like backspace.
        depth = len(app.stack)
        app.action_escape()
        await pilot.pause()
        check(len(app.stack) == depth - 1, "escape goes back a frame")

        # With a filter open it closes that first, leaving the frame alone.
        app.action_search()
        await pilot.pause()
        app.query_one("#search", Input).value = "cpu"
        await pilot.pause()
        depth = len(app.stack)
        app.action_escape()
        await pilot.pause()
        check(len(app.stack) == depth and not app.filter,
              "escape drops the filter before it drops the frame")

        # Kernel source, coloured by pygments through rich.
        highlighted = _highlight_source([
            Row("1", None, "", "/* a comment", False),
            Row("2", None, "", " * over two lines */", False),
            Row("3", None, "", "struct task_struct *p;", False),
        ])
        check(highlighted["2"].spans and all(
                  span.style.dim for span in highlighted["2"].spans),
              "a comment stays a comment on its continuation line")
        check(any(span.style.color for span in highlighted["3"].spans),
              "a declaration is coloured")
        check(highlighted["3"].plain == "struct task_struct *p;",
              "highlighting does not alter the text")

        # Value cells, coloured by the form the value takes. A styled span must
        # never start or end on a space: the value column is aligned by those
        # spaces, and styling them is how they get lost.
        def painted(value):
            cell = Text(value)
            _paint_value(cell)
            return {value[s.start:s.end]: s.style for s in cell.spans}

        cases = {
            "NULL": "NULL",
            "0xffff800080038000": "0xffff800080038000",
            "{…} 5 fields": "{…} 5 fields",
            "0 (root)": "0",
            "1  = S (sleeping)": "= S (sleeping)",
        }
        for value, span in cases.items():
            check(span in painted(value), f"{value!r} colours {span!r}")
        spans = painted("4194560 (0x400100)  = PF_KTHREAD")
        check(set(spans) == {"4194560", "(0x400100)", "= PF_KTHREAD"},
              f"a number, its hex and its decoding are coloured apart: {sorted(spans)}")
        check(all(part == part.strip() for part in spans),
              "no coloured span starts or ends on a space")
        check(not painted("init_user_ns"), "a bare symbol name keeps the default colour")

        # Switching back restores the structure view.
        tabs.active = "view-structures"
        await pilot.pause()
        subsystem_names = [str(n.label) for n in tree.root.children]
        check("sched" in subsystem_names, "structure view restored")

    print("\nPASS" if ok else "\nFAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

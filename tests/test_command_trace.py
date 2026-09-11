"""Live command tracing, interface stacks, selected fields, and source navigation.

These checks need root, bpftrace, and the running kernel's debugging information.
Pure parsing and process-launcher checks also run in the host suite.
"""

from __future__ import annotations

import asyncio
import importlib
import subprocess
import sys
import time
from types import SimpleNamespace
from unittest.mock import Mock, patch

import drgn
from harness import settle
from textual.widgets import DataTable, Tree

from kexplore.catalog.registry import Entry
from kexplore.catalog.userspace import UNFILLED
from kexplore.tui.app import Explorer

ok = True


def check(condition: bool, message: str) -> None:
    global ok
    ok &= bool(condition)
    print(("  ok   " if condition else "  FAIL ") + message)


def open_entry(app, tree, key):
    node = next(
        n
        for branch in tree.root.children
        for n in branch.children
        if isinstance(n.data, Entry) and n.data.key == key
    )
    app.stack.clear()
    tree.select_node(node)


def test_trace_evidence() -> None:
    trace = importlib.import_module("kexplore.operations.command_trace")
    from kexplore.core.probe import Section

    prog = Mock()
    prog.symbol.side_effect = lambda address: SimpleNamespace(
        name={101: "wanted", 202: "startup"}[address]
    )
    sections = {
        "shows": Section(
            "shows",
            [
                ("/, /, loadavg, 101, 0", "1", ""),
                ("/, 42, maps, 202, 0", "20", ""),
            ],
        )
    }
    check(
        trace._measured_leaf(prog, sections, "/proc/loadavg") == ("wanted", 101),
        "a busier different file cannot supply the selected file's leaf",
    )
    prog.symbols.return_value = [SimpleNamespace(address=1000)]
    with patch.object(trace, "_where_at", return_value="fs/read_write.c:555") as where:
        trace._stack_where(prog, "vfs_read+204")
        check(
            where.call_args.args == (prog, 1204),
            "stack lookup retains the recorded offset",
        )
    prog.symbols.return_value = [
        SimpleNamespace(address=1000),
        SimpleNamespace(address=2000),
    ]
    check(
        trace._stack_where(prog, "m_show+4") == "",
        "ambiguous stack names have no invented source location",
    )

    interface = trace.Interface("/proc/<pid>/stat", "leaf", probed=True)
    sources = {
        "leaf": (
            "task->flags; helper(task); missing(task);",
            {"task": "struct task_struct *"},
        ),
        "helper": ("p->utime; deeper(p);", {"p": "struct task_struct *"}),
    }
    with patch.object(
        trace,
        "_source_function",
        side_effect=lambda p, name, address=0: sources.get(name),
    ) as source:
        evidence = trace._field_evidence(prog, interface)
        check(
            evidence.helpers
            == [("task_struct.utime", "struct task_struct *", "helper")],
            "one helper level keeps parameter types and helper provenance",
        )
        check(
            "deeper" not in [call.args[1] for call in source.call_args_list],
            "helper calls do not recurse",
        )
        check(
            evidence.unavailable == ["missing"],
            "unavailable helper analysis is explicit",
        )
    selected = trace._selected_evidence("task_struct.utime", [interface], [evidence])
    check(
        "via helper" in selected.why
        and "runtime access not established" in selected.why,
        "selected field distinguishes static helper evidence from measured access",
    )
    selected = trace._selected_evidence("task_struct.on_cpu", [interface], [evidence])
    check(
        "not established" in selected.why,
        "unexplained selected fields are reported honestly",
    )

    sections = {
        "reads": Section(
            "reads", [("/, 42, stat", "1", ""), ("/, 42, status", "3", "")]
        ),
        "entry": Section("entry", [("kprobe:inet_diag_dump", "2", "")]),
    }
    raw = "@read_stack[/, 42, stat,\nseq_read_iter+0\n]: 1\n@read_stack[/, 42, status,\nseq_read_iter+0\n]: 3\n@entry_stack[kprobe:inet_diag_dump,\ninet_diag_dump+0\n]: 2\n"
    items = trace._interfaces(prog, sections, raw, "ss -p", None)
    check(
        {item.path or item.function for item in items}
        == {"inet_diag_dump", "/proc/<pid>/stat", "/proc/<pid>/status"},
        "netlink does not discard file interfaces",
    )
    check(
        all(item.stacks for item in items),
        "every interface keeps its own discovery stack",
    )
    with patch.object(trace, "_probeable", return_value=True):
        script = trace._stack_script(prog, items)
    check(
        '== "42"' not in script and "@trace_file[tid] ==" in script,
        "second pass correlates the file and allows a new self PID",
    )


def interface_row(rows, function):
    return next(
        (
            r
            for r in rows
            if r.value == function
            or (r.label.strip() == function and "discovery calls" in r.why)
        ),
        None,
    )


async def main() -> int:
    test_trace_evidence()
    app = Explorer(drgn.program_from_kernel())

    async with app.run_test(size=(160, 50)) as pilot:
        tree = app.query_one("#nav", Tree)
        table = app.query_one("#fields", DataTable)
        table.focus()

        open_entry(app, tree, "processes")
        await settle(app, pilot)
        app.action_follow()
        await settle(app, pilot)

        names = [r.name for r in app.stack[-1].rows]
        table.move_cursor(row=names.index("flags"))
        check(
            app.command_under_cursor() == "",
            "a type column that is still a type is not a command",
        )

        await pilot.press("u")
        await settle(app, pilot)
        names = [r.name for r in app.stack[-1].rows]
        table.move_cursor(row=names.index("flags"))
        command = app.command_under_cursor()
        check("/proc/" in command, f"the row carries a command: {command[:40]}")

        depth = len(app.stack)
        await pilot.press("t")
        check(len(app.stack) == depth + 1, "t pushes a frame rather than a dialog")
        check(
            "under bpftrace" in app.stack[-1].rows[0].name,
            "the placeholder says what is running while it runs",
        )

        await settle(app, pilot)
        rows = app.stack[-1].rows
        headings = [r.name for r in rows if r.name[:2] in ("1.", "2.", "3.", "4.")]
        check(len(headings) == 4, f"four stages: {headings}")
        # The trace replaces the view it was started from, so the first stage
        # names the row rather than pointing at one that is no longer there.
        check(
            rows[0].value == "userspace column of flags",
            f"the command says where it came from: {rows[0].value}",
        )

        stack = [r for r in rows if "fs/proc/array.c" in r.type_name]
        check(bool(stack), "the measured stack reaches fs/proc/array.c")
        leaf = next((r for r in rows if "do_task_stat" in r.name), None)
        check(leaf is not None, "do_task_stat is in the stack, not asserted by a table")

        published = [
            r.name.strip() for r in rows if r.name.strip().startswith("task_struct.")
        ]
        check(
            "task_struct.flags" in published,
            f"the field it was opened from is listed as published: {published[:3]}",
        )

        selected = next((r for r in rows if r.name.strip() == "selected field"), None)
        check(
            selected is not None
            and selected.type_name == "task_struct.flags"
            and "direct source reference" in selected.value,
            "the selected struct field is carried into the trace with its evidence",
        )

        # A struct reached from a task does not carry its pid, and the command
        # cannot run with the placeholder still in it. The mm_struct's owner is
        # walked back to, which is what makes this row traceable at all.
        while len(app.stack) > 1:
            app.action_back()
        await settle(app, pilot)
        app.action_follow()  # back down into the first task
        await settle(app, pilot)
        names = [r.name for r in app.stack[-1].rows]
        table.move_cursor(row=names.index("mm"))
        app.action_follow()
        await settle(app, pilot)
        names = [r.name for r in app.stack[-1].rows]
        table.move_cursor(row=names.index("pgtables_bytes"))
        mm_command = app.command_under_cursor()
        check(
            "<pid>" not in mm_command and "/status" in mm_command,
            f"an mm_struct row carries a runnable command: {mm_command}",
        )
        await pilot.press("t")
        await settle(app, pilot)
        mm_rows = app.stack[-1].rows
        check(
            any("proc_pid_status" in r.name for r in mm_rows),
            "status descends to proc_pid_status, not to the stat function",
        )
        publishes = next(
            (r for r in mm_rows if "struct mm_struct" in r.type_name), None
        )
        check(
            publishes is not None
            and "mm_struct" in publishes.type_name
            and "task_struct" in publishes.type_name,
            "one file publishing two structs names both: "
            f"{publishes.type_name if publishes else '(missing)'}",
        )

        while len(app.stack) > 1:
            app.action_back()
        await settle(app, pilot)
        app.action_follow()
        await settle(app, pilot)
        names = [r.name for r in app.stack[-1].rows]
        table.move_cursor(row=names.index("flags"))
        await pilot.press("t")
        await settle(app, pilot)
        rows = app.stack[-1].rows
        stack = [r for r in rows if "fs/proc/array.c" in r.type_name]

        # A link row shows a command too, and one that names no file at all:
        # the trace has to find out which file it read.
        while len(app.stack) > 1:
            app.action_back()
        await settle(app, pilot)
        app.action_follow()
        await settle(app, pilot)
        names = [r.name for r in app.stack[-1].rows]
        table.move_cursor(row=names.index("children"))
        link_command = app.command_under_cursor()
        check(
            link_command.startswith("pgrep"),
            f"a link row carries a command too: {link_command}",
        )
        await pilot.press("t")
        await settle(app, pilot)
        link_rows = app.stack[-1].rows
        serves = next((r for r in link_rows if r.type_name == "proc_pid_status"), None)
        check(
            serves is not None and "leaf from the catalog" in serves.value,
            f"the file was measured, not read off the command: "
            f"{serves.value if serves else '(missing)'}",
        )
        check(
            serves is not None and "proc_pid_status" in serves.type_name,
            f"and it leads to a real function: "
            f"{serves.type_name if serves else '(missing)'}",
        )

        while len(app.stack) > 1:
            app.action_back()
        await settle(app, pilot)
        app.action_follow()
        await settle(app, pilot)
        names = [r.name for r in app.stack[-1].rows]
        table.move_cursor(row=names.index("flags"))
        await pilot.press("t")
        await settle(app, pilot)
        rows = app.stack[-1].rows
        stack = [r for r in rows if "fs/proc/array.c" in r.type_name]

        # The rung rows carry file:line, so the source key still works on them:
        # a dialog would have ended the chain here.
        table.move_cursor(row=rows.index(stack[-1]))
        await pilot.press("s")
        await settle(app, pilot)
        check(
            app.stack[-1].label.startswith("fs/proc/array.c:"),
            f"s on a stack row opens its source: {app.stack[-1].label}",
        )

        # A struct file records no holder, so its commands can only be
        # completed by finding the task that has it open. Same resolver as the
        # task and mm rows above, which is the point of it being one resolver.
        while len(app.stack) > 1:
            app.action_back()
        await settle(app, pilot)
        open_entry(app, tree, "files_pid1")
        await settle(app, pilot)
        app.action_follow()
        await settle(app, pilot)
        rows = app.stack[-1].rows
        commands = [r.type_name for r in rows if r.original_type]
        check(bool(commands), f"the struct file view shows commands: {len(commands)}")
        check(
            all("<pid>" not in c and "<n>" not in c for c in commands),
            f"a struct file fills both placeholders: {commands[:2]}",
        )

        # A command reaching the kernel through something other than a file
        # read still resolves: ss goes over netlink and reads nothing.
        from kexplore.operations.command_trace import command_trace

        rows = list(command_trace(app.prog, "ss -tanH"))
        entry = interface_row(rows, "inet_diag_dump")
        check(
            entry is not None and "inet_diag_dump" in entry.label,
            f"a netlink command resolves its entry point: "
            f"{entry.value if entry else '(missing)'}",
        )

        # ss -tanp reads /proc/<pid>/stat for every process to put a name next
        # to each socket, so both interfaces fire. The netlink request is what
        # the command asked; the reads are how it decorates the answer.
        rows = list(command_trace(app.prog, "ss -tanp"))
        entry = interface_row(rows, "inet_diag_dump")
        check(
            entry is not None and "inet_diag_dump" in entry.label,
            f"the netlink request retains its own interface: "
            f"{entry.value if entry else '(missing)'}",
        )
        check(
            entry is not None
            and any(r.label.strip() == "/proc/<pid>/stat" for r in rows),
            f"and the reads are still reported, not dropped: "
            f"{entry.why if entry else '(missing)'}",
        )

        # m_show is defined in two translation units, so kallsyms holds two of
        # them and no kprobe can name one. The stack is taken at the caller and
        # the discovered function stays separate from that measured stack.
        rows = list(command_trace(app.prog, "findmnt"))
        entry = interface_row(rows, "m_show")
        check(
            entry is not None and "m_show" in entry.value,
            f"an ambiguous leaf still names the function: "
            f"{entry.value if entry else '(missing)'}",
        )
        added = next((r for r in rows if r.label.strip().endswith("m_show")), None)
        check(
            added is not None and "not probed" in added.why,
            "and marks the frame it could not probe",
        )
        check(
            added is not None and added.value.startswith("fs/namespace.c:"),
            f"resolved to the right one of the two: "
            f"{added.value if added else '(missing)'}",
        )

        # An empty stage 4 states why it is empty. vfs_statx reads a local
        # struct path, and a local says nothing about what the caller passed in,
        # so the scan reports none.
        rows = list(command_trace(app.prog, "ls -l /proc/1/ns"))
        reads = next(
            (
                r
                for r in rows
                if r.label.strip() == "vfs_statx" and "parameter references" in r.why
            ),
            None,
        )
        check(
            reads is not None and reads.value == "none found",
            f"an empty stage 4 says so plainly: "
            f"{reads.value if reads else '(missing)'}",
        )
        check(
            reads is not None and "parameter references" in reads.why,
            f"and gives the reason, not just the outcome: "
            f"{reads.why if reads else '(missing)'}",
        )

        # "the busiest" is meaningless without the tally it won: name the
        # interfaces and their counts, so the choice can be checked.
        rows = list(command_trace(app.prog, "ls -l /proc/1/fd"))
        entry = interface_row(rows, "vfs_statx")
        check(
            entry is not None and interface_row(rows, "iterate_dir") is not None,
            f"stat and directory interfaces each have their own counts: "
            f"{entry.why if entry else '(missing)'}",
        )

        # And a /proc file with no entry in the catalog resolves its leaf from
        # the seq_file the kernel is holding, rather than failing.
        rows = list(command_trace(app.prog, "cat /proc/loadavg"))
        entry = interface_row(rows, "loadavg_proc_show")
        check(
            entry is not None and "loadavg_proc_show" in entry.value,
            f"an untabled file resolves its leaf: "
            f"{entry.value if entry else '(missing)'}",
        )
        check(
            entry is not None and "leaf from the seq_file" in entry.why,
            "and says the leaf was measured, not looked up",
        )

        rows = list(command_trace(app.prog, "cat /proc/loadavg /proc/uptime"))
        for function in ("loadavg_proc_show", "uptime_proc_show"):
            item = interface_row(rows, function)
            check(
                item is not None and "matched to the current read" in item.why,
                f"{function} has a file-correlated serving stack",
            )

        rows = list(
            command_trace(
                app.prog,
                "ps -o psr= -p 1",
                origin="on_cpu",
                selected_field="task_struct.on_cpu",
            )
        )
        paths = {r.label.strip() for r in rows}
        check(
            {"/proc/<pid>/stat", "/proc/<pid>/status"} <= paths,
            "ps retains stat and status rather than choosing one by count",
        )
        selected = next(r for r in rows if r.label.strip() == "selected field")
        check(
            selected.value == "task_struct.on_cpu"
            and "not established" in selected.why,
            "the trace does not claim ps output proves an on_cpu field access",
        )

        # A command that never exits is capped rather than waited on, and says
        # so: vmstat -n 1 prints a line a second forever.
        trace_module = importlib.import_module("kexplore.operations.command_trace")
        run_command = trace_module.trace_command
        run_times = []

        def timed_run(*args, **kwargs):
            started = time.monotonic()
            result = run_command(*args, **kwargs)
            run_times.append(time.monotonic() - started)
            return result

        with patch.object(trace_module, "trace_command", side_effect=timed_run):
            rows = list(command_trace(app.prog, "vmstat -n 1"))
        elapsed = sum(run_times)
        note = next((r for r in rows if r.label.strip() == "note"), None)
        check(
            note is not None and "did not exit within" in note.why,
            f"a command that never exits is reported as capped: "
            f"{note.why if note else '(missing)'}",
        )
        check(
            elapsed < 45,
            f"the measured runs are capped independently of source downloads: {elapsed:.0f}s",
        )

        # Read counts alone rank the wrong file: grep reads its own
        # /proc/self/maps twice while starting and the file it was asked about
        # once. Both files must remain visible.
        rows = list(command_trace(app.prog, "grep VmPin /proc/1/status"))
        entry = interface_row(rows, "proc_pid_status")
        check(
            entry is not None and "proc_pid_status" in entry.value,
            f"the named file keeps its own evidence alongside other reads: "
            f"{entry.value if entry else '(missing)'}",
        )
        check(
            any(r.why == "source" for r in rows)
            or any(r.why == "catalog" for r in rows),
            "field rows keep their evidence labels",
        )
        check(
            (
                await asyncio.to_thread(
                    subprocess.run,
                    ["pgrep", "-x", "vmstat"],
                    capture_output=True,
                    check=False,
                )
            ).returncode
            != 0,
            "with nothing left running afterwards",
        )

        # An mm with no owning task has no /proc directory at all, so its rows
        # say that rather than showing a command that cannot be completed.
        while len(app.stack) > 1:
            app.action_back()
        await settle(app, pilot)
        open_entry(app, tree, "init_mm")
        await settle(app, pilot)
        app.action_follow()
        await settle(app, pilot)
        rows = app.stack[-1].rows
        unfilled = [r.type_name for r in rows if r.original_type]
        check(
            bool(unfilled) and all(u == UNFILLED["<pid>"] for u in unfilled),
            f"init_mm says why instead of showing <pid>: {set(unfilled)}",
        )
        table.move_cursor(row=rows.index(next(r for r in rows if r.original_type)))
        depth = len(app.stack)
        await pilot.press("t")
        await settle(app, pilot)
        check(len(app.stack) == depth, "and t refuses it rather than running it")

    print("\nPASS" if ok else "\nFAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

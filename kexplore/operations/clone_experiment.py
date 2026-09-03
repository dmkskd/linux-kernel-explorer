"""Controlled clone experiment: pass one flag at a time and look at the result.

Watching an existing process cannot attribute cause. pthread_create passes
CLONE_VM|CLONE_FILES|CLONE_FS|CLONE_SIGHAND|CLONE_THREAD in one call, so every
structure differs at once. This runs a helper that clones once per flag
combination, holds the children alive, reads their task_structs, and reports
which structure each flag actually changed.

Reading down a column shows which flag controls that structure. Reading across
a row shows what one flag combination buys, and what it costs.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

from drgn import Program

from ..core import experiment
from ..core.nav import Row
from .algorithm import Algorithm, Observation, register_algorithm

SOURCE = (
    Path(__file__).resolve().parent.parent.parent / "tests" / "helpers" / "clone_matrix.c"
)
BINARY = Path("/tmp/kexplore_clone_matrix")

FIELDS = ("mm", "files", "fs", "sighand", "signal", "nsproxy")

# Which member counts the sharers, per structure. The shapes are not uniform:
# atomic_t exposes .counter, refcount_t nests it under .refs, and fs_struct
# uses a plain int.
REFCOUNTS = {
    "mm": "mm_users",
    "files": "count",
    "fs": "users",
    "sighand": "count",
    "signal": "live",
    "nsproxy": "count",
}

# Fields that survive as a record of which flags were passed. The flags
# themselves are consumed by copy_process and never stored.
TRACES = (
    ("exit_signal", "-1 means CLONE_THREAD; otherwise the signal sent on exit"),
    ("group_leader", "the leader of this task's group; itself, if it is the leader"),
    ("set_child_tid", "set by CLONE_CHILD_SETTID"),
    ("clear_child_tid", "set by CLONE_CHILD_CLEARTID; the futex pthread_join waits on"),
    ("vfork_done", "non-NULL only while a vfork parent is suspended"),
)


def _sharers(obj, field: str) -> str:
    """Read a refcount, whatever shape it takes on this structure."""
    from ..core import ctypes as ct

    name = REFCOUNTS.get(field)
    if name is None or not obj.value_():
        return ""
    counter = ct.safe(lambda: obj.member_(name), None)
    if counter is None:
        return ""
    # atomic_t exposes .counter; refcount_t nests it under .refs; fs_struct
    # uses a plain int. Try each and take the first that yields a number.
    for path in (
        lambda c: c.counter,
        lambda c: c.refs.counter,
        lambda c: c,
    ):
        try:
            # A wrong path raises AttributeError, which ct.safe does not catch
            # because it is a programming error everywhere else.
            value = path(counter).value_()
        except Exception:  # noqa: BLE001 - trying shapes until one fits
            continue
        if isinstance(value, int):
            return f" ({value})"
    return ""


def _clone_matrix(prog: Program) -> Iterator[Observation]:
    from drgn.helpers.linux.pid import find_task

    from ..core import ctypes as ct

    # The field name is not the struct name: task->mm is a struct mm_struct *.
    # Take the type from DWARF rather than assuming it from the field.
    task_type = prog.type("struct task_struct")
    struct_of = {f: ct.type_name(task_type.member(f).type) for f in FIELDS}

    error = experiment.build(SOURCE, BINARY)
    if error:
        yield Observation("cannot build helper", cells=(f"gcc: {error}",), kind="result")
        return

    # The helper prints its timings, then one line per held child. Wait for the
    # last variant so every child exists before drgn looks.
    running = experiment.start(BINARY, ["120"], ready="READY", timeout=60)
    if running.error:
        yield Observation("helper failed", cells=(running.error,), kind="result")
        return

    try:
        parent_pid = 0
        costs: dict[str, int] = {}
        children: list[tuple[int, str]] = []
        for line in running.lines:
            parts = line.split(None, 1)
            if line.startswith("parent "):
                parent_pid = int(parts[1])
            elif line.startswith("COST "):
                _, low, mid, high, runs, name = line.split(None, 5)
                costs[name] = (int(low), int(mid), int(high), int(runs))
            elif parts and parts[0].isdigit():
                children.append((int(parts[0]), parts[1]))

        if not parent_pid or not children:
            yield Observation(
                "helper produced no children", cells=(str(running.lines[:3]),), kind="result"
            )
            return

        parent = find_task(prog, parent_pid)
        base = {f: parent.member_(f).value_() for f in FIELDS}
        held = {name for _pid, name in children}

        # Timed-only variants have no child to inspect; report the cost and say
        # why the structure columns are empty.
        for name, stats in costs.items():
            if name in held:
                continue
            low, mid, high, _runs = stats
            yield Observation(
                name,
                cells=(
                    name,
                    "new group",
                    *("-" for _ in FIELDS),
                    f"{low/1000:.1f} / {mid/1000:.1f} / {high/1000:.1f}",
                ),
                doc_for=(
                    "vfork suspends the parent until the child execs or exits, so "
                    "there is no moment when both exist to compare. It shares the "
                    "address space (CLONE_VM) and skips the page table copy "
                    "entirely, which is why posix_spawn uses it."
                ),
            )

        def build_detail(name: str, pid: int, child_task) -> list[Row]:
            """Build the drill-down now, while both tasks are still alive.

            This must not be lazy. The helper is killed when the analysis
            returns, so a closure that read the tasks at expand time would be
            reading freed slab memory and silently getting nothing.
            """
            rows = [
                Row(
                    "", None, "", "", False,
                    cells=("(pid)", "", str(parent_pid), str(pid), ""),
                    kind="derived",
                )
            ]
            for f in FIELDS:
                pv = base[f]
                cv = ct.safe(lambda: child_task.member_(f).value_(), 0)
                shared = pv == cv
                rows.append(
                    Row(
                        f, None, "", "", False,
                        cells=(
                            f"task->{f}",
                            struct_of[f],
                            f"{pv:#x}{_sharers(parent.member_(f), f)}",
                            f"{cv:#x}{_sharers(child_task.member_(f), f)}",
                            "shared" if shared else "new",
                        ),
                        doc=(
                            f"{name}: task->{f} is a {struct_of[f]}. The child's "
                            + ("equals" if shared else "differs from")
                            + " the parent's, so kernel_clone "
                            + ("reused it" if shared else "allocated a new one")
                            + ". The number in brackets is how many tasks share it."
                        ),
                    )
                )

            rows.append(Row("", None, "", "", False, cells=("", "", "", "", "")))
            for field_name, why in TRACES:
                if not task_type.has_member(field_name):
                    continue
                pv = ct.safe(lambda: parent.member_(field_name).value_(), None)
                cv = ct.safe(lambda: child_task.member_(field_name).value_(), None)
                if pv is None or cv is None:
                    continue
                fmt = str if field_name == "exit_signal" else (lambda v: f"{v:#x}")
                rows.append(
                    Row(
                        field_name, None, "", "", False,
                        cells=(
                            f"task->{field_name}",
                            ct.type_name(task_type.member(field_name).type),
                            fmt(pv),
                            fmt(cv),
                            "same" if pv == cv else "differs",
                        ),
                        doc=why,
                    )
                )
            return rows

        for pid, name in children:
            task = find_task(prog, pid)
            mine = {f: task.member_(f).value_() for f in FIELDS}
            cells = [name]
            # What userspace calls a thread is exactly this: same thread group.
            cells.append(
                "same group" if task.tgid == parent.tgid else "new group"
            )
            for f in FIELDS:
                value = task.member_(f).value_()
                cells.append("shared" if value == base[f] else "new")
            stats = costs.get(name)
            if stats and stats[0] > 0:
                low, mid, high, _runs = stats
                cells.append(f"{low/1000:.1f} / {mid/1000:.1f} / {high/1000:.1f}")
            else:
                cells.append("?")
            yield Observation(
                name,
                cells=tuple(cells),
                # Deliberately no obj: the helper is killed when this analysis
                # returns, so these task_structs are TASK_DEAD by the time a
                # row could be followed. Enter opens the captured comparison
                # instead, which is the only thing worth seeing here anyway.
                # Rows are built now and merely handed back later.
                expand=(lambda rows=build_detail(name, pid, task): rows),
                expand_columns=(
                    "field",
                    "points to",
                    "parent",
                    "this child",
                    "result",
                ),
                doc_for=(
                    f"pid {pid}, tgid {task.tgid.value_()} "
                    f"(parent tgid {parent.tgid.value_()}): "
                    + ", ".join(
                        f"{f}={task.member_(f).value_():#x}" for f in FIELDS
                    )
                    + f". shared means this pointer equals the parent's "
                    f"({', '.join(f'{f}={base[f]:#x}' for f in FIELDS)}), so "
                    f"kernel_clone reused that structure rather than allocating "
                    f"a new one."
                ),
            )
    finally:
        running.stop()



CLONE_MATRIX = register_algorithm(
    Algorithm(
        key="clone_matrix",
        label="what each clone flag actually does",
        subsystem="process",
        rule=(
            "Runs a helper that clones once per flag combination and holds the "
            "children alive, then compares each child's structure pointers against "
            "the parent's. Takes a few seconds; nothing is left running."
        ),
        doc="Controlled experiment: one clone flag at a time, with its cost.",
        analyse=_clone_matrix,
        columns=(
            "clone flags passed",
            "thread group",
            "mm",
            "files",
            "fs",
            "sighand",
            "signal",
            "nsproxy",
            "clone() us  min/med/p90",
        ),
            background=True,
        )
)


def _cow_after_fork(prog: Program) -> Iterator[Observation]:
    """Count pages a forked child still shares with its parent.

    Uses the same helper as the flag matrix rather than an arbitrary process:
    an unrelated parent may have exec'd, may have written most of its pages
    already, or may not exist at all, none of which is reproducible.
    """
    from drgn.helpers.linux.mm import follow_page, for_each_vma
    from drgn.helpers.linux.pid import find_task

    from ..core import ctypes as ct

    PAGES_PER_VMA = 256

    error = experiment.build(SOURCE, BINARY)
    if error:
        yield Observation("cannot build helper", cells=(f"gcc: {error}",), kind="result")
        return

    # 64 MiB of dirtied anonymous memory, so there is something to share.
    running = experiment.start(BINARY, ["120", "64"], ready="READY", timeout=90)
    if running.error:
        yield Observation("helper failed", cells=(running.error,), kind="result")
        return

    try:
        parent_pid = 0
        child_pid = 0
        for line in running.lines:
            if line.startswith("parent "):
                parent_pid = int(line.split()[1])
            elif line.endswith("SIGCHLD (plain fork)"):
                # The COST line ends with the same name; only the held-child
                # line starts with a pid.
                first = line.split()[0]
                if first.isdigit():
                    child_pid = int(first)
        if not parent_pid or not child_pid:
            yield Observation("no fork child", cells=("helper output unexpected",), kind="result")
            return

        parent, child = find_task(prog, parent_pid), find_task(prog, child_pid)

        def page_at(mm, addr):
            return ct.safe(lambda: follow_page(mm, addr).value_(), None)

        same = different = parent_only = 0
        for vma in for_each_vma(parent.mm):
            start = vma.vm_start.value_()
            end = min(vma.vm_end.value_(), start + PAGES_PER_VMA * 4096)
            for addr in range(start, end, 4096):
                p, c = page_at(parent.mm, addr), page_at(child.mm, addr)
                if p and c:
                    if p == c:
                        same += 1
                    else:
                        different += 1
                elif p:
                    parent_only += 1

        yield Observation(
            "mm_struct",
            cells=("mm_struct", "different", f"parent {parent.mm.value_():#x}, child {child.mm.value_():#x}"),
            doc_for="fork always allocates a new mm_struct; the pages behind it are what is shared.",
        )
        yield Observation(
            "same page",
            cells=("pages at the same physical page", str(same), "still shared: neither side has written"),
        )
        yield Observation(
            "different page",
            cells=("pages at a different physical page", str(different), "one side wrote, so the fault handler copied it"),
        )
        yield Observation(
            "parent only",
            cells=("resident only in the parent", str(parent_only), "faulted in after the fork, so the child never saw it"),
        )
    finally:
        running.stop()


COW = register_algorithm(
    Algorithm(
        key="cow_after_fork",
        label="copy-on-write after fork",
        subsystem="mm",
        rule=(
            "fork gives the child its own mm_struct, but the page tables point at "
            "the parent's physical pages until one side writes. Runs the clone "
            "helper with 64 MiB of dirtied memory and compares the two address "
            "spaces page by page."
        ),
        doc="Counts pages a forked child still shares with its parent.",
        analyse=_cow_after_fork,
        columns=("what", "count", "meaning"),
        background=True,
    )
)

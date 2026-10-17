"""The experiments tab: start and stop a workload, and open the catalog entries that show its sockets and skbs.

An experiment, written to be removable; ``operations/workloads.py`` lists every
place the rest of the explorer refers to it. ``frames`` is imported inside the
functions for the reason given in ``procfile.py``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from rich.text import Text

from ..catalog.registry import subsystems
from ..core import ctypes as ct
from ..core.nav import Row
from ..operations import workloads
from ..operations.workloads import Workload

COLUMNS = ("row", "description")
DOC = ("Each experiment is a program that kexplore runs as a child process. The "
       "program creates a condition in the running kernel, for example a full accept "
       "queue.")
# The description of each sidebar branch, by its path.
BRANCH_DOCS = {
    ("net",): ("Modes of tests/helpers/traffic.py: sockets on 127.0.0.1, AF_UNIX "
               "socketpairs, and a netem root qdisc on lo."),
    ("net", "TCP"): "TCP connections on 127.0.0.1, ports 40100 to 40601.",
    ("net", "UDP"): "A UDP socket bound to 127.0.0.1:40700.",
    ("net", "AF_UNIX"): "socketpair() pairs of type SOCK_STREAM and SOCK_DGRAM.",
    ("net", "qdisc"): "A netem root qdisc on lo with 1 s latency.",
}
RUNNING_MARK = "● "


@dataclass(frozen=True)
class Action:
    """The item of a pane's start/stop row. plan_for returns None for it; the
    app's follow acts on it through ``action_of``."""

    workload: Workload


def build_tree(tree) -> None:
    from . import frames

    tree.root.set_label("experiments")
    branches = {(): tree.root}
    contents: dict[tuple[str, ...], list] = {(): []}
    for workload in workloads.WORKLOADS:
        for depth in range(1, len(workload.group) + 1):
            path = workload.group[:depth]
            if path not in branches:
                branches[path] = branches[path[:-1]].add(path[-1], expand=True)
                contents[path] = []
                contents[path[:-1]].append(path)
        branches[workload.group].add_leaf(_label(workload), data=workload)
        contents[workload.group].append(workload)
    # A heading lists what is under it: its workloads, and its sub-branches as
    # the listings they open.
    listings = {}
    for path in sorted(contents, key=len, reverse=True):
        items = [listings[i] if isinstance(i, tuple) else i for i in contents[path]]
        label = path[-1] if path else "experiments"
        listings[path] = frames.Listing(label, BRANCH_DOCS.get(path, DOC), tuple(items))
        branches[path].data = listings[path]


def _label(workload: Workload) -> str:
    return (RUNNING_MARK if workloads.running(workload) else "") + workload.label


def action_of(row) -> Workload | None:
    """The workload a start/stop row acts on, or None for any other row."""
    item = getattr(row, "item", None)
    return item.workload if isinstance(item, Action) else None


def toggle(workload: Workload, tree) -> str:
    """Start or stop it, update its sidebar label, and say which happened."""
    workloads.toggle(workload)
    pending = list(tree.root.children)
    while pending:
        node = pending.pop()
        if node.data is workload:
            node.set_label(_label(workload))
        pending.extend(node.children)
    return f"{'started' if workloads.running(workload) else 'stopped'} {workload.label}"


def plan_for(item, ctx):
    from . import frames

    if not isinstance(item, Workload):
        return None
    return frames.Plan(workloads.title(item), item.doc, COLUMNS, lambda: frame(ctx, item))


def frame(ctx, workload: Workload):
    """The pane: what Enter does on each row is shown by its mark.

    ``▸`` acts (start or stop), ``→`` opens a catalog entry, and a row with
    no mark is text. A heading names each group.
    """
    from . import frames

    def make_rows() -> list[Row]:
        running = workloads.running(workload)
        verb = "stop" if running else "start"
        rows = [
            Row(verb, None, f"Enter {verb}s the workload", "", True,
                kind="derived", item=Action(workload)),
            _heading("inspect", "Enter opens the entry"),
            *(_entry_row(subsystem, key, note) for subsystem, key, note in workload.look),
            _heading("state", ""),
            _text("status", workloads.status(workload)),
        ]
        warning = _namespace_warning(ctx)
        if warning:
            rows.append(Row(warning, None, "", "", False, kind="error", depth=1))
        lines = workloads.output(workload)
        rows.extend(_text("output" if index == 0 else "", line)
                    for index, line in enumerate(lines))
        return rows

    return frames.Frame(workloads.title(workload), make_rows, doc=workload.doc, columns=COLUMNS)


def _heading(name: str, detail: str) -> Row:
    return Row(name, None, detail, "", False, kind="derived")


def paint(row: Row, cells: list[Text]) -> list[Text]:
    """Style each row of this pane by its role.

    The explorer colours rows by kind. In this pane every row except the links
    is ``derived``. This function styles them by item and depth instead.
    """
    if action_of(row) is not None:
        running = row.name == "stop"
        label = " ■ stop " if running else " ▶ start "
        style = "bold white on dark_red" if running else "bold white on dark_green"
        return [Text(label, style=style), Text(cells[1].plain, style="dim")]
    if row.kind == "derived" and row.depth == 0:
        return [Text(row.name.upper(), style="bold"), Text(cells[1].plain, style="dim italic")]
    if row.kind == "link":
        cells[0].stylize("bold blue")
        return cells
    if row.name == "status":
        cells[1].stylize("green" if cells[1].plain.startswith("running") else "dim")
    cells[0].stylize("dim")
    return cells


def _text(name: str, value: str) -> Row:
    return Row(name, None, value, "", False, kind="derived", depth=1)


def _entry_row(subsystem_key: str, entry_key: str, note: str) -> Row:
    """A row that opens the catalog entry, as a listing row does."""
    subsystem = next((s for s in subsystems() if s.key == subsystem_key), None)
    entry = next((e for e in subsystem.entries if e.key == entry_key), None) if subsystem else None
    if entry is None:
        return Row(f"no entry {subsystem_key}/{entry_key}", None, "", "", False,
                   kind="error", depth=1)
    return Row(f"{subsystem.label} › {entry.label}", None, note, "", True, kind="link",
               doc=entry.doc, depth=1, item=entry)


def _namespace_warning(ctx) -> str:
    """A warning when the children's sockets are not in the tables the entries read.

    The socket entries read init_net. The children share the explorer's network
    namespace, which is not init_net when the explorer runs in a container.
    """
    if ctx is None or not ctx.live:
        return "a vmcore is a static image; workloads change only a live kernel"
    init = ct.safe(lambda: ctx.prog["init_net"].ns.inum.value_(), None)
    try:
        own = os.stat("/proc/self/ns/net").st_ino
    except OSError:
        return ""
    if init is not None and init != own:
        return ("kexplore runs outside init_net, and the socket entries read only "
                "init_net's tables, which exclude these sockets")
    return ""

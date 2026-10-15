"""A pseudo-file as rows: each line, and the member it is read from.

An experiment, written to be removable. Its code is in this module and in
``catalog/pseudofs.py``; the rest of the explorer refers to it in three marked
lines in ``frames.py``:

* the import of this module,
* ``procfile.task_rows(target)`` in ``object_frame``, which adds a row for the
  task's /proc directory,
* ``procfile.plan_for(item, ctx)`` in ``frames.plan_for``, which opens the
  item that row leads to,

and in four places in ``tui/app.py``: the tab, the branch of ``build_tree``
that fills the sidebar from this module, the node-expanded handler that lists
a directory, and the tab in the cycling order.

Deleting this module, ``catalog/pseudofs.py`` and ``tests/test_pseudofs.py``,
and removing those marked lines, restores the explorer to its state without
the experiment.

``frames`` is imported inside the functions, not at module level, because it
imports this module: the circular import succeeds at call time and fails at
import time. Importing it locally also keeps the dependency one-directional,
so removing the experiment requires only deletions.
"""

from __future__ import annotations

from dataclasses import dataclass

from rich.text import Text

from drgn import Object
from drgn.helpers.linux.pid import find_task

from ..catalog import pseudofs
from ..core import ctypes as ct
from ..core.nav import MAX_ROWS, Row

# A pseudo-file is read as text, so its rows are the file's lines, each with
# the member it is read from.
COLUMNS = ("line", "value", "read from")
# A directory lists its entries; a file with no line table is shown as text,
# with the same three columns, so the column layout does not change between
# the two.
DIR_COLUMNS = ("name", "kind", "what it is")
TEXT_COLUMNS = ("line", "name", "value")


def task_rows(target: Object) -> list[Row]:
    """The task's /proc directory, as a row that opens it.

    Only a task gets this row. It is an index row, not a link, because it
    opens files the kernel publishes, not another struct.
    """
    if ct.tag_of(target.type_) != "task_struct":
        return []
    pid = ct.safe(lambda: target.pid.value_(), None)
    if not pid:
        return []
    directory = pseudofs.directory_for(pid)
    return [
        Row(directory.path, None, "procfs",
            "what the kernel publishes about this task, as text",
            True, kind="link", doc=directory.doc, item=directory)
    ]


def plan_for(item, ctx):
    """The plan for a directory or a file, or None when this is neither.

    ``frames.plan_for`` calls this first and, on None, continues with its own
    dispatch, so it remains the only dispatch point.
    """
    from . import frames

    if isinstance(item, pseudofs.Directory):
        return frames.Plan(item.label, item.doc, DIR_COLUMNS,
                           lambda: directory_frame(ctx, item))
    if isinstance(item, pseudofs.FileItem):
        if item.spec is None:
            return frames.Plan(item.label, item.doc, TEXT_COLUMNS,
                               lambda: text_frame(ctx, item))
        return frames.Plan(item.label, item.doc, COLUMNS,
                           lambda: file_frame(ctx, item))
    return None


def file_frame(ctx, item: pseudofs.FileItem):
    """A pseudo-file as one row per line, in the order the file prints them.

    The values are those in the file; none is read from kernel memory. A row
    shows where the kernel reads the value, not whether a new read of the file
    would print the same value.
    """
    from . import frames

    def make_rows() -> list[Row]:
        try:
            lines = pseudofs.read_file(item.spec, item.path)
        except OSError as exc:
            return [Row(f"{item.path}: {exc.strerror or exc}", None, "", "",
                        False, kind="error")]
        if not lines:
            return [Row("(empty)", None, "", "", False, kind="derived")]
        rows = [_line_row(ctx, item, line) for line in lines]
        rows.append(_raw_row(item))
        return rows

    return frames.Frame(item.label, make_rows, doc=item.doc, columns=COLUMNS)


def _raw_row(item: pseudofs.FileItem) -> Row:
    """The file as cat prints it, in a collapsed row.

    The annotated rows are an interpretation of the file. The raw text is on
    the same screen so the two can be compared, and is collapsed by default so
    that it does not double the length of every view.
    """
    def raw() -> list[Row]:
        try:
            text = pseudofs.contents(item.path)
        except OSError as exc:
            return [Row("", None, "", "", False, kind="error",
                        cells=("", str(exc.strerror or exc), ""))]
        return [
            Row(f"raw {number}", None, "", line, False, kind="derived",
                cells=("", line, ""))
            for number, line in enumerate(text.splitlines()[:MAX_ROWS], 1)
        ] or [Row("", None, "", "", False, kind="derived",
                  cells=("", "(empty)", ""))]

    return Row(f"cat {item.path}", None, "", "", False, kind="derived",
               cells=(f"cat {item.path}", "", ""),
               doc="the file as it is, to read against the annotation",
               expand=raw)


def _line_row(ctx, item: pseudofs.FileItem, line: pseudofs.Line) -> Row:
    """One line of the file, with the member it is picked up from."""
    origin = line.origin
    if origin is None and item.spec.repeating:
        # Every line represents one object, so all lines have the same
        # columns, and the expansion names each column.
        return Row(line.label, None, "", line.value, False, kind="field",
                   cells=(line.label, line.value, _walk_path(item.spec)),
                   doc=item.spec.doc,
                   expand=lambda: _column_rows(ctx, item.spec),
                   item=Jump(_pid_of(item.path), index=int(line.label) - 1))
    if origin is None:
        return Row(line.label, None, "", line.value, False, kind="derived",
                   cells=(line.label, line.value, ""),
                   doc="this table does not claim this line")
    return Row(
        line.label, None, "", line.value, False,
        kind="field" if origin.member else "derived",
        cells=(line.label, line.value, origin.path(_root_name(item.spec))),
        doc=origin.doc,
        expand=lambda: _origin_rows(ctx, item.spec, origin),
        item=Jump(_pid_of(item.path), origin=origin),
    )


def _pid_of(path: str) -> int:
    """The pid whose directory this file is in, or 0."""
    parts = path.split("/")
    return int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0


@dataclass(frozen=True)
class Jump:
    """The location of a line's value, as an object the explorer can open.

    Stored on the row, so the key that opens it needs no lookup table. The row
    is not followable, so Enter does not open it.

    ``index`` is set for a file with one line per object: the line is the nth
    object, which is reached by a walk instead of a member read.
    """

    pid: int
    origin: pseudofs.Origin | None = None
    index: int | None = None


def jump_target(ctx, row) -> tuple[str, Object] | None:
    """The struct a line is read from, resolved now against the live kernel.

    Returns None, without raising, when the row is not a pseudo-file line, the
    task has exited, or a pointer on the path is NULL; the caller reports it.
    """
    jump = getattr(row, "item", None)
    if not isinstance(jump, Jump):
        return None
    if jump.origin is not None and jump.origin.symbol:
        try:
            return jump.origin.symbol, ctx.prog[jump.origin.symbol]
        except KeyError:
            return None
    if not jump.pid:
        return None
    task = find_task(ctx.prog, jump.pid)
    if not task:
        return None
    if jump.index is not None:
        return _nth_vma(task, jump.index)
    if jump.origin is None:
        return None
    obj, label = task, "task_struct"
    for hop in jump.origin.hops:
        # A hop written with a leading dot is an embedded struct. The member is
        # read by name in both cases; the dot records only the access syntax.
        name = hop.lstrip(".")
        try:
            obj = obj.member_(name)
        except LookupError:
            return None
        if not hop.startswith(".") and not obj:
            return None
        label = name
    return label, obj


def _nth_vma(task: Object, index: int) -> tuple[str, Object] | None:
    """The mapping a maps line represents, found by its position in the file.

    maps prints the vmas in address order, which is the order the walk
    produces, so the nth line is the nth vma.
    """
    from drgn.helpers.linux.mm import for_each_vma

    if not task.mm:
        return None
    for position, vma in enumerate(for_each_vma(task.mm)):
        if position == index:
            return "vm_area_struct", vma
    return None


def _split(line: str) -> tuple[str, str]:
    """A line as name and value, when it is written that way.

    Most /proc files print "name: value", padded into columns. Kept in one
    cell, the value would be the truncated end of the line. A line with no
    colon is kept whole in the first column.
    """
    name, separator, value = line.partition(":")
    label = name.strip()
    # One token before the colon is a label. Several tokens mean the colon is
    # part of the content: a maps line contains the device as "00:23", and
    # splitting there would make the address range, permissions and offset
    # the label.
    if not separator or not label or len(label.split()) != 1:
        return line, ""
    return label, " ".join(value.split())


def _origin_rows(ctx, spec: pseudofs.PseudoFile,
                 origin: pseudofs.Origin) -> list[Row]:
    """One line's path expanded: each pointer followed, and its target type.

    The names come from the table and the types from the running kernel's
    debug information, so the expansion shows the kernel's types, not those
    the table assumed.
    """
    # Indented here, not by the frontend, which indents rows that represent
    # kernel objects; these rows represent steps of a path. Without the prefix,
    # an expanded hop looks like another line of the file.
    steps = pseudofs.hop_types(ctx.prog, spec, origin)
    rows = [
        Row(name, None, type_name, "", False, kind="derived",
            cells=(f"   {'└' if last else '├'} {name}", type_name, ""))
        for (name, type_name), last in _with_last(steps)
    ]
    rows.append(
        Row("printed by", None, "", "", False, kind="derived",
            cells=("     printed by", f"{spec.function}()", spec.defined_in))
    )
    return rows


def _column_rows(ctx, spec: pseudofs.PseudoFile) -> list[Row]:
    """How the line's object is reached, then the columns read from it."""
    rows = [
        Row(name, None, "", "", False, kind="derived",
            cells=(f"   \u251c {name}", type_name, ""))
        for name, type_name in pseudofs.line_path(spec)
    ]
    for origin, last in _with_last(list(spec.origins)):
        steps = pseudofs.hop_types(ctx.prog, spec, origin)
        rows.append(
            Row(origin.label, None, "", "", False, kind="derived",
                cells=(f"   {'└' if last else '├'} {origin.label}",
                       origin.path(_short(spec.line_root)),
                       steps[-1][1] if steps else ""),
                doc=origin.doc)
        )
    return rows


def _walk_path(spec: pseudofs.PseudoFile) -> str:
    """The expression that reaches one line's object, as C with a walk.

    A line of a repeating file is one object of a collection, not a member, so
    the column shows the collection and the walk.
    """
    chain = _root_name(spec) or spec.root
    for hop in spec.line_hops:
        chain += "->" + hop.lstrip(".")
    return f"{chain}->{spec.line_walk}" if spec.line_walk else chain


def _root_name(spec: pseudofs.PseudoFile) -> str:
    """The name of the root object in a path, or an empty string for a global."""
    return "task" if spec.root == "task_struct" else ""


def _short(tag: str) -> str:
    """What a struct is called in a path: vm_area_struct is vma everywhere."""
    return "vma" if tag == "vm_area_struct" else (tag or "obj")


def _with_last(items: list) -> list[tuple[object, bool]]:
    """Each item, and whether it is the last one."""
    return [(item, index == len(items) - 1) for index, item in enumerate(items)]


# ------------------------------------------------------------- the sidebar


def build_tree(tree) -> None:
    """Fill the sidebar with the pseudo-filesystems, one root each.

    Only the roots are added here. A directory is read when it is expanded,
    because /proc holds one directory per task and reading all of them before
    any is expanded would read hundreds of directories that are not displayed.
    """
    tree.root.set_label("filesystems")
    tree.root.data = pseudofs.ROOT
    for root in (pseudofs.ROOT,):
        node = tree.root.add(root.path, data=root)
        fill(node)
        node.expand()


def fill(node) -> bool:
    """Add a directory's children to its tree node. True when it did.

    Called when a node is expanded. Returns False for a node that is not a
    pseudo-filesystem directory or is already filled, so the handler in the
    app is one line and contains no /proc logic.
    """
    item = getattr(node, "data", None)
    if not isinstance(item, pseudofs.Directory) or node.children:
        return False
    try:
        entries = pseudofs.listing(item.path)
    except OSError as exc:
        node.add_leaf(f"({exc.strerror or exc})")
        return True
    for entry in entries[:MAX_ROWS]:
        if entry.is_dir and not entry.link_target:
            node.add(_tree_label(entry), data=pseudofs.Directory(entry.path))
        else:
            node.add_leaf(_tree_label(entry), data=pseudofs.FileItem(entry.path))
    if len(entries) > MAX_ROWS:
        node.add_leaf(f"… {len(entries) - MAX_ROWS} more")
    return True


# The colour of a file with a line table, and of a directory containing one:
# the cyan the explorer uses for the rows it adds to a struct. Files shown
# only as text keep the default colour.
ANNOTATED = "cyan"


def _name(entry: pseudofs.Node) -> str:
    """The name, and for a task its command.

    A directory named with digits is a pid. Reading its comm costs one small
    file read and shows each pid with its command.
    """
    if entry.is_dir and entry.name.isdigit():
        comm = pseudofs.comm_of(entry.path)
        return f"{entry.name}  {comm}" if comm else entry.name
    return entry.name


def _tree_label(entry: pseudofs.Node):
    """The name, coloured when a line table is written for it."""
    marked = (pseudofs.holds_annotated(entry.path) if entry.is_dir
              else pseudofs.annotates(entry.path))
    return Text(_name(entry), style=ANNOTATED) if marked else _name(entry)


def _entry_row(entry: pseudofs.Node) -> Row:
    """One directory entry: a folder to open, a file, or an annotated file.

    The kind determines the colour. A directory is a link, like a curated
    link out of a struct, because opening it is the same action. A file with a
    line table has the colour of the rows the explorer adds, and a file without
    one has the default colour.
    """
    if entry.is_dir:
        kind = "link"
        name = _name(entry) + "/"
    elif pseudofs.annotates(entry.path):
        kind = "derived"
        name = _name(entry)
    else:
        kind = "field"
        name = _name(entry)
    if entry.link_target:
        what = "link"
    elif entry.is_dir:
        what = "dir"
    else:
        what = "file"
    return Row(entry.name, None, "", "", True, kind=kind,
               cells=(name, what, entry.doc),
               doc=entry.doc,
               item=(pseudofs.Directory(entry.path)
                     if entry.is_dir and not entry.link_target
                     else pseudofs.FileItem(entry.path)))


def parent_plan(ctx, label: str):
    """The plan for the directory above ``label``, or None.

    The frames here are named by their path, so the parent is the path with
    its last component removed. /proc itself has no parent listed.
    """
    if not label.startswith("/proc") or label.rstrip("/") == "/proc":
        return None
    parent = label.rstrip("/").rsplit("/", 1)[0]
    return plan_for(pseudofs.Directory(parent), ctx)


def directory_frame(ctx, item: pseudofs.Directory):
    """What a directory holds, one row per entry."""
    from . import frames

    def make_rows() -> list[Row]:
        try:
            entries = pseudofs.listing(item.path)
        except OSError as exc:
            return [Row(f"{item.path}: {exc.strerror or exc}", None, "", "",
                        False, kind="error")]
        rows = [_entry_row(entry) for entry in entries[:MAX_ROWS]]
        if len(entries) > MAX_ROWS:
            rows.append(Row(f"… {len(entries) - MAX_ROWS} more", None, "", "",
                            False, kind="truncated"))
        return rows

    return frames.Frame(item.label, make_rows, doc=item.doc,
                        columns=DIR_COLUMNS)


def text_frame(ctx, item: pseudofs.FileItem):
    """A file with no line table, shown as the text it is."""
    from . import frames

    def make_rows() -> list[Row]:
        try:
            text = pseudofs.contents(item.path)
        except OSError as exc:
            return [Row(f"not read: {exc.strerror or exc}", None, "", "",
                        False, kind="error",
                        cells=("not read", str(exc.strerror or exc), ""))]
        lines = text.splitlines()
        rows = [
            Row(str(number), None, "", line, False, kind="derived",
                cells=(str(number), *_split(line)))
            for number, line in enumerate(lines[:MAX_ROWS], 1)
        ]
        if not rows:
            return [Row("(empty)", None, "", "", False, kind="derived",
                        cells=("(empty)", "", ""))]
        if len(lines) > MAX_ROWS:
            rows.append(Row(f"… {len(lines) - MAX_ROWS} more lines", None, "",
                            "", False, kind="truncated"))
        return rows

    return frames.Frame(item.label, make_rows, doc=item.doc,
                        columns=TEXT_COLUMNS)

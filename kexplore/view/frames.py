"""Turn a catalog item into the table that represents it.

Every view in the explorer has the same parts: a label, a line of
documentation, some columns, and rows. So this module has one job: given
something from the catalog or from ``operations``, produce the :class:`Frame`
that shows it.

Nothing here imports a UI toolkit. That is deliberate: this is the layer a
second frontend would reuse, and it is what the crawl test drives instead of
opening a terminal. What the frontend still owns is *when* to build: a
:class:`Plan` says whether a frame can be built inline or has to go to a worker
first, because resolving one can mean running pahole over a 700MB vmlinux,
fetching source through debuginfod, or holding a bpftrace program open for ten
seconds.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Callable

from drgn import Object, Program, TypeKind

from ..catalog.decoders import decode_field
from ..catalog.links import Derived, Link, derived_for, links_for, userspace_for
from ..catalog.registry import Entry, FactEntry, Measurement, Subsystem
from ..catalog.userspace import entry_command, field_command
from ..core import ctypes as ct
from ..core import debuginfod
from ..core.nav import Row, collect, collection_rows, rows_for
from ..core.source import KernelSource
from ..operations.algorithm import Algorithm
from ..operations.walkthrough import Step, Walkthrough

# Struct views show field/type/value; the other views name their own columns.
FIELD_COLUMNS = ("field", "type", "value", "offset+size")
MEASURE_COLUMNS = ("bucket / key", "count", "distribution")
STEP_COLUMNS = ("step", "source", "what happens")
SOURCE_COLUMNS = ("line", "", "source")
LISTING_COLUMNS = ("entry", "what it shows")

# What a branch of the sidebar is, when the branch is not itself a catalog
# item. The tree groups entries by ``entry.group``; this says what that group
# holds, so landing on the heading explains it rather than showing nothing.
GROUP_DOCS = {
    "measure": "Tracers run in the foreground for a few seconds. Nothing is "
               "measured until one is opened.",
}


def _sort_key(cell: str) -> tuple[int, float, str]:
    """Order a cell as a number when it is one, and as text otherwise.

    A pid column sorted as text puts 10 before 2, and an address column sorted
    as text is ordered by its digits rather than by where it points. Numbers
    sort before text, so a blank cell (no thread count, no group) collects at
    one end rather than among the values.
    """
    text = cell.strip()
    try:
        return (0, float(int(text, 16) if text.startswith("0x") else float(text)), "")
    except ValueError:
        return (1, 0.0, text.lower())


def sort_rows(rows: list[Row], column: int, reverse: bool) -> list[Row]:
    """Order the rows that stand for objects, leaving the others where they are.

    "… truncated" and "(empty)" say something about the list as a whole, so
    they stay at the end instead of being sorted into it.
    """
    listed = [row for row in rows if row.marked and row.cells is not None]
    rest = [row for row in rows if not (row.marked and row.cells is not None)]
    listed.sort(
        key=lambda row: _sort_key(row.cells[column] if column < len(row.cells) else ""),
        reverse=reverse,
    )
    return listed + rest


@dataclass
class Frame:
    """One level of the navigation stack."""

    label: str
    make_rows: Callable[[], list[Row]]
    obj: Object | None = None
    doc: str = ""
    rows: list[Row] = field(default_factory=list)
    columns: tuple[str, ...] = FIELD_COLUMNS
    # Which column the rows are ordered by, None being the order the walk
    # produced them in. Sorting rebuilds the frame, so any row opened in place
    # closes: a child sorted away from its parent belongs to nothing.
    sort_column: int | None = None
    sort_reverse: bool = False

    def load(self) -> None:
        self.rows = self.make_rows()
        if self.sort_column is not None:
            self.rows = sort_rows(self.rows, self.sort_column, self.sort_reverse)


@dataclass
class Context:
    """What every builder needs: the kernel, its source, and the display mode.

    Held by the frontend and read at row-build time, so toggling ``userspace``
    and reloading a frame is enough to swap the kernel origins for the commands
    that get the same information from userspace.
    """

    prog: Program
    source: KernelSource
    userspace: bool = False


@dataclass(frozen=True)
class Plan:
    """How to open one item: the frame, and whether it can be built inline.

    ``activity`` is what to say while a slow build runs; when it is empty the
    build is cheap and the frontend just calls ``build()``. ``placeholder`` is
    what to show meanwhile -- a measurement keeps its own definition on screen
    while the tracer runs, rather than replacing the view with a spinner.
    """

    label: str
    doc: str
    columns: tuple[str, ...]
    build: Callable[[], Frame]
    activity: str = ""
    placeholder: tuple[Row, ...] = ()

    @property
    def deferred(self) -> bool:
        return bool(self.activity)

    def waiting_rows(self) -> list[Row]:
        if self.placeholder:
            return list(self.placeholder)
        return [Row(self.activity, None, "", "", False, kind="derived")]


# --------------------------------------------------------------------- rows


def _decoded(parent: Object, row: Row) -> Row:
    """Annotate a field with its human-readable meaning, if we know one.

    The raw value is kept alongside the decoded text -- these tables are
    hand-maintained, so the number stays visible to check against.
    """
    if row.obj is None or row.kind != "field":
        return row
    result = decode_field(parent, row.name, row.obj)
    if result is None:
        return row
    text, doc = result
    # "=" not "→": this is the same value spelled differently, not a change.
    # The arrow is already the marker for a navigable link.
    return replace(row, value=f"{row.value}  = {text}", doc=doc)


def _with_userspace(parent: Object, row: Row) -> Row:
    """Replace a field's type with how to read it from userspace, if possible.

    Most fields have no equivalent; those keep their type, because claiming an
    equivalent that does not exist would be worse than saying nothing.
    """
    if row.kind != "field":
        return row
    tag = ct.tag_of(parent.type_)
    if tag is None:
        return row
    pid = ct.safe(lambda: parent.pid.value_(), None) if tag == "task_struct" else None
    command = field_command(tag, row.name, pid)
    if not command:
        return row
    return replace(row, type_name=command, original_type=row.type_name)


def _link_row(link: Link, obj: Object, userspace: bool) -> Row:
    """A curated relationship, rendered as a followable row.

    In userspace mode the origin column is replaced by the command that gets
    the same information on a box without this tool.
    """
    return Row(
        name=link.label,
        obj=None,
        type_name=userspace_for(link, obj) if userspace else link.origin,
        value="",
        followable=True,
        kind="link",
        doc=link.doc,
        expand=lambda: collection_rows(collect(link.label, lambda: link.resolve(obj))),
    )


def _derived_row(derived: Derived, obj: Object) -> Row:
    """A computed value: read-only, shown above the real fields."""
    return Row(
        name=derived.label,
        obj=None,
        type_name="",
        value=ct.safe(lambda: str(derived.compute(obj))),
        followable=False,
        kind="derived",
        doc=derived.doc,
    )


# ------------------------------------------------------------------- frames


def object_frame(label: str, obj: Object, ctx: Context | None = None, doc: str = "") -> Frame:
    """A struct's computed values, its curated links, then its real fields."""
    # Resolvers and decoders get a pointer to the struct: drgn's Linux helpers
    # require one, and member access behaves identically through it. If obj is
    # already a pointer, use it as-is -- taking address_of_() again would make
    # a double pointer that still reports the same tag, so the mistake would
    # only surface later as a missing member.
    if ct.strip(obj.type_).kind == TypeKind.POINTER:
        target = obj
    else:
        target = obj.address_of_() if obj.address_ is not None else obj

    # A NULL pointer has no struct behind it to compute against.
    empty = ct.strip(target.type_).kind == TypeKind.POINTER and not ct.safe(
        target.value_, 0
    )

    def make_rows() -> list[Row]:
        if empty:
            return [Row("(NULL)", None, ct.type_name(obj.type_), "", False)]
        userspace = ctx.userspace if ctx is not None else False
        computed = [_derived_row(d, target) for d in derived_for(obj)]
        links = [
            _link_row(link, target, userspace)
            for link in links_for(obj)
            if link.visible(target)
        ]
        fields = [_decoded(target, row) for row in rows_for(obj)]
        if userspace:
            fields = [_with_userspace(target, row) for row in fields]
        return computed + links + fields

    return Frame(label, make_rows, obj=obj, doc=doc)


def entry_frame(ctx: Context, entry: Entry, subsystem_key: str = "") -> Frame:
    """A frame listing whatever an entry's provider produced."""
    doc = entry.doc
    if ctx.userspace:
        command = entry_command(subsystem_key, entry.key)
        if command:
            doc = f"from userspace:  {command}"

    return Frame(
        entry.label,
        lambda: collection_rows(entry.resolve(ctx.prog)),
        doc=doc,
        columns=entry.columns or FIELD_COLUMNS,
    )


def fact_frame(ctx: Context, entry: FactEntry) -> Frame:
    """A frame of computed answers rather than kernel objects."""

    def make_rows() -> list[Row]:
        return [
            Row(
                name=fact.label,
                obj=None,
                type_name="",
                value=fact.value,
                followable=False,
                kind="error" if fact.failed else "derived",
                doc=fact.evidence,
            )
            for fact in entry.resolve(ctx.prog)
        ]

    return Frame(entry.label, make_rows, doc=entry.doc)


def algorithm_frame(ctx: Context, algorithm: Algorithm) -> Frame:
    """The rule an algorithm applies, its inputs, and the outcome."""

    def make_rows() -> list[Row]:
        # The rule belongs above the table, not in a row: it applies to all of
        # them and would otherwise be repeated or truncated.
        return [
            Row(
                name=observation.label,
                obj=observation.obj,
                type_name=observation.value,
                value=observation.why,
                followable=observation.obj is not None or observation.expand is not None,
                kind="derived" if observation.kind != "input" else "field",
                doc=observation.doc_for or observation.why,
                cells=observation.cells,
                expand=observation.expand,
                expand_columns=observation.expand_columns,
            )
            for observation in algorithm.run(ctx.prog)
        ]

    return Frame(algorithm.label, make_rows, doc=algorithm.rule, columns=algorithm.columns)


def walkthrough_frame(ctx: Context, walk: Walkthrough) -> Frame:
    """The steps of an operation, with each function resolved to its source."""

    def make_rows() -> list[Row]:
        offset = 0
        if ct.safe(lambda: ctx.source.available, False):
            stext = ct.safe(lambda: ctx.prog.symbol("_stext").address, 0)
            if stext:
                offset = ctx.source.kaslr_offset(stext)

        rows: list[Row] = []
        for number, step in enumerate(walk.steps, start=1):
            location = ""
            if offset:
                address = ct.safe(lambda f=step.function: ctx.prog.symbol(f).address, 0)
                if address:
                    found = ctx.source.function_location(address, offset)
                    if found:
                        location = f"{found[0]}:{found[1]}"

            rows.append(
                Row(
                    name=f"{number}. {step.function}",
                    obj=None,
                    type_name=location,
                    value=step.summary,
                    followable=step.structures is not None,
                    kind="link" if step.structures is not None else "field",
                    doc=step.detail or step.summary,
                    expand=_step_expander(ctx.prog, step),
                )
            )
        return rows

    return Frame(walk.label, make_rows, doc=walk.doc, columns=STEP_COLUMNS)


def _step_expander(prog: Program, step: Step) -> Callable[[], list[Row]] | None:
    """Closure that opens the structures a walkthrough step touches."""
    if step.structures is None:
        return None
    return lambda: collection_rows(
        collect(step.function, lambda: step.structures(prog))
    )


def measurement_frame(entry: Measurement) -> Frame:
    """Run the tracer, then render its sections as rows.

    Blocking for ``entry.duration`` seconds by construction -- that is the
    measurement. The plan marks it deferred so a frontend runs it off its own
    thread.
    """
    return Frame(
        entry.label,
        lambda: _measurement_rows(entry, entry.run()),
        doc=entry.doc,
        columns=MEASURE_COLUMNS,
    )


def measurement_definition_frame(entry: Measurement) -> Frame:
    """What the probe measures and cannot see, without attaching it.

    What a highlighted measurement shows in the sidebar preview: moving the
    cursor over an entry must not start a tracer, so the definition stands in
    until enter runs it.
    """
    return Frame(
        entry.label,
        lambda: [
            *_definition_rows(entry),
            Row(f"enter to run for {entry.duration}s", None, "", "", False,
                kind="derived"),
        ],
        doc=entry.doc,
        columns=MEASURE_COLUMNS,
    )


def _definition_rows(entry: Measurement) -> list[Row]:
    """The probe's own account of itself: what it times, and what it misses."""
    rows = [_measures_row(entry)]
    if entry.blind_spot:
        rows.append(
            Row("blind spot", None, "", entry.blind_spot, False, kind="derived",
                doc=entry.blind_spot)
        )
    return rows


def _measurement_rows(entry: Measurement, result) -> list[Row]:
    """Render a probe's sections as rows, keeping the definition on screen."""
    rows = _definition_rows(entry)
    if result.error:
        rows.append(Row(result.error, None, "", "", False, kind="error"))
        return rows

    for section in result.sections:
        labels = entry.key_labels.get(section.name, {})
        unit = ""
        if section.name.startswith("us_"):
            unit = "  (microseconds)"
        elif section.name.startswith("ns_"):
            unit = "  (nanoseconds)"
        rows.append(
            Row(f"── {section.name}{unit}", None, "", "", False, kind="derived")
        )
        for label, value, bar in section.rows:
            name = labels.get(label, label)
            count = value
            if entry.per_second and not bar:
                count = f"{value}  ({_as_int(value) / entry.duration:.0f}/s)"
            rows.append(Row(f"   {name}", None, count, bar, False))
    if len(rows) <= 2:
        rows.append(Row("no events recorded in the interval", None, "", "", False))
    return rows


def _measures_row(entry: Measurement) -> Row:
    return Row("measures", None, "", entry.measures, False, kind="derived",
               doc=entry.measures)


def _as_int(value: str) -> int:
    try:
        return int(value)
    except ValueError:
        return 0


def source_frame(ctx: Context, path: str, line: int, title: str) -> Frame:
    """Show kernel source around a line, inside the tool.

    The file is the debuginfod-cached copy for this build, so the line numbers
    match the struct or function being displayed.
    """

    def make_rows() -> list[Row]:
        lines = ctx.source.read(path)
        if lines is None:
            return [Row(f"could not fetch {path}", None, "", "", False, kind="error")]

        start = max(1, line - 12)
        end = min(len(lines), start + 140)
        rows: list[Row] = []
        for number in range(start, end + 1):
            marker = "▸" if number == line else " "
            rows.append(
                Row(str(number), None, marker, lines[number - 1], False,
                    kind="derived" if number == line else "field")
            )
        return rows

    return Frame(f"{path}:{line}", make_rows, doc=title, columns=SOURCE_COLUMNS)


def landing_frame(ctx: Context) -> Frame:
    """Opening screen: which kernel this is, and what the tool can do to it.

    The capability lines matter because docs and measurements depend on
    external pieces (debuginfod, pahole, bpftrace). Without this they fail
    silently at the point of use instead of being reported up front.
    """

    def make_rows() -> list[Row]:
        from ..catalog.system import overview
        from ..core.probe import tool_available

        rows: list[Row] = []
        for fact in ct.safe(lambda: list(overview(ctx.prog)), []):
            rows.append(
                Row(fact.label, None, "", fact.value, False, kind="derived",
                    doc=fact.evidence)
            )

        rows.append(Row("", None, "", "", False))
        rows.append(Row("── capabilities", None, "", "", False, kind="derived"))

        docs_ok = ct.safe(lambda: ctx.source.available, False)
        rows.append(
            Row(
                "struct docs and source",
                None, "",
                "available" if docs_ok else "unavailable (needs pahole + debuginfod)",
                False, kind="derived",
                doc="Kernel source comments and the 's' key to open the source.",
            )
        )
        rows.append(
            Row(
                "debuginfo cache",
                None, "",
                ct.safe(lambda: debuginfod.status(ctx.source.build_id), "unknown"),
                False, kind="derived",
                doc="Run 'kexplore --prefetch' on a good connection, then "
                    "'--offline' to work with no network at all.",
            )
        )
        rows.append(
            Row(
                "measurements",
                None, "",
                "available" if tool_available("bpftrace") else "unavailable (needs bpftrace)",
                False, kind="derived",
                doc="The 'measure' groups run a tracer for a few seconds.",
            )
        )

        rows.append(Row("", None, "", "", False))
        rows.append(Row("── start here", None, "", "", False, kind="derived"))
        for label, where in (
            ("what kind of kernel is this", "system > scheduler, memory"),
            ("what is running right now", "sched > currently running"),
            ("a process and everything in it", "process > processes, then follow the links"),
            ("how long tasks wait for a CPU", "sched > measure"),
            ("what a struct really contains", "any entry, then enter to follow, s for source"),
        ):
            rows.append(Row(label, None, "", where, False, kind="derived"))
        return rows

    return Frame(
        "kexplore",
        make_rows,
        doc="Browse live kernel structures. enter follows, backspace goes back, "
            "s opens the source, : opens a drgn REPL.",
    )


# --------------------------------------------------------------------- plans


def landing_plan(ctx: Context) -> Plan:
    return Plan(
        "kexplore",
        doc="Browse live kernel structures.",
        columns=FIELD_COLUMNS,
        build=lambda: landing_frame(ctx),
        activity="checking what this kernel and cache can do…",
    )


def source_plan(ctx: Context, path: str, line: int, title: str) -> Plan:
    return Plan(
        f"{path}:{line}",
        doc=title,
        columns=SOURCE_COLUMNS,
        build=lambda: source_frame(ctx, path, line, title),
        activity=f"fetching {path} via debuginfod…",
    )


@dataclass(frozen=True)
class Listing:
    """A sidebar branch that is a heading rather than a catalog item.

    The root of each tree and the per-group branches under a subsystem have no
    object behind them. This gives them one, so selecting a heading opens what
    it contains instead of doing nothing.
    """

    label: str
    doc: str
    items: tuple


def listing_frame(label: str, doc: str, items) -> Frame:
    """One row per item, saying what it is and opening it when followed."""

    def make_rows() -> list[Row]:
        return [
            Row(item.label, None, "", "", True, kind="link",
                doc=getattr(item, "doc", ""),
                cells=(item.label, getattr(item, "doc", "")),
                item=item)
            for item in items
        ]

    return Frame(label, make_rows, doc=doc, columns=LISTING_COLUMNS)


def plan_for(item, ctx: Context, subsystem_key: str = "",
             preview: bool = False) -> Plan | None:
    """The plan for one catalog or operations item, or None if it opens nothing.

    The single place that asks what kind of thing an item is. Everywhere else
    -- the frontend, ``--check``, the crawl -- works through the interface the
    item itself provides.

    ``preview`` is a plan for an item the cursor is merely resting on rather
    than one the user asked for. It differs only for a measurement, which must
    not attach a tracer until asked.
    """
    if isinstance(item, Listing):
        return Plan(item.label, item.doc, LISTING_COLUMNS,
                    lambda: listing_frame(item.label, item.doc, item.items))
    if isinstance(item, Subsystem):
        return Plan(item.label, item.doc, LISTING_COLUMNS,
                    lambda: listing_frame(item.label, item.doc, item.entries))
    if isinstance(item, Measurement):
        if preview:
            return Plan(
                item.label,
                doc=item.doc,
                columns=MEASURE_COLUMNS,
                build=lambda: measurement_definition_frame(item),
            )
        return Plan(
            item.label,
            doc=item.doc,
            columns=MEASURE_COLUMNS,
            build=lambda: measurement_frame(item),
            activity=f"running for {item.duration}s…",
            placeholder=(
                _measures_row(item),
                Row(f"running for {item.duration}s…", None, "",
                    "bpftrace is attached; results appear when it exits",
                    False, kind="derived"),
            ),
        )
    if isinstance(item, FactEntry):
        return Plan(item.label, item.doc, FIELD_COLUMNS, lambda: fact_frame(ctx, item))
    if isinstance(item, Entry):
        return Plan(
            item.label,
            item.doc,
            FIELD_COLUMNS,
            lambda: entry_frame(ctx, item, subsystem_key),
        )
    if isinstance(item, Walkthrough):
        return Plan(
            item.label,
            doc=item.doc,
            columns=STEP_COLUMNS,
            build=lambda: walkthrough_frame(ctx, item),
            # Each step is resolved to a file:line through nm and addr2line
            # over the vmlinux debuginfo, so this is not instant.
            activity="resolving each step to its source line…",
        )
    if isinstance(item, Algorithm):
        activity = "running the experiment…" if item.background else ""
        return Plan(
            item.label,
            doc=item.rule,
            columns=item.columns,
            build=lambda: algorithm_frame(ctx, item),
            activity=activity,
            placeholder=(
                Row("running the experiment…", None, "",
                    "compiling and starting the helper, then reading the tasks "
                    "it creates", False, kind="derived"),
            ) if activity else (),
        )
    return None

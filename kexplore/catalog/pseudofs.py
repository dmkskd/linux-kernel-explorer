"""The struct member behind each line of a pseudo-file.

``procfs.py`` names the function that writes a whole /proc file. This module
maps each line the file prints to the member the kernel read to produce it,
and the pointers followed from the root struct to that member.

The values displayed are the ones in the file. This module does not read
memory to recompute them, because the view shows where a value is read from,
not whether the kernel would report the same value again. Each path is
checked against the running kernel's type information: ``check()`` returns the
paths missing from this kernel instead of displaying them.

Lines that no table describes are listed with an empty origin. A kernel can
print lines a table does not cover (a new architecture line, a configuration
the table was not written for), and omitting them would make the annotation
appear complete.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # the table is static data, so it imports on a host with
    from drgn import Program, Type  # no drgn: only the checks below need it


@dataclass(frozen=True)
class Origin:
    """The member behind one labelled line of a pseudo-file.

    ``hops`` are the members followed from the root struct, in order, each one
    a pointer. ``member`` is what is read at the end of them, and is empty for
    a line the kernel computes rather than reads: a bitmap built by walking an
    array, a value that lives in arch state with no member of its own.
    """

    label: str
    hops: tuple[str, ...] = ()
    member: str = ""
    doc: str = ""
    # Other names of ``member``. task_struct.state was renamed __state in 5.14,
    # so a table with one name would fail to resolve on kernels on one side of
    # that release.
    alternates: tuple[str, ...] = ()
    # Present only under some configurations. When it is absent, the line is
    # reported as unavailable on this kernel, not as a table error.
    optional: bool = False
    # A global variable the value is read from, for a file that is about the
    # system rather than about one task. The hops then start here.
    symbol: str = ""
    # The enum constant indexing that global, for the per-counter arrays the
    # memory statistics live in: vm_node_stat[NR_FILE_DIRTY].
    index: str = ""
    # The function that produces the value, for a line with no member to
    # name. The column then shows the function call.
    helper: str = ""

    def path(self, root: str = "task") -> str:
        """The member read, as a C expression.

        A hop spelled with a leading dot is an embedded struct rather than a
        pointer, so it keeps the dot: task->se.exec_start, not task->se->.
        """
        # The separator says how the parent is reached, not the member: a
        # pointer takes ->, a value takes a dot. task is always a pointer.
        chain, parent_is_value = self.symbol or root, bool(self.symbol)
        if self.symbol and self.index:
            chain += f"[{self.index}]"
        if not chain:
            # No path to walk: a helper produces the value, or the printing
            # function computes it inline, so there is no expression to show.
            return f"{self.helper}()" if self.helper else ""
        for hop in self.hops:
            chain += ("." if parent_is_value else "->") + hop.lstrip(".")
            parent_is_value = hop.startswith(".")
        if not self.member:
            if self.helper:
                return chain if self.symbol else f"{self.helper}({chain})"
            # A value with no source expression: a constant, or a field the
            # kernel no longer maintains. The column is empty, and the line
            # below the table gives the reason when the row is selected.
            return chain if self.symbol else ""
        return f"{chain}{'.' if parent_is_value else '->'}{self.member}"


@dataclass(frozen=True)
class PseudoFile:
    """One pseudo-file, its root struct, and the origin of each of its lines."""

    path: str
    root: str
    function: str
    doc: str
    origins: tuple[Origin, ...]
    # The manual page the field descriptions are taken from, paraphrased.
    source: str = ""
    # The kernel file ``function`` is defined in.
    defined_in: str = ""
    # Fields in a fixed order with no labels in the file, so a row is found by
    # its position and the name is this table's, not the kernel's.
    positional: bool = False
    # stat alone puts the command in parentheses in the middle of its fields,
    # so it alone cannot be split on whitespace.
    comm_field: bool = False
    # One line per object rather than one line per field. The origins are the
    # columns of every line.
    repeating: bool = False
    # For a repeating file, the object one line represents, and the path from
    # ``root`` to it. The columns are resolved against this object, and the
    # path starts at the task, as for every other file here.
    line_root: str = ""
    line_hops: tuple[str, ...] = ()
    line_walk: str = ""

    def origin_for(self, label: str) -> Origin | None:
        return _by_label(self).get(label)


def _by_label(spec: PseudoFile) -> dict[str, Origin]:
    return {origin.label: origin for origin in spec.origins}


def files() -> tuple[PseudoFile, ...]:
    """Every annotated file. Imported here rather than at the top: the tables
    are written in the model this module defines."""
    from .proc_tables import FILES

    return FILES


def __getattr__(name: str):
    """Expose the tables as attributes of this module, where they were defined before."""
    from . import proc_tables

    try:
        return getattr(proc_tables, name)
    except AttributeError:
        raise AttributeError(name) from None


# ------------------------------------------------------------------ reading


@dataclass(frozen=True)
class Line:
    """One line of the file as read, paired with where it is picked up from."""

    label: str
    value: str
    origin: Origin | None


def _parse_positional(spec: PseudoFile, text: str) -> list[Line]:
    """One line of space-separated fields, matched to the table by position.

    The command sits in parentheses and may contain both spaces and a closing
    parenthesis, so the split is anchored on the last ") " rather than on the
    field separator.
    """
    line = text.strip()
    if spec.comm_field:
        pid, _, rest = line.partition(" (")
        comm, _, tail = rest.rpartition(") ")
        values = [pid, comm, *tail.split()]
    else:
        values = line.split()
    lines: list[Line] = []
    for number, value in enumerate(values, 1):
        origin = spec.origins[number - 1] if number <= len(spec.origins) else None
        label = f"{number:>2}  {origin.label}" if origin else f"{number:>2}"
        lines.append(Line(label, value, origin))
    return lines


def parse(spec: PseudoFile, text: str) -> list[Line]:
    """The file's lines, in file order, each matched against the table.

    Split from the read so the matching can be checked against a captured
    file, on a machine with no kernel to read one from.
    """
    if spec.positional:
        return _parse_positional(spec, text)
    if spec.repeating:
        return [
            Line(str(number), raw.rstrip(), None)
            for number, raw in enumerate(text.splitlines(), 1)
        ]
    table = _by_label(spec)
    lines: list[Line] = []
    for raw in text.splitlines():
        label, separator, value = raw.partition(":")
        if not separator:
            continue
        # sched pads its labels to a column width, so a label is stripped
        # before it is compared with the table.
        label = label.strip()
        lines.append(Line(label, " ".join(value.split()), table.get(label)))
    return lines


def read_file(spec: PseudoFile, path: str) -> list[Line]:
    """The same, read from a live file at ``path``.

    Raises OSError when the file cannot be read, for example when the task
    exited between being listed and being opened.
    """
    return parse(spec, Path(path).read_text())


def read(spec: PseudoFile, pid: int) -> list[Line]:
    """The file of one task, by pid."""
    return read_file(spec, spec.path.replace("<pid>", str(pid)))


def unclaimed(spec: PseudoFile, text: str) -> list[str]:
    """Labels in this text that the table does not describe."""
    return [line.label for line in parse(spec, text) if line.origin is None]


# ------------------------------------------------------------------ checking


def _member_type(type_: "Type", name: str) -> "Type | None":
    """The type of one member, seeing through anonymous structs and unions."""
    from ..core import ctypes as ct

    aggregate = ct.struct_type(type_)
    if aggregate is None or not aggregate.members:
        return None
    for member in aggregate.members:
        if member.name == name:
            return member.type
        if member.name is None:
            found = _member_type(member.type, name)
            if found is not None:
                return found
    return None


def resolve(prog: "Program", spec: PseudoFile, origin: Origin) -> str:
    """Empty when this kernel has the path, or what is missing from it.

    A repeating file's columns are reached from the object a line represents,
    not from the task, so they are resolved from ``line_root``.
    """
    from drgn import TypeKind

    from ..core import ctypes as ct

    if origin.symbol:
        try:
            current = prog[origin.symbol].type_
        except KeyError:
            return f"{origin.symbol} not in this kernel"
        walked = origin.symbol
        if origin.index and not origin.index.isdigit():
            # The counter arrays are indexed by an enum whose names change as
            # counters are added and removed, so the name is checked too.
            try:
                prog.constant(origin.index)
            except LookupError:
                return f"{origin.index} not in this kernel"
        if not origin.member:
            return ""
    else:
        root = spec.line_root or spec.root
        if not root:
            # A system-wide line with no global named is computed from a clock,
            # a helper or a sum, so there is no path to resolve.
            return ""
        try:
            current = prog.type(f"struct {root}")
        except LookupError:
            return f"struct {root} not in this kernel"
        walked = root
    for hop in origin.hops:
        found = _member_type(current, hop.lstrip("."))
        if found is None:
            return f"{walked} has no member {hop.lstrip('.')}"
        if ct.struct_type(found) is None:
            return f"{walked}->{hop} is not a struct"
        # jump_target reads the separator to decide whether to test for NULL,
        # so a wrong separator causes a fault there as well as a wrong path
        # here.
        is_pointer = ct.strip(found).kind == TypeKind.POINTER
        if hop.startswith(".") == is_pointer:
            name = hop.lstrip(".")
            return (f"{walked}->{name} is a pointer, spell the hop {name}"
                    if is_pointer else
                    f"{walked}->{name} is an embedded struct, spell the hop .{name}")
        current, walked = found, f"{walked}->{hop.lstrip('.')}"
    if not origin.member:
        return ""
    names = ct.member_names(ct.struct_type(current))
    if origin.member in names:
        return ""
    for alternate in origin.alternates:
        if alternate in names:
            return ""
    return f"{walked} has no member {origin.member}"


def check(prog: "Program", spec: PseudoFile) -> tuple[bool, str]:
    """Whether every path in the table exists in this kernel's types."""
    missing = []
    absent = []
    for origin in spec.origins:
        problem = resolve(prog, spec, origin)
        if not problem:
            continue
        (absent if origin.optional else missing).append(f"{origin.label}: {problem}")
    total = len(spec.origins)
    if missing:
        return False, f"{len(missing)} of {total} paths not in this kernel: " + \
                      "; ".join(missing)
    note = f", {len(absent)} optional absent" if absent else ""
    return True, f"{total} paths resolve{note}"


def line_path(spec: PseudoFile) -> list[tuple[str, str]]:
    """How the kernel reaches one line's object from the task.

    Named rather than resolved: the last step is a tree walk, not a member,
    and hop_types only follows members.
    """
    steps = [(spec.root, f"struct {spec.root}")]
    steps += [(f"->{hop}", "") for hop in spec.line_hops]
    if spec.line_walk:
        steps.append((f"walks {spec.line_walk}", f"struct {spec.line_root}"))
    return steps


def hop_types(prog: "Program", spec: PseudoFile, origin: Origin) -> list[tuple[str, str]]:
    """Each step of the path, with the type this kernel gives it.

    The names come from the table and the types from the running kernel's
    debug information, so an expanded row shows the kernel's types, not the
    ones the table assumed.
    """
    from ..core import ctypes as ct

    root = spec.line_root or spec.root
    if origin.symbol:
        try:
            return [(origin.symbol, ct.type_name(prog[origin.symbol].type_))]
        except KeyError:
            return [(origin.symbol, "not in this kernel")]
    if not root:
        return []
    steps: list[tuple[str, str]] = [(root, f"struct {root}")]
    try:
        current = prog.type(f"struct {root}")
    except LookupError:
        return steps
    for hop in origin.hops:
        found = _member_type(current, hop.lstrip("."))
        arrow = hop if hop.startswith(".") else f"->{hop}"
        if found is None:
            steps.append((arrow, "not in this kernel"))
            return steps
        steps.append((arrow, ct.type_name(found)))
        current = found
    if origin.member:
        found = _member_type(current, origin.member)
        if found is None:
            for alternate in origin.alternates:
                found = _member_type(current, alternate)
                if found is not None:
                    steps.append((f".{alternate}", ct.type_name(found)))
                    return steps
            steps.append((f".{origin.member}", "not in this kernel"))
        else:
            steps.append((f".{origin.member}", ct.type_name(found)))
    return steps


# -------------------------------------------------------------- the tree

# Files that must not be read by a browser. kmsg blocks until something is
# logged and consumes the message when it arrives; kcore and the kpage* maps
# are the size of memory; sysrq-trigger acts on what is written to it. The
# rest of /proc is text a reader may open safely.
UNREADABLE = {
    "/proc/kmsg": "blocks until a message is logged, and consumes it",
    "/proc/kcore": "the whole of memory, as an ELF image",
    "/proc/kpageflags": "one 64-bit word per page of memory",
    "/proc/kpagecount": "one 64-bit word per page of memory",
    "/proc/kpagecgroup": "one 64-bit word per page of memory",
    "/proc/sysrq-trigger": "acts on what is written to it",
    "/proc/self/mem": "the address space itself, read through a seek",
}

# Maximum number of bytes read from a file. No text file under /proc is this
# large, so reaching the limit indicates a file that is not text.
MAX_BYTES = 256 * 1024


@dataclass(frozen=True)
class Node:
    """One entry of a pseudo-filesystem directory."""

    path: str
    name: str
    is_dir: bool
    # A symlink resolved to reach this: /proc/self, /proc/<pid>/cwd.
    link_target: str = ""

    @property
    def label(self) -> str:
        return self.name

    @property
    def doc(self) -> str:
        if self.link_target:
            return f"\u2192 {self.link_target}"
        known = spec_for(self.path)
        if known is not None:
            return known.doc
        from .procfs import SERVED_BY

        served = SERVED_BY.get(_generic(self.path))
        if served is not None:
            return f"{served.function}(). {served.doc}"
        return ""


def _generic(path: str) -> str:
    """A path with its pid replaced, so one table entry answers for any task."""
    return re.sub(r"/proc/\d+/", "/proc/<pid>/", path)


def spec_for(path: str) -> PseudoFile | None:
    """The annotation table for this path, when one is written."""
    generic = _generic(path)
    return next((spec for spec in files() if spec.path == generic), None)


def annotates(path: str) -> bool:
    """Whether a line table is written for this file."""
    return spec_for(path) is not None


def holds_annotated(path: str) -> bool:
    """Whether this directory holds a file a line table is written for."""
    # The trailing slash first: the pid pattern ends in one, so /proc/1 on
    # its own is not recognised as a task directory without it.
    generic = _generic(path.rstrip("/") + "/")
    return any(spec.path.startswith(generic)
               and "/" not in spec.path[len(generic):]
               for spec in files())


def listing(path: str) -> list[Node]:
    """What the directory holds, directories first and pids in numeric order.

    The entries are read from /proc, not from a table, so a pid is listed
    because that task exists when the directory is read.
    """
    nodes: list[Node] = []
    with os.scandir(path) as entries:
        for entry in entries:
            target = ""
            if entry.is_symlink():
                target = os.readlink(entry.path)
            nodes.append(
                Node(entry.path, entry.name, entry.is_dir(), target)
            )
    return sorted(nodes, key=_order)


def _order(node: Node) -> tuple[int, int, str]:
    """Sort key: system entries first, task directories last.

    Task directories are listed after every other entry because there are
    hundreds of them, and listed first they would push meminfo below the first
    screen.
    """
    if node.is_dir and node.name.isdigit():
        return (2, int(node.name), "")
    return (0 if node.is_dir else 1, 0, node.name)


def comm_of(path: str) -> str:
    """The command of the task whose directory this is, or an empty string.

    One read per task directory listed. A task that exits between the listing
    and this read is listed with an empty name.
    """
    try:
        with open(f"{path}/comm") as handle:
            return handle.read().strip()
    except OSError:
        return ""


def contents(path: str) -> str:
    """The file as text, or a reason it is not being read.

    Raises OSError, which the caller turns into a row. A file this function
    does not read is still listed, with the reason it is not read.
    """
    refusal = UNREADABLE.get(path) or UNREADABLE.get(_generic(path))
    if refusal is not None:
        raise OSError(refusal)
    with open(path, "rb") as handle:
        raw = handle.read(MAX_BYTES)
    if b"\0" in raw:
        # cmdline and environ separate their entries with NUL instead of a
        # newline; each entry is printed on a line of its own.
        stripped = raw.rstrip(b"\0")
        if b"\0\0" not in stripped and _printable(stripped):
            return stripped.replace(b"\0", b"\n").decode("utf-8", "replace")
        raise OSError("not text")
    return raw.decode("utf-8", "replace")


def _printable(raw: bytes) -> bool:
    """Whether every byte is text, tab or newline, NUL separators aside."""
    return all(byte >= 0x20 or byte in (0x09, 0x0A, 0x00) for byte in raw)


# ------------------------------------------------------------------- items


@dataclass(frozen=True)
class FileItem:
    """One pseudo-file to open: annotated when a table describes it, raw otherwise."""

    path: str

    @property
    def spec(self) -> PseudoFile | None:
        return spec_for(self.path)

    @property
    def label(self) -> str:
        return self.path

    @property
    def doc(self) -> str:
        spec = self.spec
        if spec is not None:
            return f"{spec.function}(). {spec.doc}"
        from .procfs import SERVED_BY

        served = SERVED_BY.get(_generic(self.path))
        if served is not None:
            return f"{served.function}(). No line table yet."
        return "No line table yet."


@dataclass(frozen=True)
class Directory:
    """One pseudo-filesystem directory to open."""

    path: str

    @property
    def label(self) -> str:
        return self.path

    @property
    def doc(self) -> str:
        if self.path == "/proc":
            return "Numbered directories are tasks. The rest is the system."
        if re.fullmatch(r"/proc/\d+", self.path):
            return "One task."
        return ""


def directory_for(pid: int) -> Directory:
    """The /proc directory of one task."""
    return Directory(f"/proc/{pid}")


ROOT = Directory("/proc")

"""Where each line of a pseudo-file is picked up from.

``procfs.py`` names the function that writes a whole /proc file. This goes one
level finer: for every line the file prints, the member the kernel read to
produce it, and the pointers it followed from the root struct to get to that
member.

The values on screen are the ones in the file. Nothing here reads memory to
recompute them, because the question this view answers is where a number is
picked up from, not whether the kernel would still report the same number. The
hops are checked against the running kernel's type information instead, so a
table that has drifted from this kernel says so rather than printing a path
that no longer exists.

Lines the table does not claim are listed too, with an empty origin. A kernel
prints lines this file has never heard of (a new arch line, a config this
table was not written against), and hiding them would make the annotation look
complete when it is not.
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
    # Other spellings of ``member``. task_struct.state became __state in 5.14,
    # and a table with one spelling in it would report a miss on half the
    # kernels this runs against.
    alternates: tuple[str, ...] = ()
    # Present only under some configurations. Absence is reported as absence
    # rather than as a table that is wrong.
    optional: bool = False
    # A global variable the value is read from, for a file that is about the
    # system rather than about one task. The hops then start here.
    symbol: str = ""
    # The enum constant indexing that global, for the per-counter arrays the
    # memory statistics live in: vm_node_stat[NR_FILE_DIRTY].
    index: str = ""
    # The function that produces the value, for a line with no member to
    # name. The column then shows a call rather than nothing.
    helper: str = ""

    def path(self, root: str = "task") -> str:
        """The member read, written the way you would write it in C.

        A hop spelled with a leading dot is an embedded struct rather than a
        pointer, so it keeps the dot: task->se.exec_start, not task->se->.
        """
        # The separator says how the parent is reached, not the member: a
        # pointer takes ->, a value takes a dot. task is always a pointer.
        chain, parent_is_value = self.symbol or root, bool(self.symbol)
        if self.symbol and self.index:
            chain += f"[{self.index}]"
        if not chain:
            # Nothing to walk: either a helper produces the value, or the
            # printing function computes it inline and there is no expression
            # to show.
            return f"{self.helper}()" if self.helper else ""
        for hop in self.hops:
            chain += ("." if parent_is_value else "->") + hop.lstrip(".")
            parent_is_value = hop.startswith(".")
        if not self.member:
            if self.helper:
                return chain if self.symbol else f"{self.helper}({chain})"
            # A value with no expression behind it: a constant, or a field the
            # kernel no longer maintains. The column stays empty and the line
            # under the cursor says why.
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
    # For a repeating file, the object one line stands for, and how the kernel
    # gets from ``root`` to it. The columns resolve against this, the path to
    # it starts at the task like every other file here.
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
    """Let the tables be reached through this module, where they used to live."""
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
        # sched pads its labels out to a column, so the name in the file is
        # not the name in the table until it is stripped.
        label = label.strip()
        lines.append(Line(label, " ".join(value.split()), table.get(label)))
    return lines


def read_file(spec: PseudoFile, path: str) -> list[Line]:
    """The same, read from a live file at ``path``.

    Raises OSError when the file cannot be read, which is the honest outcome
    when the task exited between being listed and being opened.
    """
    return parse(spec, Path(path).read_text())


def read(spec: PseudoFile, pid: int) -> list[Line]:
    """The file of one task, by pid."""
    return read_file(spec, spec.path.replace("<pid>", str(pid)))


def unclaimed(spec: PseudoFile, text: str) -> list[str]:
    """Labels in this text that the table says nothing about."""
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

    A repeating file's columns hang off the object a line stands for, not off
    the task, so they are resolved from ``line_root``.
    """
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
            # A system-wide line with no global named: computed from a clock,
            # a helper or a sum, with nothing to resolve.
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

    The names come from the table; the types come from the kernel's own debug
    information, so an expanded row shows what is there rather than what was
    written down.
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

# A file is read this far and no further. Nothing under /proc is meant to be
# larger, so hitting the cap says something is not the text file it looked
# like rather than that the reader is too small.
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

    Reading /proc is the point, so the entries are the real ones rather than a
    table: a pid appears here because that task exists right now.
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
    """The system first, the tasks last, each in the order worth reading.

    A task directory is listed after everything else because there are
    hundreds of them: put them first and meminfo is a screen and a half down,
    which is where the answer to most questions about /proc is.
    """
    if node.is_dir and node.name.isdigit():
        return (2, int(node.name), "")
    return (0 if node.is_dir else 1, 0, node.name)


def comm_of(path: str) -> str:
    """The command of the task whose directory this is, or an empty string.

    One small read per task directory listed. A task that exits between the
    listing and this read simply has no name to show.
    """
    try:
        with open(f"{path}/comm") as handle:
            return handle.read().strip()
    except OSError:
        return ""


def contents(path: str) -> str:
    """The file as text, or a reason it is not being read.

    Raises OSError, which the caller turns into a row. A file this refuses to
    open is still worth listing: what it holds is the answer, and the reason
    it cannot be printed here is part of that answer.
    """
    refusal = UNREADABLE.get(path) or UNREADABLE.get(_generic(path))
    if refusal is not None:
        raise OSError(refusal)
    with open(path, "rb") as handle:
        raw = handle.read(MAX_BYTES)
    if b"\0" in raw:
        # cmdline and environ separate their entries with NUL rather than a
        # newline, and are the files most worth reading here. One entry per
        # line is that file printed, not reformatted.
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
    """One pseudo-file to open: annotated when the table knows it, raw when not."""

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

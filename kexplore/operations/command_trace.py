"""Trace a command's process tree into each observed kernel interface.

Discovery retains procfs reads and non-file handlers with their own stacks.
A second run probes file-serving functions together, using the current read's
file context. Static field references, direct helpers, and catalog associations
are labelled separately from measured function calls.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass, field

from drgn import Program

from ..catalog.procfs import SERVED_BY, ProcFile, fields_from, path_from_dentry
from ..core import ctypes as ct
from ..core.probe import TRACE_FILTER, parse_keyed_stacks, parse_stacks, trace_command
from ..core.source import KernelSource
from ..core.source_refs import field_references, function_body, parameter_calls
from .algorithm import Algorithm, Observation, register_algorithm

COMMAND = "ps -e"

# Every interface the catalog's commands reach the kernel through. Each is the
# lowest point that still names what was asked for: the dentry for a file, the
# netlink handler for a dump. Probing one syscall entry instead would report
# the same function for every command.
DISCOVER = """
kprobe:do_sys_openat2 /{filter}/ {{ @opens[str(uptr(arg1))] = count(); }}
kprobe:vfs_read /{filter}/ {{
  $f = (struct file *)arg0;
  if ($f->f_inode->i_sb->s_magic == 0x9fa0) {{
    $d = $f->f_path.dentry;
    @vfs_reads[str($d->d_parent->d_parent->d_name.name),
               str($d->d_parent->d_name.name), str($d->d_name.name)] = count();
    @file_stack[str($d->d_parent->d_parent->d_name.name),
                str($d->d_parent->d_name.name), str($d->d_name.name), kstack(12)] = count();
  }}
}}
kprobe:seq_read_iter /{filter}/ {{
  $f = ((struct kiocb *)arg0)->ki_filp;
  if ($f->f_inode->i_sb->s_magic == 0x9fa0) {{
    $d = $f->f_path.dentry;
    @reads[str($d->d_parent->d_parent->d_name.name),
           str($d->d_parent->d_name.name), str($d->d_name.name)] = count();
    @read_stack[str($d->d_parent->d_parent->d_name.name),
                str($d->d_parent->d_name.name), str($d->d_name.name), kstack(12)] = count();
    $m = (struct seq_file *)$f->private_data;
    @shows[str($d->d_parent->d_parent->d_name.name),
           str($d->d_parent->d_name.name), str($d->d_name.name),
           (uint64)$m->op->show, (uint64)$m->private] = count();
  }}
}}
kprobe:vfs_readlink, kprobe:iterate_dir, kprobe:vfs_statx,
kprobe:rtnetlink_rcv_msg, kprobe:inet_diag_dump, kprobe:__netlink_dump_start
/{filter}/ {{
  @entry[probe] = count();
  @entry_stack[probe, kstack(12)] = count();
}}
"""

_PID_PATH = re.compile(r"^/proc/\d+")

# The same substitution inside a command line, for comparing what the command
# names against what it was measured reading.
_NAMED_PID = re.compile(r"/proc/\d+/")

# A multi-interface trace can visit several uncached source files. Do not let
# each unavailable file hold the result for the browser's full download timeout.
_SOURCE = KernelSource(source_timeout=5)


def _grouped_opens(rows: list[tuple[str, str, str]]) -> list[tuple[str, int]]:
    """Collapse the per-pid paths, which are one row each, into one line.

    ps opens /proc/1, /proc/2, /proc/17… once each. Listing them is a list of
    the pids that existed, which is not what the stage is about; the count is.
    """
    counts: dict[str, int] = {}
    for label, value, _bar in rows:
        path = _PID_PATH.sub("/proc/<pid>", label)
        try:
            counts[path] = counts.get(path, 0) + int(value)
        except ValueError:
            continue
    # /proc first: the point of the stage is the file the kernel serves, and it
    # is opened once, so a plain sort by count buries it under the loader's.
    return sorted(
        counts.items(),
        key=lambda pair: (pair[0].startswith("/proc"), pair[1]),
        reverse=True,
    )


def _location(prog: Program, function: str) -> tuple[str, int] | None:
    """(relative path, line) for a kernel function, if debuginfo is loaded."""
    if not ct.safe(lambda: _SOURCE.available, False):
        return None
    stext = ct.safe(lambda: prog.symbol("_stext").address, 0)
    if not stext:
        return None
    offset = _SOURCE.kaslr_offset(stext)
    address = ct.safe(lambda: prog.symbol(function).address, 0)
    if not address or not offset:
        return None
    return _SOURCE.function_location(address, offset)


def _where(prog: Program, function: str) -> str:
    found = _location(prog, function)
    return f"{found[0]}:{found[1]}" if found else ""


def _stack_where(prog: Program, frame: str) -> str:
    """Resolve the recorded instruction offset, not just the function entry."""
    name, sep, displacement = frame.partition("+")
    try:
        offset = (
            int(displacement, 16 if displacement.startswith("0x") else 10) if sep else 0
        )
    except ValueError:
        return ""
    symbols = ct.safe(lambda: prog.symbols(name), [])
    if len(symbols) != 1:
        return ""  # A printed name cannot identify a duplicate static symbol.
    return _where_at(prog, symbols[0].address + offset)


def _symbol(prog: Program, address: int) -> str:
    return ct.safe(lambda: prog.symbol(address).name, "")


def _probeable(prog: Program, name: str) -> bool:
    """Whether a kprobe on this name can attach.

    A static function's name is not unique: two translation units both define
    m_show, kallsyms holds both, and bpftrace refuses the ambiguous name rather
    than guessing. The measured address still says which one ran, so the trace
    keeps the function and gives up only the stack below it.
    """
    return len(ct.safe(lambda: prog.symbols(name), [])) == 1


def _location_at(prog: Program, address: int) -> tuple[str, int] | None:
    """(path, line) for an exact address, which a duplicated name cannot give."""
    if not address or not ct.safe(lambda: _SOURCE.available, False):
        return None
    stext = ct.safe(lambda: prog.symbol("_stext").address, 0)
    offset = _SOURCE.kaslr_offset(stext) if stext else 0
    if not offset:
        return None
    return _SOURCE.function_location(address, offset)


def _where_at(prog: Program, address: int) -> str:
    found = _location_at(prog, address)
    return f"{found[0]}:{found[1]}" if found else ""


def _opens_stage(command: str, sections: dict) -> Iterator[Observation]:
    """Stage 2: the files one run of the command opened."""
    opens = _grouped_opens(sections["opens"].rows) if "opens" in sections else []

    yield Observation(
        "2. files opened",
        f"{sum(count for _path, count in opens)} open calls",
        "kprobe:do_sys_openat2",
        kind="heading",
    )
    for path, count in opens[:6]:
        # ps opens the task directory once and then opens the files inside it
        # by name, so most of these arrive as a bare "stat" with a directory fd
        # this probe does not record. Marking them keeps the row honest about
        # what was measured.
        relative = not path.startswith("/")
        yield Observation(
            f"   {'…/' if relative else ''}{path}",
            str(count),
            "relative to a directory fd" if relative else "",
        )


def _read_path(grandparent: str, parent: str, name: str) -> str:
    """Reconstruct known proc paths; retain an explicit suffix for deeper ones."""
    return path_from_dentry(grandparent, parent, name) or "/proc/…/" + "/".join(
        part for part in (grandparent, parent, name) if part != "/"
    )


def _merge_stacks(stacks):
    counts = {}
    for frames, count in stacks:
        key = tuple(frames)
        counts[key] = counts.get(key, 0) + count
    return [(list(frames), count) for frames, count in counts.items()]


def _files_read(sections: dict, command: str) -> list[tuple[str, int]]:
    """Every /proc path the command read, the one it is about first.

    Read counts alone rank the wrong file. ``grep VmPin /proc/1/status`` reads
    the status file once and its own ``/proc/self/maps`` twice while starting
    up, so the busiest file is the one nobody asked about. A path the command
    names outright wins regardless of count; everything else falls back to it.
    """
    named = _NAMED_PID.sub("/proc/<pid>/", command)
    counts: dict[str, int] = {}
    for section_name in ("reads", "vfs_reads"):
        per_path: dict[str, int] = {}
        for label, value, _bar in (
            sections[section_name].rows if section_name in sections else []
        ):
            parts = [part.strip() for part in label.split(",")]
            if len(parts) != 3:
                continue
            path = _read_path(*parts)
            try:
                count = int(value)
            except ValueError:
                continue
            if path:
                per_path[path] = per_path.get(path, 0) + count
        for path, count in per_path.items():
            counts[path] = max(counts.get(path, 0), count)
    return sorted(
        counts.items(),
        key=lambda pair: (pair[0] in named, pair[1]),
        reverse=True,
    )


def _proc_show_at(prog: Program, inode_address: int) -> tuple[str, int] | None:
    """The function a /proc/<pid> file names in its inode.

    proc_single_show leaves the inode in the seq_file's private pointer and
    calls PROC_I(inode)->op.proc_show, so the interesting function is one
    dereference away from something the probe already recorded.
    """
    from drgn import Object, container_of

    inode = Object(prog, "struct inode *", inode_address)
    address = ct.safe(
        lambda: container_of(
            inode, "struct proc_inode", "vfs_inode"
        ).op.proc_show.value_(),
        0,
    )
    name = _symbol(prog, address)
    return (name, address) if name else None


def _measured_leaf(prog: Program, sections: dict, path: str) -> tuple[str, int]:
    """The show function the kernel is holding for the file being read.

    A file with its own iterator keeps the real one in ``m->op->show``. A
    ``single_open`` file under /proc/<pid> keeps ``proc_single_show`` there and
    the interesting one behind the inode, so unwrap that: the probe records both
    pointers and this reads the second only when the first says to.
    """
    best = ("", 0, 0)
    for label, value, _bar in sections["shows"].rows if "shows" in sections else []:
        parts = [part.strip() for part in label.split(",")]
        if len(parts) != 5 or _read_path(*parts[:3]) != path:
            continue
        try:
            show, private, count = int(parts[3]), int(parts[4]), int(value)
        except ValueError:
            continue
        address = show
        name = _symbol(prog, show)
        if name == "proc_single_show" and private:
            deeper = _proc_show_at(prog, private)
            if deeper:
                name, address = deeper
        if name and count > best[2]:
            best = (name, address, count)
    return best[0], best[1]


# Netlink handlers are listed first for scanning; file interfaces are retained.
NETLINK = frozenset({"inet_diag_dump", "rtnetlink_rcv_msg", "__netlink_dump_start"})


def _entry_points(sections: dict) -> list[tuple[str, int]]:
    """Every non-file interface that fired, busiest first."""
    found: list[tuple[str, int]] = []
    for label, value, _bar in sections["entry"].rows if "entry" in sections else []:
        try:
            found.append((label.split(":")[-1], int(value)))
        except ValueError:
            continue
    return sorted(found, key=lambda pair: pair[1], reverse=True)


def _source_function(prog: Program, function: str, address: int = 0):
    """A body and its DWARF parameters, or unavailable evidence."""
    found = _location_at(prog, address) if address else _location(prog, function)
    if not found:
        return None
    lines = _SOURCE.read(found[0])
    body = function_body(lines, found[1]) if lines else None
    if body is None:
        return None
    try:
        if len(prog.symbols(function)) != 1:
            return None
        parameters = {
            p.name: str(p.type) for p in prog.function(function).type_.parameters
        }
    except Exception:  # noqa: BLE001 - missing or ambiguous DWARF
        return None
    return body, parameters


def _reads_from_source(prog: Program, function: str, address: int = 0):
    source = _source_function(prog, function, address)
    return field_references(*source) if source is not None else None


@dataclass
class FieldEvidence:
    direct: list[tuple[str, str]] | None
    helpers: list[tuple[str, str, str]] = field(default_factory=list)
    unavailable: list[str] = field(default_factory=list)
    catalog: list[tuple[str, str]] = field(default_factory=list)
    omitted: int = 0


MAX_HELPERS = 12


def _field_evidence(prog: Program, interface) -> FieldEvidence:
    source = (
        _source_function(prog, interface.function, interface.address)
        if interface.function
        else None
    )
    evidence = FieldEvidence(
        field_references(*source) if source is not None else None,
        catalog=fields_from(interface.path) if interface.path else [],
    )
    if source is None:
        return evidence
    calls = parameter_calls(*source)
    calls.pop(interface.function, None)
    evidence.omitted = max(0, len(calls) - MAX_HELPERS)
    for helper, positions in sorted(calls.items())[:MAX_HELPERS]:
        helper_source = _source_function(prog, helper)
        if helper_source is None:
            evidence.unavailable.append(helper)
            continue
        body, parameters = helper_source
        passed = {
            name: kind
            for index, (name, kind) in enumerate(parameters.items())
            if index in positions
        }
        for name, type_name in field_references(body, passed):
            evidence.helpers.append((name, type_name, helper))
    return evidence


def _selected_evidence(selected: str, interfaces, analyses) -> Observation:
    matches = []
    for interface, analysis in zip(interfaces, analyses):
        route = interface.path or interface.function
        qualifier = (
            "observed function"
            if interface.probed
            else "candidate function, not probed"
        )
        if selected in {name for name, _type in analysis.direct or []}:
            matches.append(f"{route}: direct source reference ({qualifier})")
        for name, _type, helper in analysis.helpers:
            if name == selected:
                matches.append(
                    f"{route}: source reference via {helper} ({qualifier}; helper not probed)"
                )
        if selected in {name for name, _command in analysis.catalog}:
            matches.append(f"{route}: catalog association")
    return Observation(
        "   selected field",
        selected,
        "; ".join(matches) + "; runtime access not established"
        if matches
        else "not established: no direct, helper, or catalog association found; "
        "this trace does not explain the selected field",
    )


@dataclass
class Interface:
    path: str
    function: str
    address: int = 0
    calls: int = 0
    evidence: str = ""
    stacks: list[tuple[list[str], int]] = field(default_factory=list)
    probed: bool = False
    keys: list[tuple[str, str, str]] = field(default_factory=list)


def _interfaces(
    prog: Program, sections: dict, raw: str, command: str, served: ProcFile | None
) -> list[Interface]:
    """Keep each observed file and non-file handler with its own evidence."""
    keyed = parse_keyed_stacks(raw)
    result = []
    for function, count in _entry_points(sections):
        stacks = [
            (frames, hits)
            for key, frames, hits in keyed.get("entry_stack", [])
            if key.split(":")[-1] == function
        ]
        result.append(
            Interface(
                "",
                function,
                calls=count,
                evidence="discovery handler calls",
                stacks=stacks,
                probed=True,
            )
        )
    result.sort(key=lambda item: (item.function in NETLINK, item.calls), reverse=True)
    for path, count in _files_read(sections, command):
        known = SERVED_BY.get(path)
        function, address = (
            (known.function, 0) if known else _measured_leaf(prog, sections, path)
        )
        keys = set()
        for section_name in ("reads", "vfs_reads"):
            for label, _value, _bar in (
                sections[section_name].rows if section_name in sections else []
            ):
                parts = tuple(part.strip() for part in label.split(","))
                if len(parts) == 3 and _read_path(*parts) == path:
                    keys.add(parts)
        stacks = []
        for name in ("read_stack", "file_stack"):
            for key, frames, hits in keyed.get(name, []):
                parts = tuple(part.strip() for part in key.split(","))
                if parts in keys:
                    stacks.append((frames, hits))
            if stacks:
                break
        result.append(
            Interface(
                path,
                function,
                address,
                count,
                "leaf from the catalog" if known else "leaf from the seq_file",
                _merge_stacks(stacks),
                False,
                sorted(keys),
            )
        )
    # Named non-proc files (e.g. sysfs) have a catalog candidate, not an observed
    # read path. Its function is probed in pass two and labelled accordingly.
    if served and not any(item.path == served.path for item in result):
        result.append(
            Interface(
                served.path,
                served.function,
                evidence="catalog candidate; path not observed in discovery",
            )
        )
    return result


def _stack_script(prog: Program, interfaces: list[Interface]) -> str:
    """Attach all leaf probes together, with file context from the current read."""
    clauses = []
    probes = []
    for index, item in enumerate(interfaces):
        if not item.path or not item.function or not _probeable(prog, item.function):
            continue
        if item.keys:
            conditions = set()
            for grandparent, parent, name in item.keys:
                parts = []
                for expression, value in (
                    ("$d->d_parent->d_parent->d_name.name", grandparent),
                    ("$d->d_parent->d_name.name", parent),
                    ("$d->d_name.name", name),
                ):
                    if value.isdigit():
                        parts.append(f'str({expression}) != "/"')
                    else:
                        parts.append(f"str({expression}) == {json.dumps(value)}")
                conditions.add(" && ".join(parts))
            clauses.append(
                f"if ({' || '.join('(' + c + ')' for c in sorted(conditions))}) "
                f"{{ @trace_file[tid] = {index + 1}; }}"
            )
            predicate = f"{TRACE_FILTER} && @trace_file[tid] == {index + 1}"
        else:
            predicate = TRACE_FILTER
        probes.append(
            f"kprobe:{item.function} /{predicate}/ "
            f"{{ @serves{index}[kstack(12)] = count(); }}"
        )
    if not probes:
        return ""
    context = "\n".join(clauses)
    return f"""
kprobe:vfs_read /{TRACE_FILTER}/ {{
  delete(@trace_file[tid]);
  $f = (struct file *)arg0;
  if ($f->f_inode->i_sb->s_magic == 0x9fa0) {{
    $d = $f->f_path.dentry;
    {context}
  }}
}}
kprobe:seq_read_iter /{TRACE_FILTER}/ {{
  delete(@trace_file[tid]);
  $f = ((struct kiocb *)arg0)->ki_filp;
  if ($f->f_inode->i_sb->s_magic == 0x9fa0) {{
    $d = $f->f_path.dentry;
    {context}
  }}
}}
kretprobe:vfs_read, kretprobe:seq_read_iter /{TRACE_FILTER}/ {{ delete(@trace_file[tid]); }}
""" + "\n".join(probes)


def command_trace(
    prog: Program,
    command: str,
    served: ProcFile | None = None,
    origin: str = "",
    selected_field: str = "",
) -> Iterator[Observation]:
    """Discover every interface, then collect file-serving stacks in one run."""
    yield Observation(
        "1. command",
        command,
        f"userspace column of {origin}" if origin else "the command traced",
        kind="heading",
    )
    yield Observation(
        "   attribution",
        "launcher and descendants",
        "thread IDs tracked across fork, exec, and exit; both sides of pipelines included",
    )
    first = trace_command(DISCOVER.format(filter=TRACE_FILTER), command)
    if first.error:
        yield Observation("trace failed", first.error, kind="result")
        return
    sections = {section.name: section for section in first.sections}
    yield from _opens_stage(command, sections)
    interfaces = _interfaces(prog, sections, first.raw, command, served)
    script = _stack_script(prog, interfaces)
    second = trace_command(script, command) if script else None
    if first.stopped or (second and second.stopped):
        yield Observation(
            "   note",
            "capped at 5 seconds per run",
            "the command did not exit within the observation interval",
        )
    if second and second.error:
        yield Observation(
            "   stack pass failed", second.error, "discovery evidence retained"
        )
    elif second:
        stacks = parse_stacks(second.raw)
        for index, item in enumerate(interfaces):
            leaf_stacks = stacks.get(f"serves{index}", [])
            if leaf_stacks:
                item.stacks = leaf_stacks
                item.probed = True
                item.evidence += "; serving stack in second run"
                if item.keys:
                    item.evidence += "; matched to the current read's dentry"
                else:
                    item.evidence += "; function calls only, path not correlated"
    analyses = [_field_evidence(prog, item) for item in interfaces]
    if selected_field:
        yield _selected_evidence(selected_field, interfaces, analyses)
    yield Observation(
        "3. kernel interfaces",
        f"{len(interfaces)} interfaces",
        "each interface keeps its own counts and stack; discovery and stack pass are separate runs",
        kind="heading",
    )
    for item in interfaces:
        function = item.function or "serving function unresolved"
        yield Observation(
            f"   {item.path or function}",
            function if item.path else f"{item.calls} calls",
            f"{item.calls} discovery calls; {item.evidence}",
            kind="heading",
        )
        if item.stacks:
            frames, hits = max(item.stacks, key=lambda pair: pair[1])
            yield Observation(
                "      stack",
                f"{hits} calls on this stack",
                f"{len(item.stacks)} distinct stacks; showing the most frequent; innermost last",
            )
            for number, frame in enumerate(reversed(frames), 1):
                yield Observation(f"      {number}. {frame}", _stack_where(prog, frame))
        if item.path and not item.probed:
            yield Observation(
                f"      discovered function: {function}",
                _where_at(prog, item.address)
                if item.address
                else _where(prog, item.function),
                "not probed; read stack only, no measured call to this function",
            )
    if not interfaces:
        yield Observation("   none observed", "", "none of the traced interfaces fired")
    yield Observation(
        "4. field references",
        "static analysis and catalog",
        "direct references and one level of helpers; not measured field accesses",
        kind="heading",
    )
    for item, analysis in zip(interfaces, analyses):
        yield from _reads_stage(
            prog,
            command,
            item.function,
            item.path,
            item.address,
            analysis=analysis,
            heading=f"   {item.path or item.function}",
        )


def _reads_stage(
    prog: Program,
    command: str,
    leaf: str,
    path: str,
    address: int = 0,
    analysis: FieldEvidence | None = None,
    heading: str = "4. field references",
) -> Iterator[Observation]:
    """Direct references, one level of helpers, and catalog links kept distinct."""
    if analysis is None:
        analysis = _field_evidence(prog, Interface(path, leaf, address))
    references = analysis.direct or []
    structs = sorted(
        {name.split(".")[0] for name, _ in references + analysis.catalog}
        | {name.split(".")[0] for name, _type, _helper in analysis.helpers}
    )
    where = (
        _where_at(prog, address) if address else _where(prog, leaf)
    ) or "no debuginfo"
    reason = (
        "source scan unavailable: source or unambiguous DWARF missing"
        if analysis.direct is None
        else f"parameter references in {leaf}; direct calls followed one level"
    )
    yield Observation(
        heading,
        ", ".join("struct " + tag for tag in structs)
        or ("unavailable" if analysis.direct is None else "none found"),
        f"{where}; {reason}",
        kind="heading",
    )
    for name, type_name in references:
        yield Observation(f"      {name}", type_name, "source")
    for name, type_name, helper in analysis.helpers:
        yield Observation(
            f"      {name}", type_name, f"source via {helper}; helper not probed"
        )
    source_names = {name for name, _type in references} | {
        name for name, _type, _helper in analysis.helpers
    }
    for name, catalog_command in analysis.catalog:
        if name not in source_names:
            yield Observation(
                f"      {name}", catalog_command.split("  #")[0], "catalog"
            )
    if analysis.unavailable:
        yield Observation(
            "      helpers unavailable",
            ", ".join(analysis.unavailable),
            "no source or unambiguous DWARF; inline helpers may have no standalone symbol",
        )
    if analysis.omitted:
        yield Observation(
            "      helper limit",
            f"{analysis.omitted} additional calls not followed",
            f"at most {MAX_HELPERS} direct helpers per function, in name order",
        )
    if analysis.direct is not None:
        yield Observation(
            "      note",
            "incomplete static analysis",
            "references include writes and conditional code; only unchanged structure parameters "
            "passed to direct helpers are followed; no measured field accesses",
        )


TRACE_PS = register_algorithm(
    Algorithm(
        key="trace_ps",
        label="trace: ps -e",
        subsystem="process",
        rule=(
            "ps has no process list to ask for. It opens one directory per task "
            "under /proc and reads a file from each, and every read runs a "
            "kernel function that formats a task_struct. Traced under bpftrace: "
            "the files opened, and the stack that served the reads."
        ),
        doc="One ps -e traced from the command to the struct it reads.",
        analyse=lambda prog: command_trace(prog, COMMAND),
        columns=("stage", "detail", "evidence"),
        background=True,
    )
)

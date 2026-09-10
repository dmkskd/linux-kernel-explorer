"""What the kernel runs when a userspace command is run.

``userspace.py`` answers "how would I read this without a debugger" with a
command. That is where most explanations stop, and it hides the part worth
seeing: ps is not asking the kernel for a process list, it is opening a few
hundred files, and each read runs a function that formats a task_struct.

Four stages for one command:

    1. the command itself
    2. the files it opens, counted by path
    3. the kernel entry point it used, and the stack that served it
    4. what that function reads, from its source and from the catalog

Stages 2 and 3 are measured on this kernel rather than listed. Stage 4 pairs a
scan of the leaf's own source with the table the userspace column already keeps,
and marks which is which.

Not every command reads a file. One discovery pass covers the interfaces the
catalog's commands actually use, and the leaf comes back from whichever fired:

    seq_read_iter        a file read: ps, grep, awk, cat
    vfs_readlink         readlink /proc/<pid>/exe, ls -l on a symlink
    iterate_dir          ls of a directory
    vfs_statx            stat, and ls -l per entry
    rtnetlink_rcv_msg    ip
    inet_diag_dump       ss
    __netlink_dump_start any netlink dump

``catalog/procfs.py`` maps a /proc path to the function serving it, which spares
a second run when the command names its file. It is a shortcut, not a
requirement: a file read with no entry in that table resolves its leaf from the
seq_file the kernel is holding.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterator

from drgn import Program

from ..catalog.procfs import SERVED_BY, ProcFile, fields_from, path_from_dentry
from ..core import ctypes as ct
from ..core.probe import parse_stacks, trace_command
from ..core.source import KernelSource
from .algorithm import Algorithm, Observation, register_algorithm

COMMAND = "ps -e"

# Every interface the catalog's commands reach the kernel through. Each is the
# lowest point that still names what was asked for: the dentry for a file, the
# netlink handler for a dump. Probing one syscall entry instead would report
# the same function for every command.
DISCOVER = """
kprobe:do_sys_openat2 /comm == "{comm}"/ {{ @opens[str(uptr(arg1))] = count(); }}
kprobe:seq_read_iter /comm == "{comm}"/ {{
  $i = (struct kiocb *)arg0;
  $d = $i->ki_filp->f_path.dentry;
  @reads[str($d->d_parent->d_parent->d_name.name),
         str($d->d_parent->d_name.name), str($d->d_name.name)] = count();
  $m = (struct seq_file *)$i->ki_filp->private_data;
  @shows[(uint64)$m->op->show, (uint64)$m->private] = count();
}}
kprobe:vfs_readlink /comm == "{comm}"/ {{ @entry[probe] = count(); }}
kprobe:iterate_dir /comm == "{comm}"/ {{ @entry[probe] = count(); }}
kprobe:vfs_statx /comm == "{comm}"/ {{ @entry[probe] = count(); }}
kprobe:rtnetlink_rcv_msg /comm == "{comm}"/ {{ @entry[probe] = count(); }}
kprobe:inet_diag_dump /comm == "{comm}"/ {{ @entry[probe] = count(); }}
kprobe:__netlink_dump_start /comm == "{comm}"/ {{ @entry[probe] = count(); }}
"""

# Second run, once the leaf is known: the same opens, plus the stack that
# reached it.
STACK = """
kprobe:do_sys_openat2 /comm == "{comm}"/ {{ @opens[str(uptr(arg1))] = count(); }}
kprobe:{function} /comm == "{comm}"/ {{ @serves[kstack(12)] = count(); }}
"""

_PID_PATH = re.compile(r"^/proc/\d+")

# The same substitution inside a command line, for comparing what the command
# names against what it was measured reading.
_NAMED_PID = re.compile(r"/proc/\d+/")

# "task->comm", "p->mm". The name on the left has to be a parameter of the
# function being scanned, or this matches locals and says nothing about the
# structures the caller handed in.
_FIELD_READ = re.compile(r"\b(\w+)->(\w+)")

_SOURCE = KernelSource()


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
        f"{sum(count for _path, count in opens)} files",
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


def _files_read(sections: dict, command: str) -> list[tuple[str, int]]:
    """Every /proc path the command read, the one it is about first.

    Read counts alone rank the wrong file. ``grep VmPin /proc/1/status`` reads
    the status file once and its own ``/proc/self/maps`` twice while starting
    up, so the busiest file is the one nobody asked about. A path the command
    names outright wins regardless of count; everything else falls back to it.
    """
    named = _NAMED_PID.sub("/proc/<pid>/", command)
    counts: dict[str, int] = {}
    for label, value, _bar in sections["reads"].rows if "reads" in sections else []:
        parts = [part.strip() for part in label.split(",")]
        if len(parts) != 3:
            continue
        path = path_from_dentry(*parts)
        try:
            count = int(value)
        except ValueError:
            continue
        if path:
            counts[path] = counts.get(path, 0) + count
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
        lambda: container_of(inode, "struct proc_inode", "vfs_inode")
        .op.proc_show.value_(),
        0,
    )
    name = _symbol(prog, address)
    return (name, address) if name else None


def _measured_leaf(prog: Program, sections: dict) -> tuple[str, int]:
    """The show function the kernel is holding for the file being read.

    A file with its own iterator keeps the real one in ``m->op->show``. A
    ``single_open`` file under /proc/<pid> keeps ``proc_single_show`` there and
    the interesting one behind the inode, so unwrap that: the probe records both
    pointers and this reads the second only when the first says to.
    """
    best = ("", 0, 0)
    for label, value, _bar in sections["shows"].rows if "shows" in sections else []:
        parts = [part.strip() for part in label.split(",")]
        if len(parts) != 2:
            continue
        try:
            show, private, count = int(parts[0]), int(parts[1]), int(value)
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


# A netlink handler means the command asked the kernel a question rather than
# read a file. ss -tanp does both: the socket list comes over netlink, and the
# /proc reads are how it puts a process name next to each socket. The question
# is the point, so it outranks the reads.
NETLINK = frozenset(
    {"inet_diag_dump", "rtnetlink_rcv_msg", "__netlink_dump_start"}
)


def _tally(pairs: list[tuple[str, int]], limit: int = 3) -> str:
    """"vfs_statx 160, vfs_readlink 156, iterate_dir 2", for the evidence cell.

    The counts are calls during the discovery run, and the cell carrying this
    says so: a bare number beside a function name explains nothing.
    """
    return ", ".join(f"{name} {count}" for name, count in pairs[:limit])


def _entry_points(sections: dict) -> list[tuple[str, int]]:
    """Every non-file interface that fired, busiest first."""
    found: list[tuple[str, int]] = []
    for label, value, _bar in sections["entry"].rows if "entry" in sections else []:
        try:
            found.append((label.split(":")[-1], int(value)))
        except ValueError:
            continue
    return sorted(found, key=lambda pair: pair[1], reverse=True)


def _reads_from_source(
    prog: Program, function: str, address: int = 0
) -> list[tuple[str, str]]:
    """Struct fields the leaf's own source reads, by scanning its body.

    The parameter names and their types come from DWARF, so ``task->flags`` in
    the body is reported as ``task_struct.flags`` rather than guessed at. This
    is a floor and not a list: a field read through a helper, as utime is
    through task_utime(), appears nowhere in this function's text.
    """
    found = _location_at(prog, address) if address else _location(prog, function)
    if not found:
        return []
    path, line = found
    lines = _SOURCE.read(path)
    if not lines:
        return []

    # From the opening line to the first line that is a closing brace in the
    # first column, which is where a kernel function ends.
    body: list[str] = []
    for text in lines[line - 1 :]:
        body.append(text)
        if text.startswith("}"):
            break

    try:
        parameters = {
            p.name: str(p.type) for p in prog.function(function).type_.parameters
        }
    except Exception:  # noqa: BLE001 - a function without DWARF has no names
        return []

    seen: set[tuple[str, str]] = set()
    for text in body:
        for name, field in _FIELD_READ.findall(text):
            type_name = parameters.get(name, "")
            tag = type_name.removeprefix("struct ").removesuffix(" *").strip()
            if type_name.startswith("struct ") and type_name.endswith("*"):
                seen.add((f"{tag}.{field}", type_name))
    return sorted(seen)


def command_trace(
    prog: Program,
    command: str,
    served: ProcFile | None = None,
    origin: str = "",
) -> Iterator[Observation]:
    """The four stages for one command, measured on this kernel.

    ``served`` short-circuits discovery when the command names a file the
    catalog knows. Everything else runs the command twice: once to find the
    kernel entry point it used, once to record the stack that reached it.

    ``origin`` is the row the command was taken from. The trace replaces that
    view, so without it the first stage can only refer to a row that is no
    longer on screen.
    """
    # The comm filter needs the program that runs, not the pipeline: in
    # "awk … /proc/1/stat | head" it is awk that reads the file.
    comm = Path(command.split("|")[0].strip().split()[0]).name[:15]

    # Filled in by the first trace: a command that has to be killed is one the
    # reader should know about, since everything below covers only the seconds
    # it was allowed to run.
    limit: list[str] = []

    yield Observation(
        "1. command",
        command,
        f"userspace column of {origin}" if origin else "the command traced",
        kind="heading",
    )

    path = served.path if served is not None else ""
    leaf = served.function if served is not None else ""
    address = 0
    how = "file named by the command"
    others: list[tuple[str, int]] = []

    if not leaf:
        first = trace_command(DISCOVER.format(comm=comm), command)
        if first.error:
            yield Observation("trace failed", first.error, "", kind="result")
            return
        found = {section.name: section for section in first.sections}
        if first.stopped:
            limit.append("capped: the command does not exit on its own")

        files = _files_read(found, command)
        entries = _entry_points(found)
        netlink = [pair for pair in entries if pair[0] in NETLINK]

        if netlink:
            leaf, _hits = netlink[0]
            # Name the alternatives and their call counts: "busiest" means
            # nothing without the tally it won.
            how = f"netlink; discovery calls: {_tally(netlink)}"
            if files:
                # Said out loud rather than dropped: the file reads are real,
                # they are just not the question the command asked.
                aside = ", ".join(f"{p} {n}" for p, n in files[:2])
                how += f"; also read {aside}"
        elif files:
            path, hits = files[0]
            others = files[1:]
            known = SERVED_BY.get(path)
            if known is not None:
                leaf, how = known.function, f"{path}, {hits} reads; leaf from the catalog"
            else:
                leaf, address = _measured_leaf(prog, found)
                how = f"{path}, {hits} reads; leaf from the seq_file"
        elif entries:
            leaf, _hits = entries[0]
            # Only say which won when something else was in the running.
            contest = "; most frequent traced" if len(entries) > 1 else ""
            how = f"no file read; discovery calls: {_tally(entries)}{contest}"
        else:
            leaf = ""

        if not leaf:
            yield from _opens_stage(command, found)
            yield Observation(
                "3. kernel entry point",
                "none observed",
                "none of the probed interfaces fired for this command",
                kind="result",
            )
            return

    # A static function's name can be ambiguous, and bpftrace refuses to attach
    # to one that is. Probe the caller instead and put the leaf back on the end
    # of the stack: it is known from the seq_file, just not probeable by name.
    probe, unprobeable = leaf, ""
    if not _probeable(prog, leaf):
        probe = "seq_read_iter" if path else ""
        unprobeable = leaf
        if not probe:
            yield Observation(
                "3. kernel entry point",
                f"{leaf} at {_where_at(prog, address) or 'no debuginfo'}",
                "name is ambiguous, so no probe attaches, and no caller of "
                "it is in the probe set",
                kind="result",
            )
            yield from _reads_stage(prog, command, leaf, path, address)
            return

    result = trace_command(STACK.format(comm=comm, function=probe), command)
    if result.error:
        yield Observation("trace failed", result.error, "", kind="result")
        return

    sections = {section.name: section for section in result.sections}
    if result.stopped and not limit:
        limit.append("capped: the command does not exit on its own")
    if limit:
        yield Observation("   note", "a slice, not a whole run", limit[0])
    yield from _opens_stage(command, sections)

    stacks = parse_stacks(result.raw).get("serves", [])
    if not stacks:
        yield Observation(
            "3. kernel entry point",
            f"{probe} not called",
            "probe attached, never fired: the second run differed from the "
            "first",
            kind="result",
        )
        return

    frames, hits = max(stacks, key=lambda pair: pair[1])
    also = "; other files read are listed below the stack" if others else ""
    plural = "s" if hits != 1 else ""
    detail = f"{hits} call{plural} to {probe}"
    evidence = f"{how}; stack at kprobe:{probe}, innermost last{also}"
    if unprobeable:
        detail = f"{hits} call{plural} reaching {unprobeable}"
        evidence = (
            f"{how}; {unprobeable} is not probeable by name, so the stack is "
            f"taken at kprobe:{probe} and the leaf appended{also}"
        )
    yield Observation("3. kernel entry point", detail, evidence, kind="heading")
    # bpftrace records leaf first; a call path reads the other way.
    for number, frame in enumerate(reversed(frames), start=1):
        yield Observation(
            f"   {number}. {frame}", _where(prog, frame.split("+")[0]), ""
        )
    if unprobeable:
        yield Observation(
            f"   {len(frames) + 1}. {unprobeable}",
            _where_at(prog, address) or "no debuginfo",
            "from the seq_file; not probed, the name is ambiguous",
        )
    for other, count in others[:3]:
        yield Observation(f"   also read {other}", f"{count} reads", "")

    yield from _reads_stage(prog, command, leaf, path, address)


def _reads_stage(
    prog: Program, command: str, leaf: str, path: str, address: int = 0
) -> Iterator[Observation]:
    """Stage 4: what the leaf reads, from its source and from the catalog."""
    from_source = _reads_from_source(prog, leaf, address)
    published = fields_from(path) if path else []
    # Spelled as C spells them. A bare "rq" is not obviously a type; the tables
    # key on the tag, and the tag alone reads as an abbreviation.
    structs = sorted(
        {"struct " + name.split(".")[0] for name, _ in from_source + published}
    )

    # The provenance goes in the heading, once. Repeating it on every field row
    # is what pushed the useful half of each row off the screen: eighteen rows
    # of the same sentence, each one truncated.
    where = (_where_at(prog, address) if address else _where(prog, leaf)) or "no debuginfo"
    if structs:
        sources = "source scan of " + leaf
        if published:
            sources += f", then the catalog for {path}"
        detail = ", ".join(structs)
    else:
        # "nothing resolved" states the outcome and hides the reason, and the
        # reason is the useful half: the scan reports fields reached through the
        # function's parameters, and a local says nothing about what the caller
        # passed in.
        detail = "none found"
        sources = f"no parameter of {leaf} is dereferenced in its body"
        if not path:
            sources += "; no /proc file was read, so the catalog has no list"
    yield Observation(
        "4. what it reads", detail, f"{where}; {sources}", kind="heading"
    )
    for name, type_name in from_source:
        yield Observation(f"   {name}", type_name, "source")
    if from_source:
        yield Observation(
            "   note",
            "incomplete",
            "a field read through a helper is not visible in this function",
        )
    for name, reads_it in published:
        if any(name == scanned for scanned, _type in from_source):
            continue
        yield Observation(f"   {name}", reads_it.split("  #")[0], "catalog")


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

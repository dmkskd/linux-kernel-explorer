"""Which kernel function publishes a /proc file.

``userspace.py`` says a struct field is "field 9 of /proc/<pid>/stat". That
answers where to read the value without a debugger, and stops there. This says
who puts it in the file: the function the kernel runs when something reads that
path, and the struct it formats.

Only the leaf is named here. The rest of the path from the read syscall down to
that function is measured, not listed, because a stack recorded on this kernel
is evidence and a stack written into a table is a claim about every kernel.
"""

from __future__ import annotations

import functools
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class ProcFile:
    """A /proc path, and the function that writes its contents."""

    path: str
    function: str
    tag: str
    doc: str
    # sysfs publishes a directory of one-value files rather than one file with
    # named fields, so its entry matches a path prefix instead of a whole path.
    prefix: bool = False


# Keyed by the path as it appears in the userspace commands, placeholders and
# all, so a command and the function serving it share one lookup string.
SERVED_BY: dict[str, ProcFile] = {
    "/proc/<pid>/stat": ProcFile(
        path="/proc/<pid>/stat",
        function="do_task_stat",
        tag="task_struct",
        doc="Formats one task as the single line ps and friends parse.",
    ),
    "/proc/<pid>/status": ProcFile(
        path="/proc/<pid>/status",
        function="proc_pid_status",
        tag="task_struct",
        doc="The same task in named lines, and the only place several "
            "mm_struct counters are published.",
    ),
    "/proc/<pid>/maps": ProcFile(
        path="/proc/<pid>/maps",
        function="show_map",
        tag="vm_area_struct",
        doc="One line per mapping, so the function runs once per vm_area_struct "
            "rather than once per read.",
    ),
    "/proc/<pid>/cmdline": ProcFile(
        path="/proc/<pid>/cmdline",
        function="proc_pid_cmdline_read",
        tag="mm_struct",
        doc="Reads the argument strings out of the task's own address space, "
            "between mm->arg_start and mm->arg_end.",
    ),
    "/proc/<pid>/comm": ProcFile(
        path="/proc/<pid>/comm",
        function="proc_task_name",
        tag="task_struct",
        doc="The 16-byte name stored in the task itself.",
    ),
    "/proc/<pid>/cgroup": ProcFile(
        path="/proc/<pid>/cgroup",
        function="proc_cgroup_show",
        tag="task_struct",
        doc="Walks the task's css_set to name its cgroup in each hierarchy.",
    ),
    "/proc/meminfo": ProcFile(
        path="/proc/meminfo",
        function="meminfo_proc_show",
        tag="zone",
        doc="System-wide page accounting, summed over the zones at read time.",
    ),
    "/proc/zoneinfo": ProcFile(
        path="/proc/zoneinfo",
        function="zoneinfo_show",
        tag="zone",
        doc="One block per zone, printed straight from struct zone.",
    ),
    "/proc/stat": ProcFile(
        path="/proc/stat",
        function="show_stat",
        tag="rq",
        doc="Per-CPU counters, including the context switches summed from every "
            "runqueue.",
    ),
    "/proc/schedstat": ProcFile(
        path="/proc/schedstat",
        function="show_schedstat",
        tag="rq",
        doc="One line per CPU, read from that CPU's runqueue.",
    ),
    "/proc/net/softnet_stat": ProcFile(
        path="/proc/net/softnet_stat",
        function="softnet_seq_show",
        tag="softnet_data",
        doc="Per-CPU receive backlog counters.",
    ),
    "/sys/class/net/": ProcFile(
        path="/sys/class/net/",
        function="sysfs_kf_seq_show",
        tag="net_device",
        doc="Every sysfs attribute is served by one function, which calls the "
            "attribute's own show method; the stack says which.",
        prefix=True,
    ),
}


# Programs whose whole job is reading one of the files above. ps takes no path
# on its command line, so the file it ends up in cannot be read off the command
# the way it can for a grep or an awk.
PROGRAM_READS: dict[str, str] = {
    "ps": "/proc/<pid>/stat",
}

_CONCRETE_PID = re.compile(r"/proc/\d+/")


@functools.lru_cache(maxsize=32)
def _ends_at(path: str) -> re.Pattern[str]:
    """Match ``path`` only where it ends: /proc/<pid>/stat is not /status."""
    return re.compile(re.escape(path) + r"(?![\w/])")


def served_by(command: str) -> ProcFile | None:
    """The /proc file a userspace command reads, if this catalog knows it.

    The command may carry a real pid, because the field rows substitute the one
    being browsed. Put the placeholder back before looking it up, so the same
    table answers for the row on screen and for the string it came from.
    """
    generic = _CONCRETE_PID.sub("/proc/<pid>/", command)
    for path, entry in SERVED_BY.items():
        if path in generic if entry.prefix else _ends_at(path).search(generic):
            return entry
    program = command.strip().split()[0] if command.strip() else ""
    path = PROGRAM_READS.get(program)
    return SERVED_BY.get(path) if path else None


def fields_from(path: str) -> list[tuple[str, str]]:
    """The struct fields whose userspace command reads this file.

    The path has to end where the match ends: /proc/<pid>/stat is a prefix of
    /proc/<pid>/status, and a plain substring test hands the stat file every
    field that is really published by status.
    """
    from .userspace import FIELD_COMMANDS

    entry = SERVED_BY.get(path)
    if entry is not None and entry.prefix:
        return sorted(
            (f"{tag}.{field}", command)
            for (tag, field), command in FIELD_COMMANDS.items()
            if path in command
        )

    ends = _ends_at(path)
    return sorted(
        (f"{tag}.{field}", command)
        for (tag, field), command in FIELD_COMMANDS.items()
        if ends.search(command)
    )


def path_from_dentry(grandparent: str, parent: str, name: str) -> str:
    """Rebuild a /proc path from the dentry names measured during a read.

    ps and pgrep open the task directory and then read the files inside it by
    name, so the path is never on the command line and cannot be recovered from
    it. The dentry chain has it. Only procfs is rebuilt here: everything else in
    the tables names its own path in the command, so there is nothing to
    recover.
    """
    if parent.isdigit():
        return f"/proc/<pid>/{name}"
    if parent == "/":
        return f"/proc/{name}"
    if parent == "net" and grandparent.isdigit():
        # /proc/net is a symlink to /proc/self/net, so the parent chain of a
        # file under it runs through the reading task's own directory. Measured:
        # cat /proc/net/softnet_stat reads a dentry whose grandparent is a pid.
        return f"/proc/net/{name}"
    return ""

"""The kernel function that writes each /proc file.

``userspace.py`` maps a struct field to its location in a file, such as "field
9 of /proc/<pid>/stat". This module names the function the kernel runs when the
file is read, and the struct it formats.

Only that function is listed. The call path from the read syscall to it is
measured at runtime instead, because a stack recorded on the running kernel is
evidence, and a stack written into a table is a claim about every kernel.
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
        doc="sysfs_kf_seq_show dispatches to the selected attribute's show method; "
            "the recorded stack identifies that method.",
        prefix=True,
    ),
}


_CONCRETE_PID = re.compile(r"/proc/\d+/")


@functools.lru_cache(maxsize=32)
def _ends_at(path: str) -> re.Pattern[str]:
    """Match ``path`` only where it ends: /proc/<pid>/stat is not /status."""
    return re.compile(re.escape(path) + r"(?![\w/])")


def served_by(command: str) -> ProcFile | None:
    """The /proc file a userspace command reads, if the catalog lists it.

    The command may contain a numeric pid, substituted by the field row being
    browsed. The pid is replaced with ``<pid>`` before the lookup, so the same
    table entry matches both the displayed command and its template.
    """
    generic = _CONCRETE_PID.sub("/proc/<pid>/", command)
    for path, entry in SERVED_BY.items():
        if path in generic if entry.prefix else _ends_at(path).search(generic):
            return entry
    return None


def fields_from(path: str) -> list[tuple[str, str]]:
    """The struct fields whose userspace command reads this file.

    The path has to end where the match ends: /proc/<pid>/stat is a prefix of
    /proc/<pid>/status, and a substring test would assign to the stat file
    every field that status publishes.
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
    name, so the path does not appear on the command line. The dentry chain
    records it. Only procfs paths are rebuilt here, because every other table
    entry has its path in the command.
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

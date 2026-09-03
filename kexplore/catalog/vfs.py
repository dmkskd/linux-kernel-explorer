"""VFS: mounts, superblocks, and a process's open files."""

from __future__ import annotations

from drgn import Program
from drgn.helpers.linux.fs import for_each_mount, mount_dst, mount_fstype
from drgn.helpers.linux.list import list_for_each_entry
from drgn.helpers.linux.pid import find_task

from .registry import Entry, Subsystem, register
from .format import as_text, task_comm
from .walk import files_of, open_files, path_of


def mounts(prog: Program):
    for mount in for_each_mount(prog):
        dst = as_text(mount_dst(mount))
        fstype = as_text(mount_fstype(mount))
        yield f"{dst}  [{fstype}]", mount


def super_blocks(prog: Program):
    for sb in list_for_each_entry(
        "struct super_block", prog["super_blocks"].address_of_(), "s_list"
    ):
        fstype = sb.s_type.name.string_().decode("utf-8", "replace")
        yield f"{fstype}  {sb.s_id.string_().decode('utf-8', 'replace')}", sb


def init_files(prog: Program):
    """Open files of pid 1 itself -- just that one task's fd table.

    Not its descendants: pid 1's own fd table, the way /proc/1/fd shows it.
    """
    task = find_task(prog, 1)
    for fd, file in files_of(task):
        yield f"fd {fd:>3}  {path_of(file)}", file


def all_files(prog: Program):
    """Every open fd on the system -- the lsof view.

    One struct file can appear many times: fork() shares the fd table, and
    dup() adds descriptors to the same file. Each row is one (task, fd) pair,
    so repeats are real rather than a bug.
    """
    for task, fd, file in open_files(prog):
        yield (
            f"{task_comm(task)}[{task.pid.value_()}] fd {fd:>3}  {path_of(file)}",
            file,
        )


def unique_files(prog: Program):
    """Distinct struct file objects, with how many fds point at each."""
    counts: dict[int, tuple[int, object]] = {}
    for _task, _fd, file in open_files(prog):
        address = file.value_()
        count, obj = counts.get(address, (0, file))
        counts[address] = (count + 1, obj)
    for address, (count, file) in counts.items():
        shared = f"  ({count} fds)" if count > 1 else ""
        yield f"{path_of(file)}{shared}", file


register(
    Subsystem(
        key="vfs",
        label="vfs",
        doc="The filesystem layer: mount tree, superblocks, open file tables.",
        entries=[
            Entry("mounts", "mounts", "Every struct mount in the init namespace.", mounts),
            Entry(
                "superblocks",
                "superblocks",
                "The global super_blocks list -- one per mounted filesystem.",
                super_blocks,
            ),
            Entry(
                "all_files",
                "all open files (lsof view)",
                "Every (task, fd) pair on the system, with the resolved path.",
                all_files,
            ),
            Entry(
                "unique_files",
                "distinct open files",
                "One row per struct file, showing how many fds share it.",
                unique_files,
            ),
            Entry(
                "files_pid1",
                "open files of init (pid 1 only)",
                "Just pid 1's own fd table -- what /proc/1/fd shows, not its children.",
                init_files,
            ),
        ],
    )
)

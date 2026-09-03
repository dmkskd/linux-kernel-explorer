"""Walks that more than one subsystem needs.

The fd table is the obvious one: vfs lists it as open files, socket and skb
filter it down to the sockets, and a task's ``open files`` link walks it again.
All four want the same loop, including the same tolerance -- a task can exit
while we are walking it, and reading its torn-down fd table faults. That
recovery is the part worth having in one place: divergence there means one view
crashing where the others survive.
"""

from __future__ import annotations

from typing import Iterator

import drgn
from drgn import Object, Program
from drgn.helpers.linux.fs import d_path, for_each_file
from drgn.helpers.linux.pid import for_each_task

from ..core import ctypes as ct
from .format import as_text


def files_of(task: Object) -> Iterator[tuple[int, Object]]:
    """(fd, file) for one task, skipping NULL entries."""
    for fd, file in for_each_file(task):
        if file.value_():
            yield fd, file


def open_files(prog: Program) -> Iterator[tuple[Object, int, Object]]:
    """(task, fd, file) for every open descriptor on the system.

    One struct file can appear many times: fork() shares the fd table, and
    dup() adds descriptors to the same file. Each tuple is one (task, fd) pair,
    so repeats are real rather than a bug.
    """
    for task in for_each_task(prog):
        try:
            yield from ((task, fd, file) for fd, file in files_of(task))
        except drgn.FaultError:
            # The task exited mid-walk and its fd table is being torn down.
            continue


def socket_files(prog: Program) -> Iterator[tuple[Object, int, Object]]:
    """The subset of open_files() whose files are sockets.

    A struct file is a socket iff its f_op is socket_file_ops, which is read
    once here rather than per file.
    """
    socket_file_ops = prog["socket_file_ops"].address_of_()
    for task, fd, file in open_files(prog):
        if file.f_op == socket_file_ops:
            yield task, fd, file


def path_of(file: Object) -> str:
    """The path behind a struct file, or "?" if it cannot be resolved.

    An anonymous inode (a pipe, an eventfd) has no path, and d_path on a
    half-torn-down file faults.
    """
    return ct.safe(lambda: as_text(d_path(file.f_path.address_of_())), "?")

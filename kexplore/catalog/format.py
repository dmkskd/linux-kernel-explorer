"""Formatting helpers shared by the subsystem modules.

drgn helpers return ``bytes`` for kernel strings, and most providers need the
same two conversions, which are defined here once.
"""

from __future__ import annotations

from drgn import Object


def as_text(value) -> str:
    """Decode a kernel string, tolerating anything that is not valid UTF-8."""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def task_comm(task: Object) -> str:
    """The task's short name, as ``ps`` shows it."""
    return task.comm.string_().decode("utf-8", "replace")

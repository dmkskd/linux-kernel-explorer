"""Small formatting helpers shared by the subsystem modules.

drgn helpers return ``bytes`` for kernel strings, and nearly every provider
needs the same two conversions, so they live here rather than being redefined
per module.
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

"""Detect supported kernel layouts from DWARF, independently of releases."""
from drgn import Program, Type


def type_has_member(type_: Type, name: str) -> bool:
    try:
        type_.member(name)
    except LookupError:
        return False
    return True


def has_member(prog: Program, type_name: str, name: str) -> bool:
    try:
        type_ = prog.type(type_name)
    except LookupError:
        return False
    return type_has_member(type_, name)


def mutex_wait_support(prog: Program) -> str | None:
    if not has_member(prog, "struct task_struct", "blocked_on"):
        return ("This kernel does not record mutex wait targets in task_struct.blocked_on; "
                "an empty list would not mean that no tasks are waiting.")
    return None


def hardware_queue_support(prog: Program) -> str | None:
    if not any(has_member(prog, "struct request_queue", member)
               for member in ("queue_hw_ctx", "hctx_table")):
        return "No supported hardware queue layout: expected queue_hw_ctx or hctx_table."
    return None


def futex_support(prog: Program) -> str | None:
    try:
        data = prog["__futex_data"]
    except LookupError:
        return "No supported futex hash layout: __futex_data is unavailable."
    if not type_has_member(data.type_, "queues") or not any(
        type_has_member(data.type_, member) for member in ("hashsize", "hashmask")
    ):
        return "No supported futex hash layout: expected queues and hashsize or hashmask."
    return None

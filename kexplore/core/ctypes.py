"""Type inspection and one-line value rendering for drgn objects.

A read of live kernel memory can fault at any time: a task exits, a pointer
is stale, or RCU frees an object during the read. ``safe(fn, default)`` runs
one read and, if it raises one of the exceptions in ``READ_ERRORS``
(``drgn.FaultError``, ``drgn.OutOfBoundsError``, ``LookupError``,
``ValueError``, ``TypeError``, ``OverflowError``), returns ``default``, which
is ``"<fault>"`` unless the caller passes another value. Any other exception
propagates, because it indicates a bug in kexplore.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import drgn
from drgn import Object, Type, TypeKind

AGGREGATE_KINDS = frozenset({TypeKind.STRUCT, TypeKind.UNION, TypeKind.CLASS})

# Errors raised when memory is not readable at the time of the read, as opposed
# to errors in kexplore.
READ_ERRORS = (
    drgn.FaultError,
    drgn.OutOfBoundsError,
    LookupError,
    ValueError,
    TypeError,
    OverflowError,
)

MAX_STRING = 64


def strip(type_: Type) -> Type:
    """Resolve typedefs down to the underlying type."""
    while type_.kind == TypeKind.TYPEDEF:
        type_ = type_.type
    return type_


def safe(fn: Callable[[], Any], default: Any = "<fault>") -> Any:
    try:
        return fn()
    except READ_ERRORS:
        return default


def type_name(type_: Type) -> str:
    """Short type name, keeping the kernel's typedef spelling."""
    return str(type_.type_name())


def is_char(type_: Type) -> bool:
    t = strip(type_)
    return t.kind == TypeKind.INT and t.size == 1 and "char" in t.name


def is_aggregate(type_: Type) -> bool:
    return strip(type_).kind in AGGREGATE_KINDS


def struct_type(type_: Type) -> Type | None:
    """The aggregate behind ``type_``, seeing through typedefs and pointers.

    Returns None if there is no tagged struct or union. ``.tag`` raises on
    pointer and scalar types, so callers must check the result first.
    """
    stripped = strip(type_)
    if stripped.kind == TypeKind.POINTER:
        stripped = strip(stripped.type)
    return stripped if stripped.kind in AGGREGATE_KINDS else None


def tag_of(type_: Type) -> str | None:
    aggregate = struct_type(type_)
    return aggregate.tag if aggregate is not None else None


def member_names(type_: Type) -> frozenset[str]:
    """Every member name reachable on ``type_``, flattening anonymous members.

    drgn resolves members of an anonymous struct or union on the parent, so
    every name here is accepted by ``obj.member_()``. The source scanner
    validates its parse against this set.
    """
    names: set[str] = set()

    def walk(current: Type) -> None:
        for member in current.members or ():
            if member.name is None:
                inner = strip(member.type)
                if inner.kind in AGGREGATE_KINDS:
                    walk(inner)
                continue
            names.add(member.name)

    stripped = strip(type_)
    if stripped.kind in AGGREGATE_KINDS and stripped.members:
        walk(stripped)
    return frozenset(names)


def _read_string(obj: Object) -> str:
    raw = obj.string_()
    text = raw[:MAX_STRING].decode("utf-8", "replace")
    suffix = "…" if len(raw) > MAX_STRING else ""
    return f'"{text}{suffix}"'


def _symbol_for(prog: drgn.Program, address: int) -> str | None:
    try:
        sym = prog.symbol(address)
    except LookupError:
        return None
    offset = address - sym.address
    return sym.name if offset == 0 else f"{sym.name}+{offset:#x}"


def _render_int(obj: Object, type_: Type) -> str:
    value = obj.value_()
    # Small values are shown in decimal. Large values are usually addresses,
    # masks or flag words, so hexadecimal is shown as well.
    if abs(value) >= 0x1000:
        return f"{value} ({value:#x})"
    return str(value)


def _render_pointer(obj: Object, type_: Type) -> str:
    address = obj.value_()
    if address == 0:
        return "NULL"

    target = strip(type_.type)
    if is_char(target):
        text = safe(lambda: _read_string(obj), None)
        if text is not None:
            return f"{address:#x} {text}"
    if target.kind == TypeKind.FUNCTION:
        name = _symbol_for(obj.prog_, address)
        if name:
            return f"{address:#x} <{name}>"
    return f"{address:#x}"


def _render_array(obj: Object, type_: Type) -> str:
    length = type_.length
    if is_char(type_.type):
        text = safe(lambda: _read_string(obj), None)
        if text is not None:
            return text
    if length is None:
        return "[]"
    return f"[{length}]"


def _render_aggregate(type_: Type) -> str:
    """Summarise an embedded struct/union without expanding it.

    A zero-sized struct with no members is the kernel's idiom for a feature
    compiled out -- ``typedef struct { } netns_tracker;`` when
    CONFIG_NET_NS_REFCNT_TRACKER is off. Such a struct is rendered as compiled
    out, because "{…}" would indicate members that can be followed.
    """
    members = type_.members or ()
    if not members:
        return "{} zero-sized (feature compiled out)"
    count = len(members)
    return f"{{…}} {count} field{'s' if count != 1 else ''}"


def _render_enum(obj: Object, type_: Type) -> str:
    value = obj.value_()
    for enumerator in type_.enumerators or ():
        if enumerator.value == value:
            return f"{enumerator.name} ({value})"
    return str(value)


def render_value(obj: Object) -> str:
    """A compact one-line summary of ``obj``, suitable for a table cell."""

    def render() -> str:
        type_ = strip(obj.type_)
        kind = type_.kind

        if kind == TypeKind.POINTER:
            return _render_pointer(obj, type_)
        if kind == TypeKind.ARRAY:
            return _render_array(obj, type_)
        if kind in AGGREGATE_KINDS:
            return _render_aggregate(type_)
        if kind == TypeKind.ENUM:
            return _render_enum(obj, type_)
        if kind == TypeKind.BOOL:
            return "true" if obj.value_() else "false"
        if kind == TypeKind.INT:
            return _render_int(obj, type_)
        if kind == TypeKind.FLOAT:
            return str(obj.value_())
        if kind == TypeKind.FUNCTION:
            name = _symbol_for(obj.prog_, obj.address_ or 0)
            return f"<{name}>" if name else "<function>"
        if kind == TypeKind.VOID:
            return "void"
        return str(safe(obj.value_, "?"))

    return safe(render)


def address_of(obj: Object) -> str:
    return f"{obj.address_:#x}" if obj.address_ is not None else "<value>"

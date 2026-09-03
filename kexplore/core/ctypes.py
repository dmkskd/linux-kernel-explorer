"""Type inspection and one-line value rendering for drgn objects.

Everything here is defensive: reading live kernel memory can fault at any
moment (a task exits, a pointer is stale, RCU frees something under us). A
fault must never take down the explorer, so reads are wrapped and rendered as
``<fault>`` instead of raising.
"""

from __future__ import annotations

from typing import Any, Callable

import drgn
from drgn import Object, Type, TypeKind

AGGREGATE_KINDS = frozenset({TypeKind.STRUCT, TypeKind.UNION, TypeKind.CLASS})

# Errors that mean "this memory isn't readable right now", not "bug in the tool".
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
    """Short type name, keeping the typedef spelling the kernel actually uses."""
    return str(type_.type_name())


def is_char(type_: Type) -> bool:
    t = strip(type_)
    return t.kind == TypeKind.INT and t.size == 1 and "char" in t.name


def is_aggregate(type_: Type) -> bool:
    return strip(type_).kind in AGGREGATE_KINDS


def struct_type(type_: Type) -> Type | None:
    """The aggregate behind ``type_``, seeing through typedefs and pointers.

    Returns None if there's no tagged struct/union there -- ``.tag`` raises on
    pointer and scalar types, so callers must not reach for it blindly.
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

    drgn lets you read members of an anonymous struct/union straight off the
    parent, so these are all names that ``obj.member_()`` accepts -- and the
    set the source scanner validates its parse against.
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
    # Small numbers read better in decimal; anything large is almost always an
    # address, mask, or flag word, so show hex alongside.
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
    CONFIG_NET_NS_REFCNT_TRACKER is off. Showing "{…}" for those would promise
    contents that don't exist and can't be followed, so they say so instead.
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

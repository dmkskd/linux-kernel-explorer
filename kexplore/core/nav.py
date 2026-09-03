"""Navigation model: turn a drgn Object into rows you can walk into.

This is the layer that makes the explorer more than a pretty-printer. drgn
already formats a struct beautifully; what it doesn't give you is "put the
cursor on ``.mm`` and press enter", plus a breadcrumb trail you can back out
of. That's all this module is.

It holds real ``drgn.Object`` values rather than a serialized
form -- the TUI runs in the same process as drgn, so there is no wire format
to design. When a second frontend needs JSON, it serializes ``Row`` here
rather than reaching into drgn itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterable, Iterator

import drgn
from drgn import Object, TypeKind

from . import ctypes as ct

# Arrays and collections are truncated so that walking into `page[]` or a
# few hundred thousand tasks doesn't hang the UI.
MAX_ROWS = 512

# What every provider in the catalog looks like once its argument is bound:
# either one object, or labelled objects to list. Entries, links and
# walkthrough steps all look like this, and all resolve through ``collect``.
Produce = Callable[[], "Object | Iterable[tuple[str, Object]]"]

# Set once at startup from arch.cache_line_size(), because a Row has no program
# to ask and the answer is a property of the machine, not of the field.
CACHE_LINE = 64


def set_cache_line(size: int) -> None:
    global CACHE_LINE
    CACHE_LINE = size or 64


@dataclass(frozen=True)
class Row:
    """One line in the detail pane.

    ``kind`` says what the row *is*, and is the only place that is recorded:

    * ``field``    -- a real struct member
    * ``link``     -- a curated relationship; supplies ``expand``, because
      traversing it may mean walking a list rather than dereferencing a pointer
    * ``derived``  -- a computed value, a heading, or an informational line
    * ``error``    -- something failed and the message is the row's name
    * ``truncated`` -- the list stops here; there was more

    ``note`` is a short annotation printed after the name, and nothing else:
    the bit-field width of a member. It used to double as the error marker,
    which put the word "error" on screen next to the message.
    """

    name: str
    obj: Object | None
    type_name: str
    value: str
    followable: bool
    note: str = ""
    kind: str = "field"
    doc: str = ""
    expand: Callable[[], list["Row"]] | None = None
    # Views that are a matrix rather than field/type/value supply their own
    # cells; when set these replace the three default columns.
    cells: tuple[str, ...] | None = None
    # Whether those cells still stand for a kernel object, and so want the
    # follow marker in front of them. A measurement's cells do not.
    marked: bool = False
    # Columns the expanded view wants, when they differ from this frame's.
    expand_columns: tuple[str, ...] | None = None
    # The catalog item this row stands for, when the row is an index entry
    # rather than a value read from memory. Following it opens that item the
    # same way the sidebar does, through frames.plan_for.
    item: object | None = None
    # The C type, kept when the type column is showing something else.
    original_type: str = ""
    # Byte offset and size within the containing struct. C lays members out in
    # declaration order, so these show where a field physically sits.
    offset: int | None = None
    size: int | None = None
    # In-place expansion: how far this row is nested under the one that opened
    # it, and whether its own children are currently spliced in below it.
    depth: int = 0
    expanded: bool = False

    @property
    def placement(self) -> str:
        """Offset and size, with the cache line the field starts in.

        Line numbers are relative to the start of the struct, so they are real
        cache lines only when the allocation is line-aligned -- which it is for
        slab objects, the ones worth looking at this way.
        """
        if self.offset is None:
            return ""
        return f"{self.offset}+{self.size} L{self.offset // CACHE_LINE}"

    @property
    def display_name(self) -> str:
        return f"{self.name}{f'  {self.note}' if self.note else ''}"

    @property
    def marker(self) -> str:
        if self.expanded:
            return "▾"
        if self.kind == "link":
            return "→"
        return "▸" if self.followable else " "

    def children(self) -> list["Row"]:
        """The rows one level down, for opening this row without leaving the frame.

        A link brings its own expansion, because reaching the other end may
        mean walking a list rather than dereferencing a pointer.
        """
        if self.expand is not None:
            return list(self.expand())
        if self.obj is None:
            return []
        target = follow(self.obj)
        if target is None:
            return []
        return rows_for(target)


@dataclass
class Node:
    """A position in the exploration: a labelled object plus how we got here."""

    label: str
    obj: Object
    doc: str = ""

    @property
    def type_name(self) -> str:
        return ct.type_name(self.obj.type_)

    def rows(self) -> list[Row]:
        return rows_for(self.obj)


@dataclass
class Collection:
    """A labelled set of objects -- per-CPU runqueues, every mount, etc.

    Providers yield lazily so an expensive walk can be truncated without
    paying for the whole thing.
    """

    label: str
    items: list[tuple[str, Object]] = field(default_factory=list)
    truncated: bool = False
    error: str | None = None


def collect(label: str, produce: Produce, limit: int = MAX_ROWS) -> Collection:
    """Run a provider, truncating at ``limit`` and trapping its failures.

    Every catalog concept that yields objects -- a subsystem entry, a curated
    link, a walkthrough step -- goes through here, so all three truncate at the
    same size and report a failure the same way. Helper availability shifts
    with kernel version and config, and a walk can fault half way through
    because the task it was walking exited, so an error is a value rather than
    an exception: one bad edge greys out one row.
    """
    try:
        result = produce()
    except Exception as exc:  # noqa: BLE001 - surface any helper failure as UI text
        return Collection(label, error=f"{type(exc).__name__}: {exc}")

    if isinstance(result, Object):
        return Collection(label, [(label, result)])

    items: list[tuple[str, Object]] = []
    truncated = False
    try:
        for item_label, obj in result:
            if len(items) >= limit:
                truncated = True
                break
            items.append((item_label, obj))
    except Exception as exc:  # noqa: BLE001 - a walk can fault mid-iteration
        return Collection(label, items, truncated, f"{type(exc).__name__}: {exc}")

    return Collection(label, items, truncated)


def _address(obj: Object) -> str:
    """``obj``'s value as hex, for a pointer. Empty for anything else."""
    try:
        return f"{obj.value_():#x}"
    except (TypeError, ValueError):
        return ""


def _item_row(label: str | tuple[str, ...], obj: Object) -> Row:
    """One item of a collection.

    A provider that yields a tuple is describing columns of its own -- pid,
    state, command -- rather than one label. Those become the row's cells, and
    the frame supplies the header to match. A provider that yields a string
    gets the field/type/value layout, which is what a list of structures with
    nothing in particular to say about them wants.
    """
    if not isinstance(label, tuple):
        return Row(
            name=label,
            obj=obj,
            type_name=ct.type_name(obj.type_),
            value=ct.render_value(obj),
            followable=can_follow(obj),
        )
    cells = tuple(str(cell) for cell in label)
    # The name is never displayed as a cell, but it is what the filter matches
    # and what names the frame once the row is followed. The type and the
    # address are dropped from it because the path line above the table already
    # carries both, and a breadcrumb reading "1 S systemd struct task_struct *
    # 0xffff0000c086ca00" is unreadable.
    noise = {ct.type_name(obj.type_), _address(obj)}
    return Row(
        name=" ".join(
            cell.strip() for cell in cells if cell.strip() and cell not in noise
        ),
        obj=obj,
        type_name=ct.type_name(obj.type_),
        value=ct.render_value(obj),
        followable=can_follow(obj),
        cells=cells,
        marked=True,
    )


def collection_rows(collection: Collection) -> list[Row]:
    """A resolved collection as rows: its items, or why there are none.

    The empty and truncated cases get a row of their own rather than an empty
    table, because "this really is empty" and "the walk failed" look identical
    otherwise.
    """
    if collection.error:
        return [Row(collection.error, None, "", "", False, kind="error")]

    rows = [_item_row(label, obj) for label, obj in collection.items]
    if collection.truncated:
        rows.append(Row("… truncated", None, "", "", False, kind="truncated"))
    if not rows:
        rows.append(Row("(empty)", None, "", "", False, kind="derived"))
    return rows


def _member_rows(obj: Object, type_: drgn.Type, base: int = 0) -> Iterator[Row]:
    """Rows for a struct/union, flattening anonymous members.

    drgn lets you reach members of an anonymous struct/union directly from the
    parent, so flattening avoids needing a synthetic object for the anonymous
    container.

    ``base`` is the bit offset of ``type_`` within the struct the rows are
    being shown for. drgn reports ``bit_offset`` relative to the immediately
    enclosing type, so flattening an anonymous member has to carry its offset
    down, or every field inside it reports a position relative to the union it
    happens to sit in.
    """
    for member in type_.members or ():
        if member.name is None:
            inner = ct.strip(member.type)
            if inner.kind in ct.AGGREGATE_KINDS:
                yield from _member_rows(obj, inner, base + member.bit_offset)
                continue

        value = ct.safe(lambda m=member: obj.member_(m.name), None)
        note = ""
        if member.bit_field_size:
            note = f":{member.bit_field_size}"

        if value is None:
            yield Row(member.name, None, ct.type_name(member.type), "<fault>", False, note)
            continue

        yield Row(
            name=member.name,
            obj=value,
            type_name=ct.type_name(member.type),
            value=ct.render_value(value),
            followable=can_follow(value),
            note=note,
            offset=ct.safe(lambda m=member: (base + m.bit_offset) // 8, None),
            size=ct.safe(lambda m=member: drgn.sizeof(m.type), None),
        )


def _array_rows(obj: Object, type_: drgn.Type) -> Iterator[Row]:
    length = type_.length or 0
    shown = min(length, MAX_ROWS)
    element_type = ct.type_name(type_.type)
    for index in range(shown):
        value = ct.safe(lambda i=index: obj[i], None)
        if value is None:
            yield Row(f"[{index}]", None, element_type, "<fault>", False)
            continue
        yield Row(
            name=f"[{index}]",
            obj=value,
            type_name=element_type,
            value=ct.render_value(value),
            followable=can_follow(value),
        )
    if length > shown:
        yield Row(
            f"… {length - shown} more", None, element_type, "", False, kind="truncated"
        )


def rows_for(obj: Object) -> list[Row]:
    """Expand ``obj`` one level into displayable rows."""
    type_ = ct.strip(obj.type_)

    # A pointer to an aggregate is shown as its target: nobody wants an
    # intermediate screen containing a single "*" row.
    if type_.kind == TypeKind.POINTER:
        target = follow(obj)
        if target is None:
            return []
        obj, type_ = target, ct.strip(target.type_)

    if type_.kind in ct.AGGREGATE_KINDS:
        return list(_member_rows(obj, type_))
    if type_.kind == TypeKind.ARRAY:
        return list(_array_rows(obj, type_))
    return []


def can_follow(obj: Object) -> bool:
    """Can this value be navigated into?"""
    type_ = ct.strip(obj.type_)

    if type_.kind == TypeKind.POINTER:
        target = ct.strip(type_.type)
        if target.kind in (TypeKind.VOID, TypeKind.FUNCTION):
            return False
        # A null or unreadable pointer is a dead end, not a destination.
        address = ct.safe(obj.value_, 0)
        return bool(address)
    if type_.kind in ct.AGGREGATE_KINDS:
        return bool(type_.members)
    if type_.kind == TypeKind.ARRAY:
        return bool(type_.length)
    return False


def follow(obj: Object) -> Object | None:
    """Dereference one level, or return the object itself if it's already there."""
    type_ = ct.strip(obj.type_)
    if type_.kind != TypeKind.POINTER:
        return obj if type_.kind in ct.AGGREGATE_KINDS or type_.kind == TypeKind.ARRAY else None
    if not ct.safe(obj.value_, 0):
        return None
    return ct.safe(lambda: obj[0], None)

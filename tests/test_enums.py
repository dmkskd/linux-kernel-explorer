"""Check the hand-written tables against the kernel that is running.

Some of what the catalog prints is not read from the kernel but typed out
here: the name for a clock event state, the bits of wq_flags, which timer
wheel base is which. Every one of those is a claim about the kernel, written
from memory, and a wrong one is invisible -- it produces a plausible word next
to a real number.

Where the kernel keeps the same knowledge as an enum in its debug info, this
compares the two and fails on a difference. That turns "this table is right"
from something to be trusted into something the suite proves on whatever
kernel it runs against. A table with no enum behind it (the CLOCK_* ids are
uapi constants, not DWARF) is listed at the end as still unchecked, so the gap
is visible rather than forgotten.
"""

from __future__ import annotations

import sys
import time

import drgn

from kexplore.catalog.decoders import (
    CLOCK_EVENT_STATES,
    CLOCK_NAMES,
    TICK_MODES,
    WHEEL_BASES,
    WQ_FLAG_NAMES,
)
from kexplore.catalog.links import POOL_BH

ok = True


def check(condition: bool, message: str) -> None:
    global ok
    ok &= bool(condition)
    print(("  ok   " if condition else "  FAIL ") + message)


def enumerators(prog, name: str) -> dict[str, int] | None:
    try:
        return {e.name: e.value for e in prog.type(f"enum {name}").enumerators}
    except LookupError:
        return None


def check_value_table(prog, enum_name: str, prefix: str, table: dict[int, str]) -> None:
    """A {value: name} table against the kernel's enum of the same thing."""
    values = enumerators(prog, enum_name)
    if values is None:
        check(True, f"enum {enum_name} absent from this kernel's debug info, table unchecked")
        return
    expected = {
        value: name[len(prefix):].lower().replace("_", " ")
        for name, value in values.items()
        if name.startswith(prefix)
    }
    check(table == expected, f"{enum_name}: {table} == {expected}")


def main() -> int:
    prog = drgn.program_from_kernel()

    check_value_table(prog, "clock_event_state", "CLOCK_EVT_STATE_", CLOCK_EVENT_STATES)
    check_value_table(prog, "tick_device_mode", "TICKDEV_MODE_", TICK_MODES)

    # wq_flags: every bit the catalog spells out must exist in the enum with
    # that value. The enum has more (the __WQ_ internals), which the catalog
    # deliberately leaves out.
    flags = enumerators(prog, "wq_flags") or {}
    mismatched = [
        (bit, name) for bit, name in WQ_FLAG_NAMES
        if flags.get(f"WQ_{name.upper()}", flags.get(f"__WQ_{name.upper()}")) != bit
    ]
    check(not mismatched and bool(flags),
          f"wq_flags bits match the kernel enum (mismatched: {mismatched})")

    pool_flags = enumerators(prog, "worker_pool_flags") or {}
    check(pool_flags.get("POOL_BH") == POOL_BH,
          f"POOL_BH is {POOL_BH:#x} here and {pool_flags.get('POOL_BH')} in the kernel")

    # The wheel base names are keyed on how many bases this kernel has, so the
    # table must have an entry for that count.
    bases = prog["timer_bases"].type_.length
    check(bases in WHEEL_BASES,
          f"this kernel has {bases} timer wheel bases, and the table names that many")

    # hrtimer clock bases: the catalog calls the second half of the array the
    # soft ones, rather than hard-coding where the split is.
    hrtimer_bases = enumerators(prog, "hrtimer_base_type") or {}
    total = hrtimer_bases.get("HRTIMER_MAX_CLOCK_BASES")
    soft = [name for name, value in hrtimer_bases.items()
            if name.endswith("_SOFT") and value >= (total or 0) // 2]
    check(total is not None and len(soft) == total // 2,
          f"the soft hrtimer bases are the upper half of {total}: {sorted(soft)}")

    # CLOCK_* ids are uapi constants with no enum in the debug info. What can
    # be checked is that each id this table names is one clock_gettime accepts,
    # which is what the "expires in" row relies on.
    unreadable = []
    for clockid, name in CLOCK_NAMES.items():
        try:
            time.clock_gettime(clockid)
        except OSError:
            unreadable.append(name)
    check(not unreadable,
          f"every clock this table names can be read from userspace "
          f"(unreadable: {unreadable})")

    print("\nPASS" if ok else "\nFAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

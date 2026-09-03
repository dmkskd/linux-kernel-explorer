"""bpftrace output parsing, with no tracer involved.

``core/probe.py`` turns whatever bpftrace printed into sections of rows. The
parsing is pure text work, so it runs anywhere -- which matters, because the
alternative is only ever exercising it against whatever a two-second trace on
one idle VM happened to emit.

The samples below are real bpftrace output: a histogram, keyed counts,
a scalar, and the two together in one run.
"""

from __future__ import annotations

import sys

from kexplore.core.probe import parse_bpftrace

ok = True


def check(condition: bool, message: str) -> None:
    global ok
    ok &= bool(condition)
    print(("  ok   " if condition else "  FAIL ") + message)


HISTOGRAM = """
Attaching 4 probes...

@us_wakeup_to_oncpu_cpu_was_idle:
[1]                    2 |@@                                                  |
[2, 4)                97 |@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@|
[4, 8)                31 |@@@@@@@@@@@@@@@@                                    |
"""

COUNTS = """
@switches_per_cpu[0]: 93
@switches_per_cpu[3]: 512
@switches_per_cpu[1]: 240
"""

SCALAR = """
@count_cow_write_faults_after: 1841
"""

MIXED = HISTOGRAM + COUNTS + SCALAR


def main() -> int:
    # --- a histogram ----------------------------------------------------
    sections = parse_bpftrace(HISTOGRAM)
    check(len(sections) == 1, f"one section ({len(sections)})")
    check(
        sections[0].name == "us_wakeup_to_oncpu_cpu_was_idle",
        f"the map name is the section name: {sections[0].name}",
    )
    check(len(sections[0].rows) == 3, f"three buckets ({len(sections[0].rows)})")
    label, count, bar = sections[0].rows[1]
    check(label == "[2, 4)" and count == "97", f"bucket parsed: {label} {count}")
    check(bar.startswith("@") and not bar.endswith(" "), "the bar is kept, trimmed")
    check(
        "Attaching 4 probes..." not in [r[0] for r in sections[0].rows],
        "bpftrace's preamble is not a row",
    )

    # --- keyed counts ---------------------------------------------------
    sections = parse_bpftrace(COUNTS)
    check(len(sections) == 1, "keys of one map group into one section")
    check(sections[0].name == "switches_per_cpu", "grouped under the map name")
    check(
        [r[0] for r in sections[0].rows] == ["3", "1", "0"],
        f"counts sort by value, descending: {[r[1] for r in sections[0].rows]}",
    )

    # --- a scalar -------------------------------------------------------
    sections = parse_bpftrace(SCALAR)
    check(len(sections) == 1 and len(sections[0].rows) == 1, "a scalar is one row")
    check(
        sections[0].rows[0][:2] == ("count_cow_write_faults_after", "1841"),
        f"an unkeyed entry labels itself: {sections[0].rows[0][:2]}",
    )

    # --- several maps in one run ----------------------------------------
    sections = parse_bpftrace(MIXED)
    names = [s.name for s in sections]
    check(
        names == ["us_wakeup_to_oncpu_cpu_was_idle", "switches_per_cpu",
                  "count_cow_write_faults_after"],
        f"every map gets its own section, in order: {names}",
    )
    histogram = sections[0]
    check(
        all(row[2] for row in histogram.rows),
        "histogram rows keep their bars when other maps follow",
    )
    check(
        [r[0] for r in sections[1].rows] == ["3", "1", "0"],
        "counts still sort when they follow a histogram",
    )

    # --- nothing at all -------------------------------------------------
    check(parse_bpftrace("") == [], "empty output parses to no sections")
    check(
        parse_bpftrace("Attaching 2 probes...\n") == [],
        "a run that recorded nothing produces no sections",
    )

    print("\nPASS" if ok else "\nFAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

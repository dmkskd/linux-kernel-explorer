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

from kexplore.catalog.procfs import SERVED_BY, fields_from, served_by
from kexplore.catalog.userspace import UNFILLED, fill, runnable
from kexplore.core.probe import parse_bpftrace, parse_stacks

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



# A stack map, as bpftrace prints one: the key spans lines and the count sits
# on the closing bracket. Recorded from kprobe:do_task_stat during one ps -e.
STACKS = """
Attaching 2 probes...
@opens[/proc/1]: 1
@serves[
        do_task_stat+0
        proc_single_show+100
        seq_read_iter+292
        vfs_read+204
]: 153
"""


def test_stacks() -> None:
    stacks = parse_stacks(STACKS)
    check(list(stacks) == ["serves"], "only the stack map is a stack")
    frames, count = stacks["serves"][0]
    check(count == 153, f"the count sits on the closing bracket: {count}")
    check(len(frames) == 4, f"{len(frames)} frames, leaf first")
    check(frames[0] == "do_task_stat+0", "the leaf keeps its +offset")
    check(parse_stacks("@opens[/proc/1]: 1\n") == {}, "a one-line map is not a stack")
    sections = parse_bpftrace(STACKS)
    names = [s.name for s in sections]
    check("opens" in names, f"the row parser still reads the flat map: {names}")


def test_served_by() -> None:
    cases = {
        "awk '{print $7}' /proc/1/stat": "do_task_stat",
        "grep VmPTE /proc/1/status": "proc_pid_status",
        "cat /sys/class/net/eth0/mtu": "sysfs_kf_seq_show",
        "grep ^ctxt /proc/stat": "show_stat",
        "ps -e": "do_task_stat",
        "ss -tanH | awk '{print $2}'": None,
        "": None,
    }
    for command, expected in cases.items():
        found = served_by(command)
        got = found.function if found else None
        check(got == expected, f"{command[:34]!r} -> {got}")
    check(
        all(entry.path == path for path, entry in SERVED_BY.items()),
        "every entry agrees with the key it is filed under",
    )


def test_fill() -> None:
    # One substitution step for every command in the tables, so a struct either
    # fills a placeholder everywhere it appears or nowhere. A command that
    # cannot be completed is not shown as one: if no task owns the object, no
    # /proc directory publishes it and there is nothing to run.
    command = "stat -L /proc/<pid>/fd/<n>"
    check(
        fill(command, {"<pid>": "1", "<n>": "3"}) == "stat -L /proc/1/fd/3",
        "every placeholder a struct knows is filled",
    )
    check(
        fill(command, {"<pid>": "1"}) == UNFILLED["<n>"],
        f"a half-filled command says why instead: {fill(command, {'<pid>': '1'})}",
    )
    check(
        fill(command, {}) == UNFILLED["<pid>"],
        "and so does one with nothing known",
    )
    check(
        fill("ps -e", {}) == "ps -e",
        "a command with no placeholders is untouched",
    )


def test_runnable() -> None:
    cases = {
        "ls /proc/1/task, or ps -L -p 1": "ls /proc/1/task",
        "ps -o ni= -p 1  # nice": "ps -o ni= -p 1",
        "slabtop, or cat /proc/slabinfo": "slabtop",
        "ss -tanmH | grep -o 'skmem:([^)]*)'": "ss -tanmH | grep -o 'skmem:([^)]*)'",
    }
    for shown, expected in cases.items():
        check(runnable(shown) == expected, f"{shown[:38]!r} runs as {runnable(shown)!r}")


def test_proc_fields() -> None:
    stat = [name for name, _command in fields_from("/proc/<pid>/stat")]
    check(len(stat) == 7, f"{len(stat)} fields come out of /proc/<pid>/stat")
    check(
        all("status" not in command for _n, command in fields_from("/proc/<pid>/stat")),
        "stat does not claim the fields that status publishes",
    )
    check(
        fields_from("/proc/<pid>/status") != fields_from("/proc/<pid>/stat"),
        "the prefix does not swallow the longer path",
    )


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

    # --- stacks and the /proc field map ---------------------------------
    test_stacks()
    test_served_by()
    test_fill()
    test_runnable()
    test_proc_fields()

    print("\nPASS" if ok else "\nFAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

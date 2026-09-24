"""Check the /proc annotation table: its paths, and what it claims.

Two halves. The table itself is static data, so the parsing and the coverage
of a captured file are checked anywhere, with no kernel. The paths are checked
against the running kernel's types when drgn can attach, because a member that
was renamed upstream is exactly the failure this table has.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kexplore.catalog import pseudofs

ok = True


def check(condition: bool, message: str) -> None:
    global ok
    ok &= bool(condition)
    print(("  ok   " if condition else "  FAIL ") + message)


# One kernel's output, kept verbatim. Not to assert any of these numbers, but
# because a file with every optional line in it (the arch lines, hugetlb,
# seccomp) is what shows whether the table covers a real kernel's output.
CAPTURED = """\
Name:   python3
Umask:  0022
State:  S (sleeping)
Tgid:   4287
Ngid:   0
Pid:    4287
PPid:   4286
TracerPid:      0
Uid:    0       0       0       0
Gid:    0       0       0       0
FDSize: 256
Groups: 0
NStgid: 4287
NSpid:  4287
NSpgid: 4287
NSsid:  4286
Kthread:        0
VmPeak:  1417212 kB
VmSize:  1417212 kB
VmLck:         0 kB
VmPin:         0 kB
VmHWM:    740232 kB
VmRSS:    740232 kB
RssAnon:          319164 kB
RssFile:          421068 kB
RssShmem:              0 kB
VmData:   973472 kB
VmStk:       132 kB
VmExe:         4 kB
VmLib:     29476 kB
VmPTE:      1588 kB
VmSwap:        0 kB
HugetlbPages:          0 kB
CoreDumping:    0
THP_enabled:    1
untag_mask:     0xffffffffffffff
Threads:        8
SigQ:   2/31615
SigPnd: 0000000000000000
ShdPnd: 0000000000000000
SigBlk: 0000000000000000
SigIgn: 0000000001001000
SigCgt: 00000001080a0002
CapInh: 0000000000000000
CapPrm: 000001ffffffffff
CapEff: 000001ffffffffff
CapBnd: 000001ffffffffff
CapAmb: 0000000000000000
NoNewPrivs:     0
Seccomp:        0
Seccomp_filters:        0
Speculation_Store_Bypass:       vulnerable
SpeculationIndirectBranch:      unknown
Cpus_allowed:   f
Cpus_allowed_list:      0-3
Mems_allowed:   00000000,00000000,00000000,00000000,00000000,00000000,00000000,00000000,00000000,00000000,00000000,00000000,00000000,00000000,00000000,00000001
Mems_allowed_list:      0
voluntary_ctxt_switches:        41819
nonvoluntary_ctxt_switches:     1594
"""


def main() -> int:
    spec = pseudofs.STATUS

    labels = [origin.label for origin in spec.origins]
    check(len(labels) == len(set(labels)), f"{len(labels)} origins, no duplicate label")
    check(all(origin.doc for origin in spec.origins),
          "every origin says what its line means")

    lines = pseudofs.parse(spec, CAPTURED)
    check(len(lines) == 59, f"the captured file parses to {len(lines)} lines")
    missing = pseudofs.unclaimed(spec, CAPTURED)
    check(not missing, f"every captured line has an origin{': ' + ', '.join(missing) if missing else ''}")

    unseen = [label for label in labels if label not in {line.label for line in lines}]
    check(not unseen, f"no origin for a line that kernel never printed{': ' + ', '.join(unseen) if unseen else ''}")

    check(spec.origin_for("FDSize").path() == "task->files->fdt->max_fds",
          "a two-hop path renders as C")
    check(spec.origin_for("Name").path() == "task->comm",
          "a path with no hop renders as C")
    check(spec.origin_for("SpeculationIndirectBranch").path()
          == "arch_prctl_spec_ctrl_get(task)",
          "a line with no member names the function that computes it")

    try:
        import drgn
        prog = drgn.program_from_kernel()
    except Exception as exc:  # noqa: BLE001 - no kernel here is not a failure
        print(f"  skip  kernel checks ({type(exc).__name__})")
        return 0 if ok else 1

    resolved, detail = pseudofs.check(prog, spec)
    check(resolved, f"paths against this kernel: {detail}")

    live = pseudofs.read(spec, 1)
    check(bool(live), f"/proc/1/status read: {len(live)} lines")
    strangers = [line.label for line in live if line.origin is None]
    check(not strangers,
          f"this kernel prints nothing the table misses{': ' + ', '.join(strangers) if strangers else ''}")

    steps = pseudofs.hop_types(prog, spec, spec.origin_for("VmRSS"))
    check(steps[0][1] == "struct task_struct" and len(steps) == 3,
          f"VmRSS expands to {' '.join(name for name, _ in steps)}")

    check_view(prog)
    return 0 if ok else 1


def check_view(prog) -> None:
    """The rows the explorer builds for the file, without opening a terminal.

    Goes through frames rather than the app: the dispatch in ``plan_for`` and
    the row contents are what this experiment adds, and both are decided
    before any widget exists.
    """
    from drgn.helpers.linux.pid import find_task

    from kexplore.core.source import KernelSource
    from kexplore.view import frames

    ctx = frames.Context(prog, KernelSource(), live=True)
    task = find_task(prog, 1)

    frame = frames.object_frame("init", task, ctx)
    frame.load()
    opener = next((row for row in frame.rows if row.name == "/proc/1"), None)
    check(opener is not None, "a task_struct offers its /proc directory")
    if opener is None:
        return

    plan = frames.plan_for(opener.item, ctx)
    listing = plan.build()
    listing.load()
    names = [row.name for row in listing.rows]
    check("status" in names and "stat" in names and "maps" in names,
          f"the directory lists the real files: {len(names)} entries")
    status_row = next(row for row in listing.rows if row.name == "status")

    plan = frames.plan_for(status_row.item, ctx)
    status = plan.build()
    status.load()
    check(plan.columns == ("line", "value", "read from"),
          "the file view names its own columns")
    by_label = {row.name: row for row in status.rows}
    check(len(status.rows) > 40, f"{len(status.rows)} rows, one per line of the file")
    tail = status.rows[-1]
    check(tail.cells[0] == "cat /proc/1/status", "the file itself is the last row")
    check(len(tail.children()) == len(status.rows) - 1,
          f"opening it shows {len(tail.children())} raw lines")
    check(by_label["FDSize"].cells[2] == "task->files->fdt->max_fds",
          f"FDSize is read from {by_label['FDSize'].cells[2]}")
    check(by_label["Name"].cells[1] == "systemd",
          f"the value is the file's own: Name is {by_label['Name'].cells[1]}")
    root = frames.plan_for(pseudofs.ROOT, ctx).build()
    root.load()
    names = [row.name for row in root.rows]
    check("meminfo" in names and "1" in names,
          f"/proc lists {len(names)} entries, the system and the tasks")
    check(names.index("meminfo") < names.index("1"),
          "the system files come before the task directories")

    raw = frames.plan_for(pseudofs.FileItem("/proc/1/cmdline"), ctx).build()
    raw.load()
    check(raw.rows[0].cells[1].startswith("/"),
          f"a file with no table shows its text: {raw.rows[0].cells[1][:40]}")

    blocked = frames.plan_for(pseudofs.FileItem("/proc/kmsg"), ctx).build()
    blocked.load()
    check(blocked.rows[0].kind == "error",
          f"a file that must not be read says why: {blocked.rows[0].cells[1]}")

    children = by_label["VmRSS"].children()
    shown = [child.cells[0] for child in children]
    check([cell.lstrip(" \u251c\u2514") for cell in shown] ==
          ["task_struct", "->mm", ".rss_stat", "printed by"],
          "the path expands to the hops it followed")
    check(all(cell.startswith(" ") for cell in shown),
          "an expanded hop is indented, so it does not read as a file line")

    from kexplore.view import procfile

    target = procfile.jump_target(ctx, by_label["VmRSS"])
    check(target is not None and target[0] == "mm"
          and "mm_struct" in target[1].type_.type_name(),
          f"VmRSS jumps to {target[1].type_.type_name() if target else 'nothing'}")
    task_target = procfile.jump_target(ctx, by_label["Name"])
    check(task_target is not None and task_target[0] == "task_struct",
          "a line read from the task itself jumps to the task")
    schedstat = frames.plan_for(pseudofs.FileItem("/proc/1/schedstat"), ctx).build()
    schedstat.load()
    se = procfile.jump_target(ctx, schedstat.rows[0])
    check(se is not None and se[0] == "se",
          f"a schedstat line jumps to {se[0] if se else 'nothing'}")
    printed = schedstat.rows[0].children()[-1]
    check(printed.cells[2] == "fs/proc/base.c",
          f"the printing function is located in {printed.cells[2]}")

    io_frame = frames.plan_for(pseudofs.FileItem("/proc/1/io"), ctx).build()
    io_frame.load()
    embedded = procfile.jump_target(ctx, io_frame.rows[0])
    check(embedded is not None and embedded[0] == "ioac",
          f"an embedded-struct line jumps to {embedded[0] if embedded else 'nothing'}")

    check(procfile.jump_target(ctx, raw.rows[0]) is None,
          "a raw text line has no structure to jump to")

    check(pseudofs.annotates("/proc/1/status")
          and not pseudofs.annotates("/proc/1/environ"),
          "a file says whether a line table is written for it")
    check(pseudofs.holds_annotated("/proc/1")
          and not pseudofs.holds_annotated("/proc/1/fd"),
          "a directory says whether it holds one")

    resolved, detail = pseudofs.check(prog, pseudofs.STAT)
    check(resolved, f"stat paths against this kernel: {detail}")

    stat = frames.plan_for(pseudofs.FileItem("/proc/1/stat"), ctx).build()
    stat.load()
    fields = [row for row in stat.rows if not row.name.startswith("cat ")]
    check(len(fields) == 52, f"stat parses to {len(fields)} fields")
    check(stat.rows[1].cells[1] == "systemd",
          f"field 2 is the command: {stat.rows[1].cells[1]}")
    check(stat.rows[22].cells[2] == "task->mm->total_vm",
          f"field 23 is read from {stat.rows[22].cells[2]}")

    resolved, detail = pseudofs.check(prog, pseudofs.MAPS)
    check(resolved, f"maps paths against this kernel: {detail}")

    maps = frames.plan_for(pseudofs.FileItem("/proc/1/maps"), ctx).build()
    maps.load()
    check(len(maps.rows) > 5 and maps.rows[0].cells[2] == "task->mm->mm_mt",
          f"maps is {len(maps.rows)} lines, read from {maps.rows[0].cells[2]}")
    columns = [child.cells[0].lstrip(" \u251c\u2514") for child in maps.rows[0].children()]
    check(columns == ["task_struct", "->mm", "walks mm_mt", "address range",
                      "permissions", "offset", "device", "inode", "path"],
          f"a maps line expands from the task to its columns: {columns}")

    for spec in (pseudofs.SCHED, pseudofs.SCHEDSTAT, pseudofs.STATM,
                 pseudofs.LIMITS, pseudofs.IO, pseudofs.LOADAVG,
                 pseudofs.UPTIME, pseudofs.MEMINFO, pseudofs.COMM,
                 pseudofs.WCHAN, pseudofs.OOM_SCORE, pseudofs.OOM_SCORE_ADJ,
                 pseudofs.PERSONALITY, pseudofs.COREDUMP_FILTER,
                 pseudofs.TIMERSLACK_NS):
        resolved, detail = pseudofs.check(prog, spec)
        check(resolved, f"{spec.path.rsplit('/', 1)[1]} paths: {detail}")

    sched = frames.plan_for(pseudofs.FileItem("/proc/1/sched"), ctx).build()
    sched.load()
    claimed = [row for row in sched.rows if isinstance(row.item, procfile.Jump)]
    check(len(claimed) > 20,
          f"sched: {len(claimed)} of {len(sched.rows)} lines have an origin")
    unclaimed = [row.cells[0] for row in sched.rows
                 if not isinstance(row.item, procfile.Jump)
                 and not row.name.startswith("cat ")]
    check(len(unclaimed) <= 2,
          f"sched: unclaimed lines are {unclaimed}")

    for name, expected in (("statm", 7), ("io", 7)):
        frame = frames.plan_for(pseudofs.FileItem(f"/proc/1/{name}"), ctx).build()
        frame.load()
        claimed = [row for row in frame.rows if isinstance(row.item, procfile.Jump)]
        check(len(claimed) == expected,
              f"{name}: {len(claimed)} of {len(frame.rows)} lines have an origin")

    kinds = {row.cells[0]: row.kind for row in listing.rows}
    check(kinds.get("fd/") == "link", "a directory is named with a slash and opens")
    check(kinds.get("status") == "derived", "an annotated file is coloured apart")
    check(kinds.get("environ") == "field",
          "a file with no table keeps the plain colour")

    up = procfile.parent_plan(ctx, "/proc/1/schedstat")
    check(up is not None and up.label == "/proc/1",
          "back from a file goes to its directory")
    check(procfile.parent_plan(ctx, "/proc") is None,
          "back stops at /proc")

    meminfo = frames.plan_for(pseudofs.FileItem("/proc/meminfo"), ctx).build()
    meminfo.load()
    lines = [row for row in meminfo.rows if not row.name.startswith("cat ")]
    unnamed = [row.cells[0] for row in lines
               if not isinstance(row.item, procfile.Jump)]
    check(not unnamed, f"meminfo lines with no origin: {unnamed}")
    blank = [row.cells[0] for row in lines if not row.cells[2]]
    check(blank == ["NFS_Unstable", "Bounce", "WritebackTmp", "VmallocTotal",
                    "VmallocChunk"],
          f"only constants and dead fields have an empty column: {blank}")
    free = next(row for row in meminfo.rows if row.name == "MemFree")
    check(free.cells[2] == "vm_zone_stat[NR_FREE_PAGES]",
          f"MemFree is read from {free.cells[2]}")
    target = procfile.jump_target(ctx, free)
    check(target is not None and target[0] == "vm_zone_stat",
          "a system-wide line jumps to the global it is read from")

    vma = procfile.jump_target(ctx, maps.rows[0])
    check(vma is not None and "vm_area_struct" in vma[1].type_.type_name(),
          f"a maps line jumps to {vma[1].type_.type_name() if vma else 'nothing'}")


if __name__ == "__main__":
    raise SystemExit(main())

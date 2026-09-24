"""The tables: every annotated pseudo-file, line by line.

Data only. The model these are written in, the reading and the checking are
in ``pseudofs.py``; this module is the part that grows as files are added.

Descriptions are paraphrased from the manual page each table names in
``source``. Where there is no manual page, they are written from the kernel
function named in ``function``.
"""

from __future__ import annotations

from .pseudofs import Origin, PseudoFile


# ------------------------------------------------------------------- status

# Order follows fs/proc/array.c, which is the order the file prints: the task
# itself, then everything reached through one pointer out of it.
STATUS = PseudoFile(
    path="/proc/<pid>/status",
    root="task_struct",
    function="proc_pid_status",
    defined_in="fs/proc/array.c",
    source="proc_pid_status(5)",
    doc="The Vm* block is read from mm_struct, so a kernel thread prints "
        "none of it.",
    origins=(
        Origin("Name", (), "comm",
               doc="command run by this process, truncated at 16 bytes "
                   "including the null"),
        Origin("Umask", ("fs",), "umask",
               doc="process umask, octal"),
        Origin("State", (), "__state", alternates=("state",),
               doc="current state of the process"),
        Origin("Tgid", (), "tgid",
               doc="thread group id, which is the process id"),
        Origin("Ngid", (), "", optional=True, helper="task_numa_group_id",
               doc="NUMA group id, 0 if none"),
        Origin("Pid", (), "pid",
               doc="thread id"),
        Origin("PPid", ("real_parent",), "pid",
               doc="pid of the parent process"),
        Origin("TracerPid", ("parent",), "pid",
               doc="pid of the process tracing this one, 0 if none"),
        Origin("Uid", ("cred",), "uid",
               doc="real, effective, saved set and filesystem uids"),
        Origin("Gid", ("cred",), "gid",
               doc="real, effective, saved set and filesystem gids"),
        Origin("FDSize", ("files", "fdt"), "max_fds",
               doc="file descriptor slots currently allocated, not "
                   "descriptors open"),
        Origin("Groups", ("cred", "group_info"), "gid",
               doc="supplementary group list"),
        Origin("NStgid", ("thread_pid",), "numbers",
               doc="thread group id in each pid namespace this task is "
                   "in, outermost first"),
        Origin("NSpid", ("thread_pid",), "numbers",
               doc="thread id in each of those namespaces"),
        Origin("NSpgid", ("signal",), "pids",
               doc="process group id in each of those namespaces"),
        Origin("NSsid", ("signal",), "pids",
               doc="session id in each of those namespaces"),
        Origin("Kthread", (), "flags",
               doc="PF_KTHREAD, one bit of the flags word"),
        Origin("VmPeak", ("mm",), "hiwater_vm",
               doc="peak virtual memory size"),
        Origin("VmSize", ("mm",), "total_vm",
               doc="virtual memory size"),
        Origin("VmLck", ("mm",), "locked_vm",
               doc="locked memory size, from mlock(2)"),
        Origin("VmPin", ("mm",), "pinned_vm",
               doc="pinned pages, which cannot be moved because "
                   "something accesses the physical memory directly"),
        Origin("VmHWM", ("mm",), "hiwater_rss",
               doc="peak resident set size; read from percpu counters, "
                   "so approximate"),
        Origin("VmRSS", ("mm",), "rss_stat",
               doc="resident set size, the sum of RssAnon, RssFile and "
                   "RssShmem; approximate for the same reason"),
        Origin("RssAnon", ("mm",), "rss_stat",
               doc="resident anonymous memory"),
        Origin("RssFile", ("mm",), "rss_stat",
               doc="resident file mappings"),
        Origin("RssShmem", ("mm",), "rss_stat",
               doc="resident shared memory: System V, tmpfs and shared "
                   "anonymous mappings"),
        Origin("VmData", ("mm",), "data_vm",
               doc="size of the data segment"),
        Origin("VmStk", ("mm",), "stack_vm",
               doc="size of the stack segment"),
        Origin("VmExe", ("mm",), "exec_vm",
               doc="size of the text segment"),
        Origin("VmLib", ("mm",), "exec_vm",
               doc="shared library code size, the executable total minus "
                   "VmExe"),
        Origin("VmPTE", ("mm",), "pgtables_bytes",
               doc="page table entries size"),
        Origin("VmSwap", ("mm",), "rss_stat",
               doc="anonymous private pages swapped out; shmem swap is "
                   "not counted"),
        Origin("HugetlbPages", ("mm",), "hugetlb_usage", optional=True,
               doc="hugetlb memory portions"),
        Origin("CoreDumping", ("signal",), "core_state",
               doc="1 while the process is dumping core, which is what "
                   "stops a monitor killing it mid-dump"),
        Origin("THP_enabled", ("mm",), "flags",
               doc="0 when transparent huge pages were disabled for this "
                   "process by prctl(2)"),
        Origin("untag_mask", ("mm",), "", optional=True, helper="mm_untag_mask",
               doc="which top bits of a pointer the CPU ignores; where "
                   "it is kept is per architecture"),
        Origin("Threads", ("signal",), "nr_threads",
               doc="number of threads in the process"),
        Origin("SigQ", ("signal",), "rlim",
               doc="signals queued for this real user id, and the "
                   "RLIMIT_SIGPENDING limit"),
        Origin("SigPnd", (), "pending",
               doc="signals pending for this thread, in hexadecimal"),
        Origin("ShdPnd", ("signal",), "shared_pending",
               doc="signals pending for the process as a whole"),
        Origin("SigBlk", (), "blocked",
               doc="signals blocked"),
        Origin("SigIgn", ("sighand",), "action",
               doc="signals ignored, built by walking the 64 k_sigaction "
                   "entries"),
        Origin("SigCgt", ("sighand",), "action",
               doc="signals caught, from the same walk"),
        Origin("CapInh", ("cred",), "cap_inheritable",
               doc="capabilities in the inheritable set"),
        Origin("CapPrm", ("cred",), "cap_permitted",
               doc="capabilities in the permitted set"),
        Origin("CapEff", ("cred",), "cap_effective",
               doc="capabilities in the effective set"),
        Origin("CapBnd", ("cred",), "cap_bset",
               doc="the capability bounding set"),
        Origin("CapAmb", ("cred",), "cap_ambient",
               doc="the ambient capability set"),
        Origin("NoNewPrivs", (), "atomic_flags",
               doc="the no_new_privs bit, from prctl(2): "
                   "PFA_NO_NEW_PRIVS in atomic_flags"),
        Origin("Seccomp", ("seccomp",), "mode",
               doc="seccomp mode: 0 disabled, 1 strict, 2 filter"),
        Origin("Seccomp_filters", ("seccomp",), "filter_count", optional=True,
               doc="number of seccomp filters attached to the process"),
        Origin("Speculation_Store_Bypass", (), "", optional=True, helper="arch_prctl_spec_ctrl_get",
               doc="speculation flaw mitigation state, from prctl(2)"),
        Origin("SpeculationIndirectBranch", (), "", optional=True, helper="arch_prctl_spec_ctrl_get",
               doc="the same, for indirect branch speculation"),
        Origin("Cpus_allowed", (), "cpus_mask",
               doc="mask of CPUs this process may run on"),
        Origin("Cpus_allowed_list", (), "cpus_mask",
               doc="the same mask in list format"),
        Origin("Mems_allowed", (), "mems_allowed",
               doc="mask of memory nodes allowed to this process"),
        Origin("Mems_allowed_list", (), "mems_allowed",
               doc="the same mask in list format"),
        Origin("voluntary_ctxt_switches", (), "nvcsw",
               doc="voluntary context switches"),
        Origin("nonvoluntary_ctxt_switches", (), "nivcsw",
               doc="involuntary context switches"),
    ),
)


# ---------------------------------------------------------------------- stat

# One line, fields separated by spaces, named by procfs(5) and numbered from
# one. The order is do_task_stat()'s and nothing else: a field is where it is
# because that is where it has always been printed.
STAT = PseudoFile(
    path="/proc/<pid>/stat",
    root="task_struct",
    function="do_task_stat",
    defined_in="fs/proc/array.c",
    source="proc_pid_stat(5)",
    doc="",
    positional=True,
    comm_field=True,
    origins=(
        Origin("pid", (), "pid",
               doc="the process id"),
        Origin("comm", (), "comm",
               doc="the filename of the executable, in parentheses, "
                   "truncated at 16 bytes"),
        Origin("state", (), "__state", alternates=("state",),
               doc="one character: R running, S interruptible sleep, D "
                   "uninterruptible sleep, Z zombie, T stopped, t "
                   "tracing stop, X dead, I idle"),
        Origin("ppid", ("real_parent",), "pid",
               doc="the pid of the parent"),
        Origin("pgrp", ("signal",), "pids",
               doc="the process group id"),
        Origin("session", ("signal",), "pids",
               doc="the session id"),
        Origin("tty_nr", ("signal",), "tty",
               doc="the controlling terminal, as an encoded device "
                   "number"),
        Origin("tpgid", ("signal",), "tty",
               doc="the foreground process group of that terminal"),
        Origin("flags", (), "flags",
               doc="the kernel flags word, the PF_* defines in "
                   "include/linux/sched.h"),
        Origin("minflt", (), "min_flt",
               doc="minor faults, which needed no page loaded from disk"),
        Origin("cminflt", ("signal",), "cmin_flt",
               doc="minor faults of waited-for children"),
        Origin("majflt", (), "maj_flt",
               doc="major faults, which needed a page loaded from disk"),
        Origin("cmajflt", ("signal",), "cmaj_flt",
               doc="major faults of waited-for children"),
        Origin("utime", (), "utime",
               doc="time scheduled in user mode, in clock ticks; "
                   "includes guest_time"),
        Origin("stime", (), "stime",
               doc="time scheduled in kernel mode, in clock ticks"),
        Origin("cutime", ("signal",), "cutime",
               doc="user time of waited-for children"),
        Origin("cstime", ("signal",), "cstime",
               doc="kernel time of waited-for children"),
        Origin("priority", (), "prio",
               doc="for a real-time policy the negated priority minus "
                   "one; otherwise the raw nice value, 0 high to 39 low"),
        Origin("nice", (), "static_prio",
               doc="the nice value, 19 low priority to -20 high"),
        Origin("num_threads", ("signal",), "nr_threads",
               doc="threads in this process"),
        Origin("itrealvalue", (), "",
               doc="no longer maintained, always 0"),
        Origin("starttime", (), "start_boottime",
               doc="when the process started after boot, in clock ticks"),
        Origin("vsize", ("mm",), "total_vm",
               doc="virtual memory size in bytes"),
        Origin("rss", ("mm",), "rss_stat",
               doc="pages in real memory; excludes pages that were never "
                   "demand-loaded, and pages swapped out"),
        Origin("rsslim", ("signal",), "rlim",
               doc="the current soft limit on rss, RLIMIT_RSS"),
        Origin("startcode", ("mm",), "start_code",
               doc="the address above which program text can run"),
        Origin("endcode", ("mm",), "end_code",
               doc="the address below which program text can run"),
        Origin("startstack", ("mm",), "start_stack",
               doc="the address of the bottom of the stack"),
        Origin("kstkesp", (), "", helper="KSTK_ESP",
               doc="the stack pointer from the kernel stack page; zeroed "
                   "unless the task is dumping core"),
        Origin("kstkeip", (), "", helper="KSTK_EIP",
               doc="the instruction pointer, zeroed for the same reason"),
        Origin("signal", (), "pending",
               doc="pending signals as a decimal bitmap; obsolete, it "
                   "omits real-time signals"),
        Origin("blocked", (), "blocked",
               doc="blocked signals as a decimal bitmap; obsolete"),
        Origin("sigignore", ("sighand",), "action",
               doc="ignored signals as a decimal bitmap; obsolete"),
        Origin("sigcatch", ("sighand",), "action",
               doc="caught signals as a decimal bitmap; obsolete"),
        Origin("wchan", (), "", helper="task_is_running",
               doc="the channel the process is waiting in; printed as 0 "
                   "or 1 because the address was an information leak"),
        Origin("nswap", (), "",
               doc="pages swapped, not maintained"),
        Origin("cnswap", (), "",
               doc="cumulative nswap for children, not maintained"),
        Origin("exit_signal", (), "exit_signal",
               doc="the signal sent to the parent when this task dies"),
        Origin("processor", (), "thread_info",
               doc="the CPU number last executed on"),
        Origin("rt_priority", (), "rt_priority",
               doc="real-time priority, 1 to 99 under a real-time "
                   "policy, otherwise 0"),
        Origin("policy", (), "policy",
               doc="the scheduling policy, the SCHED_* constants"),
        Origin("delayacct_blkio_ticks", (), "delays", optional=True,
               doc="aggregated block I/O delays, in centiseconds"),
        Origin("guest_time", (), "gtime",
               doc="time running a virtual CPU for a guest, in clock "
                   "ticks"),
        Origin("cguest_time", ("signal",), "cgtime",
               doc="guest time of children"),
        Origin("start_data", ("mm",), "start_data",
               doc="the address above which initialised and BSS data are "
                   "placed"),
        Origin("end_data", ("mm",), "end_data",
               doc="the address below which initialised and BSS data are "
                   "placed"),
        Origin("start_brk", ("mm",), "start_brk",
               doc="the address above which the heap can be expanded "
                   "with brk(2)"),
        Origin("arg_start", ("mm",), "arg_start",
               doc="the address above which argv is placed"),
        Origin("arg_end", ("mm",), "arg_end",
               doc="the address below which argv is placed"),
        Origin("env_start", ("mm",), "env_start",
               doc="the address above which the environment is placed"),
        Origin("env_end", ("mm",), "env_end",
               doc="the address below which the environment is placed"),
        Origin("exit_code", (), "exit_code",
               doc="the thread's exit status as waitpid(2) reports it"),
    ),
)


# ---------------------------------------------------------------------- maps

# One line per mapping. Every column comes from the vm_area_struct the line
# stands for, so the origins here are the columns rather than the lines.
MAPS = PseudoFile(
    path="/proc/<pid>/maps",
    root="task_struct",
    function="show_map_vma",
    defined_in="fs/proc/task_mmu.c",
    source="proc_pid_maps(5)",
    doc="",
    repeating=True,
    line_root="vm_area_struct",
    line_hops=("mm",),
    line_walk="mm_mt",
    origins=(
        Origin("address range", (), "vm_start",
               doc="the part of the address space this mapping occupies, "
                   "vm_start-vm_end"),
        Origin("permissions", (), "vm_flags",
               doc="r read, w write, x execute, then s shared or p "
                   "private (copy on write)"),
        Origin("offset", (), "vm_pgoff",
               doc="the offset into the file; in bytes here, in pages in "
                   "the struct"),
        Origin("device", ("vm_file", "f_inode", "i_sb"), "s_dev",
               doc="the device the file lives on, major:minor"),
        Origin("inode", ("vm_file", "f_inode"), "i_ino",
               doc="the inode on that device; 0 when no inode backs the "
                   "mapping, as for BSS"),
        Origin("path", ("vm_file",), "f_path",
               doc="the file backing the mapping, or a pseudo-path: "
                   "[heap], [stack], [vdso]"),
    ),
)


# ----------------------------------------------------------------- schedstat

SCHEDSTAT = PseudoFile(
    path="/proc/<pid>/schedstat",
    root="task_struct",
    function="proc_pid_schedstat",
    defined_in="fs/proc/base.c",
    doc="",
    positional=True,
    origins=(
        Origin("sum_exec_runtime", (".se",), "sum_exec_runtime",
               doc="nanoseconds actually spent running"),
        Origin("run_delay", (".sched_info",), "run_delay",
               doc="nanoseconds runnable but not running, which is what a "
                   "loaded machine costs this task"),
        Origin("pcount", (".sched_info",), "pcount",
               doc="times it was put on a CPU"),
    ),
)


# --------------------------------------------------------------------- sched

# Printed by macros over one task: PN() and P() name the member they print, so
# a label here is the member path with the prefix the macro adds.
SCHED = PseudoFile(
    path="/proc/<pid>/sched",
    root="task_struct",
    function="proc_sched_show_task",
    defined_in="kernel/sched/debug.c",
    doc="The statistics block is empty unless schedstats is on.",
    origins=(
        Origin("se.exec_start", (".se",), "exec_start",
               doc="when it last started running, on the CPU's clock"),
        Origin("se.vruntime", (".se",), "vruntime",
               doc="virtual runtime, the ordering key EEVDF picks by"),
        Origin("se.sum_exec_runtime", (".se",), "sum_exec_runtime"),
        Origin("se.nr_migrations", (".se",), "nr_migrations",
               doc="times it moved between CPUs"),
        Origin("se.slice", (".se",), "slice",
               doc="the slice it asks for, printed for fair tasks only"),
        Origin("sum_sleep_runtime", (".stats",), "sum_sleep_runtime"),
        Origin("sum_block_runtime", (".stats",), "sum_block_runtime"),
        Origin("wait_start", (".stats",), "wait_start"),
        Origin("sleep_start", (".stats",), "sleep_start"),
        Origin("block_start", (".stats",), "block_start"),
        Origin("sleep_max", (".stats",), "sleep_max"),
        Origin("block_max", (".stats",), "block_max"),
        Origin("exec_max", (".stats",), "exec_max",
               doc="the longest it ever ran without being taken off"),
        Origin("slice_max", (".stats",), "slice_max"),
        Origin("wait_max", (".stats",), "wait_max",
               doc="the longest it ever waited for a CPU"),
        Origin("wait_sum", (".stats",), "wait_sum"),
        Origin("wait_count", (".stats",), "wait_count"),
        Origin("iowait_sum", (".stats",), "iowait_sum"),
        Origin("iowait_count", (".stats",), "iowait_count"),
        Origin("nr_migrations_cold", (".stats",), "nr_migrations_cold"),
        Origin("nr_failed_migrations_affine", (".stats",),
               "nr_failed_migrations_affine",
               doc="wanted to move but the affinity mask forbade it"),
        Origin("nr_failed_migrations_running", (".stats",),
               "nr_failed_migrations_running"),
        Origin("nr_failed_migrations_hot", (".stats",),
               "nr_failed_migrations_hot",
               doc="left where it was because its cache was still warm"),
        Origin("nr_forced_migrations", (".stats",), "nr_forced_migrations"),
        Origin("nr_wakeups", (".stats",), "nr_wakeups"),
        Origin("nr_wakeups_sync", (".stats",), "nr_wakeups_sync"),
        Origin("nr_wakeups_migrate", (".stats",), "nr_wakeups_migrate"),
        Origin("nr_wakeups_local", (".stats",), "nr_wakeups_local",
               doc="woken on the CPU that woke it"),
        Origin("nr_wakeups_remote", (".stats",), "nr_wakeups_remote"),
        Origin("nr_wakeups_affine", (".stats",), "nr_wakeups_affine"),
        Origin("nr_wakeups_affine_attempts", (".stats",),
               "nr_wakeups_affine_attempts"),
        Origin("nr_wakeups_passive", (".stats",), "nr_wakeups_passive"),
        Origin("nr_wakeups_idle", (".stats",), "nr_wakeups_idle"),
        Origin("core_forceidle_sum", (".stats",), "core_forceidle_sum",
               optional=True),
        Origin("avg_atom", (), "", helper="proc_sched_show_task",
               doc="sum_exec_runtime divided by the switch count: how long "
                   "it holds a CPU each time"),
        Origin("avg_per_cpu", (), "", helper="proc_sched_show_task",
               doc="sum_exec_runtime divided by the migration count"),
        Origin("nr_switches", (), "", helper="proc_sched_show_task",
               doc="nvcsw + nivcsw, added up for this line only"),
        Origin("nr_voluntary_switches", (), "nvcsw"),
        Origin("nr_involuntary_switches", (), "nivcsw"),
        Origin("se.load.weight", (".se", ".load"), "weight",
               doc="the nice value as a weight, which is what the scheduler "
                   "divides CPU time by"),
        Origin("se.avg.load_sum", (".se", ".avg"), "load_sum"),
        Origin("se.avg.runnable_sum", (".se", ".avg"), "runnable_sum"),
        Origin("se.avg.util_sum", (".se", ".avg"), "util_sum"),
        Origin("se.avg.load_avg", (".se", ".avg"), "load_avg"),
        Origin("se.avg.runnable_avg", (".se", ".avg"), "runnable_avg"),
        Origin("se.avg.util_avg", (".se", ".avg"), "util_avg",
               doc="how much of a CPU it uses, out of 1024"),
        Origin("se.avg.last_update_time", (".se", ".avg"), "last_update_time"),
        Origin("se.avg.util_est", (".se", ".avg"), "util_est", optional=True),
        Origin("uclamp.min", (), "uclamp_req", optional=True,
               doc="the floor this task asks for, UCLAMP_MIN"),
        Origin("uclamp.max", (), "uclamp_req", optional=True),
        Origin("effective uclamp.min", (), "", optional=True, helper="uclamp_eff_value",
               doc="what it gets after its cgroup's limits are applied"),
        Origin("effective uclamp.max", (), "", optional=True),
        Origin("policy", (), "policy"),
        Origin("prio", (), "prio"),
        Origin("dl.runtime", (".dl",), "runtime", optional=True,
               doc="deadline tasks only"),
        Origin("dl.deadline", (".dl",), "deadline", optional=True),
        Origin("ext.enabled", (), "", optional=True, helper="task_on_scx",
               doc="task_on_scx(): whether a BPF scheduler owns this task"),
        Origin("clock-delta", (), "", helper="cpu_clock",
               doc="two clock reads back to back, printed to show what "
                   "reading the clock costs"),
        Origin("mm->numa_scan_seq", ("mm",), "numa_scan_seq", optional=True,
               doc="how many NUMA balancing scans this address space has had"),
        Origin("numa_pages_migrated", (), "numa_pages_migrated", optional=True),
        Origin("numa_preferred_nid", (), "numa_preferred_nid", optional=True),
        Origin("total_numa_faults", (), "total_numa_faults", optional=True),
        Origin("current_node", (), "", optional=True),
    ),
)


# --------------------------------------------------------------------- statm

STATM = PseudoFile(
    path="/proc/<pid>/statm",
    root="task_struct",
    function="proc_pid_statm",
    defined_in="fs/proc/array.c",
    source="proc_pid_statm(5)",
    doc="Measured in pages.",
    positional=True,
    origins=(
        Origin("size", ("mm",), "total_vm",
               doc="total program size, the same as VmSize in status"),
        Origin("resident", ("mm",), "rss_stat",
               doc="resident set size, the same as VmRSS; approximate"),
        Origin("shared", ("mm",), "rss_stat",
               doc="resident shared pages, those backed by a file: "
                   "RssFile + RssShmem"),
        Origin("text", ("mm",), "start_code",
               doc="text, computed from end_code minus start_code"),
        Origin("lib", (), "",
               doc="library, unused since 2.6 and always 0"),
        Origin("data", ("mm",), "data_vm",
               doc="data + stack, so data_vm and stack_vm added"),
        Origin("dt", (), "",
               doc="dirty pages, unused since 2.6 and always 0"),
    ),
)


# -------------------------------------------------------------------- limits

LIMITS = PseudoFile(
    path="/proc/<pid>/limits",
    root="task_struct",
    function="proc_pid_limits",
    defined_in="fs/proc/base.c",
    source="proc_pid_limits(5)",
    doc="",
    repeating=True,
    line_root="rlimit",
    line_hops=("signal",),
    line_walk="rlim[]",
    origins=(
        Origin("limit", (), "",
               doc="the resource; the name is in the kernel's lnames[] "
                   "table, not in a struct"),
        Origin("soft limit", (), "rlim_cur",
               doc="what is enforced now; the process may raise it as "
                   "far as the hard limit"),
        Origin("hard limit", (), "rlim_max",
               doc="the ceiling, which an unprivileged process can only "
                   "lower"),
        Origin("units", (), "",
               doc="the unit the two limits are counted in, also from "
                   "lnames[]"),
    ),
)


# ------------------------------------------------------------------------ io

IO = PseudoFile(
    path="/proc/<pid>/io",
    root="task_struct",
    function="do_io_accounting",
    defined_in="fs/proc/base.c",
    source="proc_pid_io(5)",
    doc="Summed over the group's threads. The counters are not atomic.",
    origins=(
        Origin("rchar", (".ioac",), "rchar",
               doc="bytes returned by successful read(2) and similar "
                   "calls"),
        Origin("wchar", (".ioac",), "wchar",
               doc="bytes returned by successful write(2) and similar "
                   "calls"),
        Origin("syscr", (".ioac",), "syscr",
               doc="file read system calls, including those the kernel "
                   "makes on the task's behalf"),
        Origin("syscw", (".ioac",), "syscw",
               doc="file write system calls"),
        Origin("read_bytes", (".ioac",), "read_bytes",
               doc="bytes really fetched from the storage layer"),
        Origin("write_bytes", (".ioac",), "write_bytes",
               doc="bytes really sent to the storage layer"),
        Origin("cancelled_write_bytes", (".ioac",), "cancelled_write_bytes",
               doc="bytes saved from writeback by truncation: written to "
                   "cache, then the file was removed"),
    ),
)


# ------------------------------------------------------------------ loadavg

LOADAVG = PseudoFile(
    path="/proc/loadavg",
    root="",
    function="loadavg_proc_show",
    defined_in="fs/proc/loadavg.c",
    source="proc_loadavg(5)",
    doc="",
    positional=True,
    origins=(
        Origin("load 1 min", (), "", symbol="avenrun", index="0",
               doc="avenrun[0], an exponentially decaying average of the "
                   "runnable and uninterruptible task count"),
        Origin("load 5 min", (), "", symbol="avenrun", index="1",
               doc="the same average over five minutes"),
        Origin("load 15 min", (), "", symbol="avenrun", index="2",
               doc="the same average over fifteen minutes"),
        Origin("running/threads", (), "", helper="nr_running",
               doc="nr_running() sums every runqueue; the second number is "
                   "the global nr_threads"),
        Origin("last pid", (), "", helper="idr_get_cursor",
               doc="the pid namespace's idr cursor minus one, so the last "
                   "pid allocated in this namespace"),
    ),
)


# ------------------------------------------------------------------- uptime

UPTIME = PseudoFile(
    path="/proc/uptime",
    root="",
    function="uptime_proc_show",
    defined_in="fs/proc/uptime.c",
    source="proc_uptime(5)",
    doc="",
    positional=True,
    origins=(
        Origin("uptime", (), "", helper="ktime_get_boottime_ts64",
               doc="seconds since boot, from the boottime clock rather than "
                   "from any counter"),
        Origin("idle", (), "", helper="kcpustat_cpu_fetch",
               doc="seconds spent idle, summed over every possible CPU's "
                   "kernel_cpustat; it exceeds uptime on a multiprocessor"),
    ),
)


# ------------------------------------------------------------------ meminfo

MEMINFO = PseudoFile(
    path="/proc/meminfo",
    root="",
    function="meminfo_proc_show",
    defined_in="fs/proc/meminfo.c",
    source="proc_meminfo(5)",
    doc="Counted in pages, printed in kB. Which lines appear depends on "
        "the configuration.",
    origins=(
        Origin("MemTotal", (), "", symbol="_totalram_pages", optional=True,
               doc="pages the kernel manages, after the firmware and the "
                   "kernel image are taken out"),
        Origin("MemFree", (), "", symbol="vm_zone_stat", index="NR_FREE_PAGES", optional=True,
               doc="on a free list right now"),
        Origin("MemAvailable", (), "", optional=True, helper="si_mem_available",
               doc="si_mem_available(): an estimate of what a new "
                   "workload could get without swapping"),
        Origin("Buffers", (), "", optional=True, helper="nr_blockdev_pages",
               doc="nr_blockdev_pages(): page cache held against block "
                   "devices themselves"),
        Origin("Cached", (), "", symbol="vm_node_stat", index="NR_FILE_PAGES", optional=True,
               doc="page cache, less the swap cache and Buffers"),
        Origin("SwapCached", (), "", optional=True, helper="total_swapcache_pages",
               doc="total_swapcache_pages(): pages that are both in swap "
                   "and in memory"),
        Origin("Active", (), "", symbol="vm_node_stat", index="NR_ACTIVE_ANON", optional=True,
               doc="the active anon and file LRU lists added"),
        Origin("Inactive", (), "", symbol="vm_node_stat", index="NR_INACTIVE_ANON", optional=True,
               doc="the inactive anon and file LRU lists added"),
        Origin("Active(anon)", (), "", symbol="vm_node_stat", index="NR_ACTIVE_ANON", optional=True,
               doc="anonymous pages on the active list"),
        Origin("Inactive(anon)", (), "", symbol="vm_node_stat", index="NR_INACTIVE_ANON", optional=True,
               doc="anonymous pages on the inactive list, which reclaim "
                   "takes first"),
        Origin("Active(file)", (), "", symbol="vm_node_stat", index="NR_ACTIVE_FILE", optional=True,
               doc="file pages on the active list"),
        Origin("Inactive(file)", (), "", symbol="vm_node_stat", index="NR_INACTIVE_FILE", optional=True,
               doc="file pages on the inactive list"),
        Origin("Unevictable", (), "", symbol="vm_node_stat", index="NR_UNEVICTABLE", optional=True,
               doc="pages reclaim cannot take, mlocked or otherwise "
                   "pinned to memory"),
        Origin("Mlocked", (), "", symbol="vm_zone_stat", index="NR_MLOCK", optional=True,
               doc="pages locked by mlock(2)"),
        Origin("SwapTotal", (), "", optional=True, helper="si_swapinfo",
               doc="si_swapinfo(): swap space configured"),
        Origin("SwapFree", (), "", optional=True, helper="si_swapinfo",
               doc="si_swapinfo(): swap space unused"),
        Origin("Zswap", (), "", optional=True, helper="zswap_total_pages",
               doc="zswap_total_pages(): memory the compressed swap cache "
                   "occupies"),
        Origin("Zswapped", (), "", symbol="zswap_stored_pages", optional=True,
               doc="the uncompressed size of what zswap holds"),
        Origin("HardwareCorrupted", (), "", symbol="num_poisoned_pages",
               optional=True,
               doc="pages taken out of service after a memory error"),
        Origin("Dirty", (), "", symbol="vm_node_stat", index="NR_FILE_DIRTY", optional=True,
               doc="written to but not yet on disk"),
        Origin("Writeback", (), "", symbol="vm_node_stat", index="NR_WRITEBACK", optional=True,
               doc="being written back right now"),
        Origin("AnonPages", (), "", symbol="vm_node_stat", index="NR_ANON_MAPPED", optional=True,
               doc="anonymous pages mapped into page tables"),
        Origin("Mapped", (), "", symbol="vm_node_stat", index="NR_FILE_MAPPED", optional=True,
               doc="file pages mapped into page tables"),
        Origin("Shmem", (), "", symbol="vm_node_stat", index="NR_SHMEM", optional=True,
               doc="tmpfs and shared anonymous pages"),
        Origin("KReclaimable", (), "", symbol="vm_node_stat", index="NR_SLAB_RECLAIMABLE_B", optional=True,
               doc="kernel memory reclaim can take back, slab and "
                   "otherwise"),
        Origin("Slab", (), "", symbol="vm_node_stat", index="NR_SLAB_RECLAIMABLE_B", optional=True,
               doc="the two slab counters added"),
        Origin("SReclaimable", (), "", symbol="vm_node_stat", index="NR_SLAB_RECLAIMABLE_B", optional=True,
               doc="slab memory reclaim can take back, mostly caches of "
                   "inodes and dentries"),
        Origin("SUnreclaim", (), "", symbol="vm_node_stat", index="NR_SLAB_UNRECLAIMABLE_B", optional=True,
               doc="slab memory that stays until freed"),
        Origin("KernelStack", (), "", symbol="vm_node_stat", index="NR_KERNEL_STACK_KB", optional=True,
               doc="kernel stacks, already in kB rather than pages"),
        Origin("PageTables", (), "", symbol="vm_node_stat", index="NR_PAGETABLE", optional=True,
               doc="page tables themselves"),
        Origin("SecPageTables", (), "", symbol="vm_node_stat", index="NR_SECONDARY_PAGETABLE", optional=True,
               doc="page tables for a second MMU, a guest's or a "
                   "device's"),
        Origin("NFS_Unstable", (), "", optional=True,
               doc="always 0 since the NFS accounting was removed"),
        Origin("Bounce", (), "", optional=True,
               doc="always 0"),
        Origin("WritebackTmp", (), "", optional=True,
               doc="always 0"),
        Origin("CommitLimit", (), "", optional=True, helper="vm_commit_limit",
               doc="vm_commit_limit(): how much the kernel will promise "
                   "when overcommit is limited"),
        Origin("Committed_AS", (), "", symbol="vm_committed_as", optional=True,
               doc="how much has been promised to processes, whether or "
                   "not they touched it"),
        Origin("VmallocTotal", (), "", optional=True,
               doc="the size of the vmalloc address range, a constant"),
        Origin("VmallocUsed", (), "", symbol="nr_vmalloc_pages", optional=True,
               doc="pages backing vmalloc allocations"),
        Origin("VmallocChunk", (), "", optional=True,
               doc="always 0 since the largest free chunk stopped being "
                   "tracked"),
        Origin("Percpu", (), "", optional=True, helper="pcpu_nr_pages",
               doc="pcpu_nr_pages(): backing for percpu allocations"),
        Origin("AnonHugePages", (), "", symbol="vm_node_stat", index="NR_ANON_THPS", optional=True,
               doc="anonymous transparent huge pages"),
        Origin("ShmemHugePages", (), "", symbol="vm_node_stat", index="NR_SHMEM_THPS", optional=True,
               doc="tmpfs transparent huge pages"),
        Origin("ShmemPmdMapped", (), "", symbol="vm_node_stat", index="NR_SHMEM_PMDMAPPED", optional=True,
               doc="tmpfs huge pages mapped with a PMD entry"),
        Origin("FileHugePages", (), "", symbol="vm_node_stat", index="NR_FILE_THPS", optional=True,
               doc="page cache transparent huge pages"),
        Origin("FilePmdMapped", (), "", symbol="vm_node_stat", index="NR_FILE_PMDMAPPED", optional=True,
               doc="page cache huge pages mapped with a PMD entry"),
        Origin("CmaTotal", (), "", symbol="totalcma_pages", optional=True,
               doc="pages reserved for the contiguous memory allocator"),
        Origin("CmaFree", (), "", symbol="vm_zone_stat", index="NR_FREE_CMA_PAGES", optional=True,
               doc="of those, the ones still free"),
        Origin("Unaccepted", (), "", symbol="vm_zone_stat", index="NR_UNACCEPTED", optional=True,
               doc="memory a confidential guest has not yet accepted "
                   "from the host"),
        Origin("Balloon", (), "", symbol="vm_node_stat", index="NR_BALLOON_PAGES", optional=True,
               doc="pages handed back to the hypervisor by a balloon "
                   "driver"),
        Origin("HugePages_Total", (), "", optional=True, helper="hugetlb_report_meminfo",
               doc="hugetlb_report_meminfo(): pages in the default "
                   "hstate's pool"),
        Origin("HugePages_Free", (), "", optional=True, helper="hugetlb_report_meminfo",
               doc="of those, unallocated"),
        Origin("HugePages_Rsvd", (), "", optional=True, helper="hugetlb_report_meminfo",
               doc="promised but not yet faulted in"),
        Origin("HugePages_Surp", (), "", optional=True, helper="hugetlb_report_meminfo",
               doc="allocated above the configured pool size"),
        Origin("Hugepagesize", (), "", optional=True, helper="hugetlb_report_meminfo",
               doc="the default huge page size"),
        Origin("Hugetlb", (), "", optional=True, helper="hugetlb_report_meminfo",
               doc="every hstate's pool, added"),
        Origin("DirectMap4k", (), "", optional=True, helper="arch_report_meminfo",
               doc="arch_report_meminfo(): kernel direct map covered by "
                   "4k entries"),
        Origin("DirectMap2M", (), "", optional=True, helper="arch_report_meminfo",
               doc="kernel direct map covered by 2M entries"),
        Origin("DirectMap1G", (), "", optional=True, helper="arch_report_meminfo",
               doc="kernel direct map covered by 1G entries"),
    ),
)




# ------------------------------------------------------- one value per file

# Files holding a single number or word. The table is still worth having: the
# value alone does not say which member it came from, and several of these
# are written as well as read.
COMM = PseudoFile(
    path="/proc/<pid>/comm",
    root="task_struct",
    function="proc_task_name",
    defined_in="fs/proc/array.c",
    source="proc_pid_comm(5)",
    doc="",
    positional=True,
    origins=(
        Origin("comm", (), "comm",
               doc="the command name, 16 bytes including the null; writing "
                   "this file sets it"),
    ),
)


WCHAN = PseudoFile(
    path="/proc/<pid>/wchan",
    root="task_struct",
    function="proc_pid_wchan",
    defined_in="fs/proc/base.c",
    source="proc_pid_wchan(5)",
    doc="",
    positional=True,
    origins=(
        Origin("wchan", (), "", helper="get_wchan",
               doc="the symbol the task is blocked in, resolved from its "
                   "kernel stack; empty when it is running"),
    ),
)


OOM_SCORE = PseudoFile(
    path="/proc/<pid>/oom_score",
    root="task_struct",
    function="proc_oom_score",
    defined_in="fs/proc/base.c",
    source="proc_pid_oom_score(5)",
    doc="",
    positional=True,
    origins=(
        Origin("oom_score", (), "", helper="oom_badness",
               doc="how attractive this process is to the OOM killer, "
                   "computed from its rss, page tables and swap use"),
    ),
)


OOM_SCORE_ADJ = PseudoFile(
    path="/proc/<pid>/oom_score_adj",
    root="task_struct",
    function="oom_score_adj_read",
    defined_in="fs/proc/base.c",
    source="proc_pid_oom_score_adj(5)",
    doc="",
    positional=True,
    origins=(
        Origin("oom_score_adj", ("signal",), "oom_score_adj",
               doc="-1000 to 1000, added to the score; -1000 exempts the "
                   "process from the OOM killer"),
    ),
)


PERSONALITY = PseudoFile(
    path="/proc/<pid>/personality",
    root="task_struct",
    function="proc_pid_personality",
    defined_in="fs/proc/base.c",
    source="proc_pid_personality(5)",
    doc="",
    positional=True,
    origins=(
        Origin("personality", (), "personality",
               doc="the execution domain word, from personality(2)"),
    ),
)


COREDUMP_FILTER = PseudoFile(
    path="/proc/<pid>/coredump_filter",
    root="task_struct",
    function="proc_coredump_filter_read",
    defined_in="fs/proc/base.c",
    source="proc_pid_coredump_filter(5)",
    doc="",
    positional=True,
    origins=(
        Origin("coredump_filter", ("mm",), "flags",
               doc="which kinds of mapping a core dump includes, the MMF_DUMP "
                   "bits of mm->flags"),
    ),
)


TIMERSLACK_NS = PseudoFile(
    path="/proc/<pid>/timerslack_ns",
    root="task_struct",
    function="timerslack_ns_show",
    defined_in="fs/proc/base.c",
    source="proc_pid_timerslack_ns(5)",
    doc="",
    positional=True,
    origins=(
        Origin("timerslack_ns", (), "timer_slack_ns",
               doc="how long a timer for this task may be deferred so it can "
                   "be batched with others"),
    ),
)


FILES: tuple[PseudoFile, ...] = (STATUS, STAT, MAPS, SCHED, SCHEDSTAT,
                                 STATM, LIMITS, IO, LOADAVG, UPTIME,
                                 MEMINFO, COMM, WCHAN, OOM_SCORE,
                                 OOM_SCORE_ADJ, PERSONALITY,
                                 COREDUMP_FILTER, TIMERSLACK_NS)

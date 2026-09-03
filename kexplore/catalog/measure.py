"""Short foreground measurements, attached to the subsystem they describe.

Measurements are grouped by what they are about, not by how they are taken:
scheduler measurements live under ``sched`` next to the runqueues they explain.
The mechanism (bpftrace, tracepoints) is an implementation detail in
``core/probe.py``.

The four scheduler measurements are legs of one loop, not separate topics::

    wakeup --(1) waiting--> on CPU --(2) running--> switched out
      ^                                                  |
      +---------------(3) off-CPU-----------------------+

Leg 3 contains leg 1, so leg 3 minus leg 1 is the time a task was genuinely
blocked rather than waiting for a CPU. The fourth entry counts transitions
around that loop instead of timing a leg.

Every probe names the two events it measures between, in the map name itself:
``@us_from_sched_wakeup_to_oncpu`` says exactly what the number is, where
"runqueue latency" would not. If you cannot state a metric as "time from
tracepoint A to tracepoint B", it does not belong here.

Each probe also records its blind spots. A measurement anchored on
``sched_wakeup`` cannot see a task that was preempted and re-queued, because
that path emits no wakeup -- and knowing that is the difference between reading
a histogram and trusting it.

This file should be split, one measurement into the file for the subsystem it
describes. Grouping by mechanism (everything bpftrace here) is the opposite of
how the rest of the catalog is split, and it costs two things: a measurement's
subsystem is only the dict key it happens to sit under, tens of lines above it,
and reading a subsystem means reading two files. With measurements for two of
the twelve subsystems the file is manageable; filled in, it would restate the
whole catalog in bpftrace.

The move: each Measurement and its script constant into that subsystem's
``entries=[...]``, after the browsable entries, keeping ``group="measure"``
because that is what makes the collapsible branch in the tree. The loop diagram
above goes to sched.py with the four measurements it explains; the general rules
(name the two tracepoints in the map name, record the blind spot) go to
registry.py next to Measurement, where anyone writing one will see them. Left
here: interrupts and syscalls, which no subsystem owns.

That leaves this file as the only caller of ``attach``, so attach, _ATTACHED and
the merge in ``subsystems()`` can go with it unless something else needs to
register entries from another file. tests/test_catalog.py asserts the current
arrangement (that sched's five are attached, that measure keeps two) and would
be rewritten to check membership instead.
"""

from __future__ import annotations

from .registry import Measurement, Subsystem, attach, register

# include/linux/interrupt.h -- softirq vector numbers.
SOFTIRQ_VECTORS = {
    "0": "HI",
    "1": "TIMER",
    "2": "NET_TX",
    "3": "NET_RX",
    "4": "BLOCK",
    "5": "IRQ_POLL",
    "6": "TASKLET",
    "7": "SCHED",
    "8": "HRTIMER",
    "9": "RCU",
}


SCHEDULE_RATE = """
tracepoint:sched:sched_switch { @switches_per_cpu[cpu] = count(); }
tracepoint:sched:sched_wakeup { @wakeups_per_cpu[cpu] = count(); }
tracepoint:sched:sched_migrate_task { @migrations_per_cpu[cpu] = count(); }
interval:s:{duration} { exit(); }
"""

# Split by whether the CPU was idle immediately before taking the task. Waking
# an idle CPU costs an IPI plus leaving WFI (and, in a VM, a hypervisor round
# trip), which dwarfs the scheduler decision. Without the split the two are
# averaged into one number that looks like queueing but is not.
RUNQ_WAIT = """
tracepoint:sched:sched_wakeup { @wake[args.pid] = nsecs; }
tracepoint:sched:sched_wakeup_new { @wake[args.pid] = nsecs; }
tracepoint:sched:sched_switch /@wake[args.next_pid]/ {
  $d = (nsecs - @wake[args.next_pid]) / 1000;
  if (args.prev_pid == 0) {
    @us_wakeup_to_oncpu_cpu_was_idle = hist($d);
  } else {
    @us_wakeup_to_oncpu_cpu_was_busy = hist($d);
  }
  delete(@wake[args.next_pid]);
}
interval:s:{duration} { exit(); }
END { clear(@wake); }
"""

# Idle is not a time slice. Track each task's own on-CPU span and keep the
# swapper (pid 0) spans separately, or the two get mixed into one histogram
# spanning five orders of magnitude.
TIMESLICE = """
tracepoint:sched:sched_switch {
  $prev = @on[args.prev_pid];
  if ($prev) {
    if (args.prev_pid == 0) {
      @us_cpu_idle_in_swapper = hist((nsecs - $prev) / 1000);
    } else {
      @us_task_running_on_cpu = hist((nsecs - $prev) / 1000);
    }
    delete(@on[args.prev_pid]);
  }
  @on[args.next_pid] = nsecs;
}
interval:s:{duration} { exit(); }
END { clear(@on); }
"""

# fentry/fexit rather than kprobe/kretprobe: measured side by side on the same
# function, kretprobe added 2-4us of its own overhead while fentry cost ~100ns.
# At this scale the instrument would otherwise dominate the measurement.
WAKEUP_BREAKDOWN = """
tracepoint:sched:sched_waking { @waking[args.pid] = nsecs; }
tracepoint:sched:sched_wakeup /@waking[args.pid]/ {
  @ns_stage1_sched_waking_to_sched_wakeup = hist(nsecs - @waking[args.pid]);
  @wakeup[args.pid] = nsecs;
  delete(@waking[args.pid]);
}
tracepoint:sched:sched_switch /@wakeup[args.next_pid]/ {
  @ns_stage2_sched_wakeup_to_running = hist(nsecs - @wakeup[args.next_pid]);
  delete(@wakeup[args.next_pid]);
}
fentry:select_task_rq_fair { @a[tid] = nsecs; }
fexit:select_task_rq_fair /@a[tid]/ {
  @ns_fn_select_task_rq_fair = hist(nsecs - @a[tid]); delete(@a[tid]);
}
fentry:enqueue_task_fair { @b[tid] = nsecs; }
fexit:enqueue_task_fair /@b[tid]/ {
  @ns_fn_enqueue_task_fair = hist(nsecs - @b[tid]); delete(@b[tid]);
}
fentry:pick_next_task_fair { @c[tid] = nsecs; }
fexit:pick_next_task_fair /@c[tid]/ {
  @ns_fn_pick_next_task_fair = hist(nsecs - @c[tid]); delete(@c[tid]);
}
interval:s:{duration} { exit(); }
END { clear(@waking); clear(@wakeup); clear(@a); clear(@b); clear(@c); }
"""

INTERRUPTS = """
tracepoint:irq:irq_handler_entry { @h[cpu] = nsecs; @hardirq_count_by_name[str(args.name)] = count(); }
tracepoint:irq:irq_handler_exit /@h[cpu]/ {
  @ns_in_hardirq_handler = hist(nsecs - @h[cpu]); delete(@h[cpu]);
}
tracepoint:irq:softirq_entry { @s[cpu] = nsecs; @softirq_count_by_vec[args.vec] = count(); }
tracepoint:irq:softirq_exit /@s[cpu]/ {
  @ns_in_softirq_handler = hist(nsecs - @s[cpu]); delete(@s[cpu]);
}
interval:s:{duration} { exit(); }
END { clear(@h); clear(@s); }
"""

OFFCPU = """
tracepoint:sched:sched_switch {
  @off[args.prev_pid] = nsecs;
  $t = @off[args.next_pid];
  if ($t) { @us_off_cpu_between_switches = hist((nsecs - $t) / 1000); delete(@off[args.next_pid]); }
}
interval:s:{duration} { exit(); }
END { clear(@off); }
"""

# CLONE_THREAD. With clone3 the exit signal lives in kernel_clone_args.exit_signal
# rather than in flags, so a plain fork shows flags like 0x1200000
# (CHILD_SETTID|CHILD_CLEARTID) and a thread shows 0x3d0f00.
CLONE_COST = """
fentry:kernel_clone {
  @start[tid] = nsecs;
  // Store the raw flags: a boolean-valued map did not survive the round trip
  // and every clone was bucketed as a fork.
  @flags[tid] = args.args->flags;
}
fexit:kernel_clone /@start[tid]/ {
  $d = nsecs - @start[tid];
  $flags = @flags[tid];
  if ($flags & 0x10000) {
    @ns_kernel_clone_for_a_thread = hist($d);
  } else {
    @ns_kernel_clone_for_a_fork = hist($d);
  }
  delete(@start[tid]); delete(@flags[tid]);
}
fentry:copy_page_range { @cpr[tid] = nsecs; }
fexit:copy_page_range /@cpr[tid]/ {
  @ns_copy_page_range_fork_only = hist(nsecs - @cpr[tid]); delete(@cpr[tid]);
}
fentry:dup_fd { @dfd[tid] = nsecs; }
fexit:dup_fd /@dfd[tid]/ { @ns_dup_fd_fork_only = hist(nsecs - @dfd[tid]); delete(@dfd[tid]); }
fentry:do_wp_page { @count_cow_write_faults_after = count(); }
interval:s:{duration} { exit(); }
END { clear(@start); clear(@flags); clear(@cpr); clear(@dfd); }
"""

SYSCALLS = """
tracepoint:raw_syscalls:sys_enter { @syscall_count_by_nr[args.id] = count(); }
tracepoint:raw_syscalls:sys_enter { @e[tid] = nsecs; }
tracepoint:raw_syscalls:sys_exit /@e[tid]/ {
  @ns_in_syscall = hist(nsecs - @e[tid]); delete(@e[tid]);
}
interval:s:{duration} { exit(); }
END { clear(@e); }
"""


# Measurements keyed by the subsystem they belong under. Anything genuinely
# cross-cutting stays in "measure" until a subsystem exists to own it.
MEASUREMENTS: dict[str, list[Measurement]] = {
    "sched": [
        Measurement(
            key="sched_rate",
            label="how often are tasks scheduled?",
            doc="Counts of sched_switch, sched_wakeup and sched_migrate_task per CPU.",
            measures=(
                "Counts events, not durations. Per CPU: sched_switch (the CPU "
                "changed which task it runs), sched_wakeup (a task became "
                "runnable), sched_migrate_task (a task moved to another CPU)."
            ),
            blind_spot=(
                "A high switch count on an idle machine is mostly the CPU going in "
                "and out of the idle task, not contention."
            ),
            script=SCHEDULE_RATE,
            group="measure",
            per_second=True,
        ),
        Measurement(
            key="runq_wait",
            label="how long do runnable tasks wait for a CPU?",
            doc="Time from being made runnable to actually getting a CPU.",
            measures=(
                "Leg 1 of the task cycle: runnable but not yet running. From "
                "sched_wakeup (the task is put on a runqueue and is now eligible) "
                "to the sched_switch where it appears as next_pid (a CPU actually "
                "switches to it). Split by whether that CPU was idle beforehand."
            ),
            blind_spot=(
                "This is not the cost of the scheduler deciding: picking the next "
                "task is sub-microsecond. An idle-CPU wakeup also pays an IPI, "
                "leaving WFI, and inside a VM a hypervisor round trip, so the idle "
                "histogram is usually the slower one. Only woken tasks are counted "
                "-- a task preempted and re-queued emits no wakeup; the kernel's own "
                "running total for that is task->sched_info.run_delay."
            ),
            script=RUNQ_WAIT,
            group="measure",
        ),
        Measurement(
            key="wakeup_breakdown",
            label="where does wakeup latency go?",
            doc="Splits leg 1 into stages, and times the functions inside it.",
            measures=(
                "Two timeline stages: sched_waking (try_to_wake_up starts) to "
                "sched_wakeup (task enqueued and visible as runnable), then "
                "sched_wakeup to the sched_switch that runs it. Plus the duration "
                "of select_task_rq_fair, enqueue_task_fair and pick_next_task_fair "
                "via fentry/fexit. Nanoseconds."
            ),
            blind_spot=(
                "For a remote wakeup the kernel queues the task to the target CPU "
                "and IPIs it, and sched_wakeup then fires on the target -- so stage 1 "
                "already contains IPI delivery and the target leaving WFI, not just "
                "waker-side work. Inside a VM every duration also includes any time "
                "the host descheduled the vCPU, which is why the tails are wide."
            ),
            script=WAKEUP_BREAKDOWN,
            group="measure",
            duration=3,
        ),
        Measurement(
            key="timeslice",
            label="how long does a task hold a CPU?",
            doc="On-CPU span per task, with idle reported separately.",
            measures=(
                "Leg 2 of the task cycle: actually executing. From the sched_switch "
                "that puts a task on a CPU (next_pid) to the one that takes it off "
                "(prev_pid). Spans belonging to swapper (pid 0) are the CPU sitting "
                "idle and are reported as a separate histogram."
            ),
            blind_spot=(
                "A short span usually means the task blocked, not that it used up "
                "its slice -- compare the bulk of the distribution against "
                "sysctl_sched_base_slice under system > scheduler."
            ),
            script=TIMESLICE,
            group="measure",
        ),
        Measurement(
            key="offcpu",
            label="how long are tasks off-CPU?",
            doc="Time between a task being switched out and switched back in.",
            measures=(
                "Leg 3 of the task cycle: everything between two runs. From the "
                "sched_switch where a task is prev_pid (leaves the CPU) to the one "
                "where it is next_pid (returns). This is sleeping/blocked time plus "
                "leg 1, so subtracting runq_wait leaves the time truly blocked."
            ),
            blind_spot=(
                "Does not separate sleeping from waiting; runq_wait covers the "
                "waiting half."
            ),
            script=OFFCPU,
            group="measure",
        ),
    ],
    "process": [
        Measurement(
            key="clone_cost",
            label="what does a fork cost versus a thread?",
            doc="Times kernel_clone split by CLONE_THREAD, plus the work fork adds.",
            measures=(
                "fentry/fexit on kernel_clone, split by whether CLONE_THREAD is "
                "set: one histogram for threads, one for forks. Then the work "
                "only fork does: copy_page_range duplicates page tables per VMA, "
                "dup_fd copies the descriptor table. Finally do_wp_page counts "
                "copy-on-write faults, which is the cost paid after the call "
                "returns. Nanoseconds."
            ),
            blind_spot=(
                "Needs clones to happen while it runs; on an idle system it "
                "records nothing. Run tests/helpers/clone_cost.py to generate them. "
                "The COW fault count is system-wide, not per process."
            ),
            script=CLONE_COST,
            group="measure",
            duration=10,
        ),
    ],
    "measure": [
        Measurement(
            key="interrupts",
            label="hard vs soft interrupts",
            doc="Counts and handler durations for hardirqs and softirqs.",
            measures=(
                "Hard: irq:irq_handler_entry to irq:irq_handler_exit, counted by IRQ "
                "name. Soft: irq:softirq_entry to irq:softirq_exit, counted by "
                "vector. Nanoseconds inside the handler."
            ),
            blind_spot=(
                "Handler duration only. Excludes interrupt entry/exit cost and work "
                "deferred further, e.g. NET_RX handing off to a NAPI poll."
            ),
            script=INTERRUPTS,
            key_labels={"softirq_count_by_vec": SOFTIRQ_VECTORS},
        ),
        Measurement(
            key="syscalls",
            label="which syscalls, and how long?",
            doc="Syscall counts by number, and time spent inside syscalls.",
            measures=(
                "Counts raw_syscalls:sys_enter by syscall number, and measures "
                "sys_enter to sys_exit per thread. Nanoseconds."
            ),
            blind_spot="Counts are by syscall number, not name.",
            script=SYSCALLS,
        ),
    ],
}


# A measurement belongs under the subsystem it describes, so all but the
# cross-cutting ones are attached to a subsystem someone else registered.
for _key, _measurements in MEASUREMENTS.items():
    if _key != "measure":
        attach(_key, *_measurements)

register(
    Subsystem(
        key="measure",
        label="measure",
        doc="Cross-cutting measurements with no single owning subsystem yet.",
        entries=list(MEASUREMENTS["measure"]),
    )
)

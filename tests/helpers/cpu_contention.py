"""Make the scheduler accounting counters show something.

On an idle machine every task's ``sched_info.run_delay`` is a fraction of a
millisecond accumulated over days, so a walkthrough of the time accounting
fields opens on numbers that say nothing. This holds two kinds of task alive
at once so the fields can be compared:

  * ``spin``   twice as many CPU-bound loops as there are CPUs, so each one
               spends about half its time on a runqueue waiting for a CPU.
               run_delay grows as fast as on-CPU time; every context switch is
               involuntary.
  * ``sleep``  one task that sleeps in short intervals and never competes.
               run_delay stays near zero; every context switch is voluntary.

Each child sets its own comm, so a walkthrough can find them by name:
``kexplore-spin`` and ``kexplore-sleep``.

What it does to the machine, for as long as it runs:

  * every CPU is saturated, so anything else running is slowed down
  * ``kernel.task_delayacct`` is set to 1 if it is not already, and the
    previous value is restored on exit

It asks before starting when there is a terminal to ask on. Detached, pass
``--yes``:

    sudo setsid nohup python3 tests/helpers/cpu_contention.py 300 --yes \
        >/tmp/load.log 2>&1 </dev/null &
"""

from __future__ import annotations

import os
import signal
import sys
import time

ARGS = [a for a in sys.argv[1:] if a != "--yes"]
ASSUME_YES = "--yes" in sys.argv[1:]
SECONDS = int(ARGS[0]) if ARGS else 300


def set_comm(name: str) -> None:
    """Rename the task, so it can be found by comm rather than by pid."""
    try:
        with open("/proc/self/comm", "w") as comm:
            comm.write(name[:15])
    except OSError:
        pass


def spin(deadline: float) -> None:
    set_comm("kexplore-spin")
    while time.monotonic() < deadline:
        pass


def doze(deadline: float) -> None:
    set_comm("kexplore-sleep")
    while time.monotonic() < deadline:
        time.sleep(0.01)


DELAYACCT = "/proc/sys/kernel/task_delayacct"


def read_delayacct() -> str:
    try:
        with open(DELAYACCT) as switch:
            return switch.read().strip()
    except OSError:
        return ""


def write_delayacct(value: str) -> bool:
    try:
        with open(DELAYACCT, "w") as switch:
            switch.write(value)
        return True
    except OSError:
        return False


def enable_delayacct() -> str:
    """Turn on per-task delay accounting, and report what was there before.

    ``task->delays`` is allocated at fork, so the switch has to be on before
    the children below are created or their pointer stays NULL. The previous
    value is restored on exit: this is a setting on the machine, not on the
    tasks started here.
    """
    before = read_delayacct()
    if before == "1":
        print(f"kernel.task_delayacct is already 1", flush=True)
        return before
    if write_delayacct("1"):
        print(f"kernel.task_delayacct set to 1 (was {before or '?'}), "
              "restored on exit", flush=True)
    else:
        print("kernel.task_delayacct could not be set (needs root): "
              "task->delays will be NULL on these children", flush=True)
    return before


def consent() -> bool:
    """Say what this will do to the machine, and get an answer.

    kexplore is for disposable VMs and lab machines, but a helper that pins
    every CPU and writes a sysctl should say so before it does either.
    """
    print(f"This saturates all {os.cpu_count()} CPUs for {SECONDS}s and sets "
          "kernel.task_delayacct=1 (restored on exit).")
    if ASSUME_YES:
        return True
    if not sys.stdin.isatty():
        print("Not a terminal: re-run with --yes to confirm.")
        return False
    return input("Continue? [y/N] ").strip().lower() in ("y", "yes")


def main() -> int:
    if not consent():
        return 1
    delayacct_before = enable_delayacct()
    deadline = time.monotonic() + SECONDS
    children = []
    # Twice the CPU count: each spinner is runnable about twice as often as it
    # can be scheduled, which is what puts time into run_delay.
    for _ in range(2 * (os.cpu_count() or 2)):
        pid = os.fork()
        if pid == 0:
            spin(deadline)
            os._exit(0)
        children.append(pid)

    pid = os.fork()
    if pid == 0:
        doze(deadline)
        os._exit(0)
    children.append(pid)

    print(f"{len(children) - 1} spinners and 1 sleeper for {SECONDS}s: "
          f"{' '.join(str(c) for c in children)}", flush=True)


    def restore() -> None:
        if delayacct_before and delayacct_before != "1":
            write_delayacct(delayacct_before)

    def stop(*_: object) -> None:
        for child in children:
            try:
                os.kill(child, signal.SIGKILL)
            except ProcessLookupError:
                pass
        restore()
        sys.exit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    for child in children:
        try:
            os.waitpid(child, 0)
        except ChildProcessError:
            pass
    restore()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

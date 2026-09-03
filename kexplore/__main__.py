"""Entry point: attach to the live kernel and start the explorer."""

from __future__ import annotations

import argparse
import os
import sys


def main() -> int:
    parser = argparse.ArgumentParser(prog="kexplore", description=__doc__)
    parser.add_argument(
        "-c", "--core", help="explore a vmcore instead of the live kernel", default=None
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="resolve every subsystem entry and report, without starting the UI",
    )
    parser.add_argument(
        "--prefetch",
        action="store_true",
        help="download the kernel debuginfo to completion and exit, so later "
             "runs need no network",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="never contact a debuginfod server; use only what is already "
             "cached (also settable with KEXPLORE_OFFLINE=1)",
    )
    args = parser.parse_args()

    from .core import debuginfod
    from .core.source import kernel_build_id

    if args.prefetch and args.offline:
        print("--prefetch and --offline are contradictory", file=sys.stderr)
        return 1

    # KEXPLORE_OFFLINE is meant to stay set for as long as there is no network,
    # so an explicit --prefetch overrides it rather than being refused by it.
    offline = not args.prefetch and (
        args.offline or os.environ.get("KEXPLORE_OFFLINE", "") not in ("", "0")
    )
    debuginfod.configure(offline_only=offline)

    if args.prefetch:
        build_id = kernel_build_id()
        if not build_id:
            print("could not read the kernel build-id from /sys/kernel/notes",
                  file=sys.stderr)
            return 1
        return 0 if debuginfod.prefetch(build_id) else 1

    import drgn

    if args.core:
        prog = drgn.program_from_core_dump(args.core)
    else:
        if os.geteuid() != 0:
            print("kexplore needs root to read /proc/kcore", file=sys.stderr)
            return 1
        # Attaching loads the kernel's DWARF, which on a cold cache means a
        # several-hundred-megabyte download with no output of its own. Say so
        # before it starts rather than looking hung for minutes.
        build_id = kernel_build_id()
        print(debuginfod.status(build_id), file=sys.stderr)
        if offline and build_id and not debuginfod.is_cached(build_id):
            # Without the DWARF drgn attaches but cannot name a single type, so
            # fail here rather than at the first empty view.
            print("run 'kexplore --prefetch' once with a connection you are "
                  "happy to use, then --offline works with no network at all",
                  file=sys.stderr)
            return 1
        if build_id and not offline and not debuginfod.is_cached(build_id):
            # Let libdebuginfod narrate the transfer; without it the wait is
            # silent. Harmless when the fetch is a cache hit.
            os.environ.setdefault("DEBUGINFOD_PROGRESS", "1")
        print("attaching to the live kernel…", file=sys.stderr, flush=True)
        prog = drgn.program_from_kernel()

    # drgn attaches happily without DWARF and only fails at the first type
    # lookup, which turns into "could not find 'cpu_online_mask'" in every view
    # instead of one comprehensible error. Check once, here.
    if not _has_debug_info(prog):
        print("\nattached, but this kernel's debug info is not loaded: every "
              "view would fail.", file=sys.stderr)
        if not args.core:
            print("the download was interrupted, or the cache is cold. Run:\n"
                  "  kexplore --prefetch      (one uninterrupted download)\n"
                  "  kexplore --offline       (afterwards, no network at all)",
                  file=sys.stderr)
        return 1

    # Every "L<n>" in a field listing counts in these bytes, so resolve it once
    # here rather than assuming 64 in the row that renders it.
    from .core import arch, nav

    nav.set_cache_line(arch.cache_line_size(prog)[0])

    if args.check:
        return _check(prog)

    from .tui.app import Explorer

    Explorer(prog).run()
    return 0


def _has_debug_info(prog) -> bool:
    """True if the kernel's DWARF actually loaded.

    ``struct task_struct`` is the cheapest thing that is always there when the
    debuginfo is present and never there when it is not.
    """
    try:
        prog.type("struct task_struct")
    except Exception:  # noqa: BLE001 - drgn raises several kinds here
        return False
    return True


def _check(prog) -> int:
    """Smoke-test every catalog entry against this kernel.

    Helper availability shifts with kernel version and config, so this reports
    which entries actually resolve here rather than failing at browse time.
    Every kind of entry answers ``check()``, so there is nothing to dispatch
    on: objects, computed facts and measurements each report themselves.
    """
    from .catalog.registry import subsystems

    failures = 0
    for subsystem in subsystems():
        print(f"\n{subsystem.label}: {subsystem.doc}")
        for entry in subsystem.entries:
            result = entry.check(prog)
            failures += not result.ok
            mark = "ok  " if result.ok else "FAIL"
            print(f"  {mark}  {entry.label}: {result.detail}")
    print(f"\n{failures} failing entr{'y' if failures == 1 else 'ies'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

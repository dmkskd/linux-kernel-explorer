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
        help="download the kernel debuginfo to completion and exit. A cold "
             "first run does this download anyway; use this to prepare for "
             "offline work",
    )
    parser.add_argument(
        "--no-prefetch",
        action="store_true",
        help="attach without downloading debuginfo to completion first; the "
             "first run then fetches lazily during attach",
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

    def say(label: str, text: str) -> None:
        """Every startup line: one label column, then the value."""
        print(f"{label:<11}{text}", file=sys.stderr)

    def indented(text: str) -> None:
        print(text, file=sys.stderr)

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
        say("debug info", "fetching to completion")
        return 0 if debuginfod.prefetch(build_id, log=indented) else 1

    import drgn

    if args.core:
        prog = drgn.program_from_core_dump(args.core)
    else:
        if os.geteuid() != 0:
            print("kexplore needs root to read /proc/kcore", file=sys.stderr)
            return 1
        build_id = kernel_build_id()
        # Local debug packages (Ubuntu dbgsym, Debian -dbg) satisfy the DWARF
        # need without the debuginfod cache, so the gates below test both.
        have_dwarf = bool(
            debuginfod.local_vmlinux()
            or (build_id and debuginfod.is_cached(build_id))
        )
        # One heading, then the fetch indents under it. Printing the cache
        # status as well would say the same thing twice.
        downloading = bool(build_id) and not have_dwarf and not offline \
            and not args.no_prefetch
        if downloading:
            say("debug info", "not cached, downloading it now "
                             "(--no-prefetch attaches without it)")
        else:
            say("debug info", debuginfod.status(build_id))

        if offline and not have_dwarf:
            # Without the DWARF drgn attaches but cannot name a single type, so
            # fail here rather than at the first empty view.
            print("run 'kexplore --prefetch' once with a connection you are "
                  "happy to use, then --offline works with no network at all",
                  file=sys.stderr)
            return 1
        if not have_dwarf and not offline:
            if downloading:
                # Download here, to completion, with progress -- not silently
                # inside the attach, where interrupting looks like a hang and
                # throws the transfer away.
                if not debuginfod.prefetch(build_id, log=indented):
                    return 1
            else:
                # Let libdebuginfod narrate the transfer; without it the wait
                # is silent. Harmless when the fetch is a cache hit.
                os.environ.setdefault("DEBUGINFOD_PROGRESS", "1")
        say("kernel", "attaching to the live kernel…")
        sys.stderr.flush()
        prog = drgn.program_from_kernel()

    # drgn attaches happily without DWARF and only fails at the first type
    # lookup, which turns into "could not find 'cpu_online_mask'" in every view
    # instead of one comprehensible error. Check once, here. The first lookup
    # also builds drgn's DWARF index, which on a multi-GB debug package takes
    # a while, so time it: a slow one should be visible, not silent.
    import time

    start = time.monotonic()
    has_debug = _has_debug_info(prog)
    elapsed = time.monotonic() - start
    if has_debug and elapsed > 5:
        say("index", f"{elapsed:.0f}s to build drgn's DWARF index")
    if not has_debug:
        print("\nattached, but this kernel's debug info is not loaded: every "
              "view would fail.", file=sys.stderr)
        if not args.core:
            # Say what the cache actually holds, so the user can tell an
            # interrupted download apart from a fetch that never happened.
            partials = debuginfod.stale_partials(build_id, max_age=0)
            if partials:
                size = sum(p.stat().st_size for p in partials)
                print(f"the last download was interrupted: {debuginfod.human(size)} "
                      "of a partial file is in the cache. Run:", file=sys.stderr)
            else:
                print("nothing is cached: the fetch never completed. Run:",
                      file=sys.stderr)
            print("  kexplore --prefetch      (one uninterrupted download)\n"
                  "  kexplore --offline       (afterwards, no network at all)",
                  file=sys.stderr)
        return 1

    # Every "L<n>" in a field listing counts in these bytes, so resolve it once
    # here rather than assuming 64 in the row that renders it.
    from .core import arch, nav

    nav.set_cache_line(arch.cache_line_size(prog)[0])

    if args.check:
        return _check(prog)

    source = None
    if not args.core:
        # Probe once here so startup says what is available instead of the UI
        # discovering it later. Skipped for -c: the probe would describe the
        # host kernel, not the core.
        source = _probe_source()
        if source is not None:
            say("source", f"on demand via debuginfod, rooted at "
                          f"{source.source_prefix}")
        else:
            say("source", "unavailable for this build; struct documentation "
                          "and the 's' key are disabled")

    from .tui.app import Explorer

    # source is None when the probe found nothing fetchable for this build, and
    # the line above has just said the 's' key is disabled. Pass that through,
    # or the key stays on the footer and contradicts it.
    Explorer(prog, source, source_available=source is not None).run()
    return 0


def _probe_source(timeout: float = 5.0) -> KernelSource | None:
    """The KernelSource if source is fetchable for this build, else None.

    The probe shells out to debuginfod-find; a hung or slow server gets
    ``timeout`` seconds, then the answer is "unavailable". An abandoned
    probe thread is daemonized and dies with the process.
    """
    import threading

    from .core.source import KernelSource

    candidate = KernelSource()
    result: list[bool] = []

    def probe() -> None:
        result.append(candidate.available)

    thread = threading.Thread(target=probe, daemon=True)
    thread.start()
    thread.join(timeout)
    return candidate if result and result[0] else None


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

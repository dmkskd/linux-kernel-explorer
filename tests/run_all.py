"""Run the test scripts, and say which ones this machine can run.

Two kinds live here:

* **host tests** need nothing but Python. They cover the parts deliberately
  written to be checkable without a kernel -- graph layout, bpftrace output
  parsing -- and run on a laptop or in CI.
* **kernel tests** attach to the live kernel through drgn, so they need the VM
  and root. Skipped, loudly, when drgn cannot attach.

    python3 tests/run_all.py            # everything this machine can do
    python3 tests/run_all.py --host     # only the ones needing no kernel
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent

# The crawl goes last: it is the slowest and the most likely to be interrupted.
HOST = ["test_layout.py", "test_parse.py", "test_catalog.py"]
KERNEL = [
    "smoke.py",
    "test_views.py",
    "test_graph.py",
    "test_decoders.py",
    "test_process.py",
    "test_socket.py",
    "test_skb_page.py",
    "test_measure.py",
    "test_crawl.py",
]


def have_kernel() -> bool:
    """Whether drgn can attach here. Requires the VM, root, and the DWARF."""
    try:
        import drgn
    except ImportError:
        return False
    try:
        drgn.program_from_kernel()
    except Exception:  # noqa: BLE001 - not root, not Linux, no debug info
        return False
    return True


def run(name: str) -> bool:
    # Flushed, because the child writes straight to the same fd: without this
    # the header lands after the output it introduces whenever stdout is a pipe.
    print(f"\n=== {name} " + "=" * (60 - len(name)), flush=True)
    result = subprocess.run(
        [sys.executable, str(TESTS / name)],
        cwd=ROOT,
        env={"PYTHONPATH": str(ROOT), "PYTHONDONTWRITEBYTECODE": "1",
             **_inherited()},
    )
    return result.returncode == 0


def _inherited() -> dict[str, str]:
    import os

    # DEBUGINFOD_URLS and TERM matter to the kernel tests; PATH to all of them.
    return {
        key: value
        for key, value in os.environ.items()
        if key not in ("PYTHONPATH", "PYTHONDONTWRITEBYTECODE")
    }


def main() -> int:
    host_only = "--host" in sys.argv
    names = list(HOST)
    if not host_only:
        if have_kernel():
            names += KERNEL
        else:
            print("no live kernel here (needs the VM and root): "
                  "running host tests only", flush=True)
            print("  skipping  " + "  ".join(KERNEL), flush=True)

    failed = [name for name in names if not run(name)]

    print("\n" + "=" * 68, flush=True)
    print(f"{len(names) - len(failed)}/{len(names)} passed")
    for name in failed:
        print(f"  FAILED {name}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

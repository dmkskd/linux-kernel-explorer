"""Live process-tree filtering: descendants included, same-name outsiders excluded."""

from __future__ import annotations

import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from kexplore.core.probe import TRACE_FILTER, trace_command


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="trace-tree-") as directory:
        root = Path(directory)
        own, child, foreign = [root / name for name in ("own", "child", "foreign")]
        for path in (own, child, foreign):
            path.write_text("test\n")
        outsider = root / "outsider.py"
        outsider.write_text(
            "import time\nfrom pathlib import Path\n"
            f"p = Path({str(foreign)!r})\n"
            "while True:\n p.read_text()\n time.sleep(.01)\n"
        )
        worker = root / "worker.py"
        worker.write_text(
            "import subprocess, time\nfrom pathlib import Path\n"
            f"Path({str(own)!r}).read_text()\n"
            f"subprocess.run(['cat', {str(child)!r}], check=True)\n"
            "time.sleep(.3)\n"
        )
        unrelated = subprocess.Popen([sys.executable, str(outsider)])
        try:
            time.sleep(0.1)
            script = (
                f"kprobe:do_sys_openat2 /{TRACE_FILTER}/ "
                + "{ @seen[str(uptr(arg1))] = count(); }"
            )
            result = trace_command(
                script, f"{shlex.quote(sys.executable)} {shlex.quote(str(worker))}"
            )
            assert not result.error, result.error
            paths = {
                label
                for section in result.sections
                if section.name == "seen"
                for label, _value, _bar in section.rows
            }
            assert str(own) in paths, paths
            assert str(child) in paths, paths
            assert str(foreign) not in paths, paths
            print(
                "PASS: stopped launcher resumes; child of another name included; same-name outsider excluded",
                flush=True,
            )
            result = trace_command(
                script, f"cat {shlex.quote(str(own))} | cat {shlex.quote(str(child))}"
            )
            assert not result.error, result.error
            paths = {
                label
                for section in result.sections
                if section.name == "seen"
                for label, _value, _bar in section.rows
            }
            assert {str(own), str(child)} <= paths, paths
            print("PASS: both sides of a pipeline included", flush=True)
        finally:
            unrelated.terminate()
            unrelated.wait(timeout=5)


if __name__ == "__main__":
    main()

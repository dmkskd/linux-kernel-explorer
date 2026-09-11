"""Process cleanup when a shell exits before its pipeline members."""

from __future__ import annotations

import os
import shlex
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, call, patch

from kexplore.core.probe import _kill_group, _run_briefly, _start_stopped, trace_command


class CleanupTests(unittest.TestCase):
    def test_interrupt_ignoring_descendant_does_not_survive(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pidfile = root / "worker.pid"
            worker = root / "worker.py"
            worker.write_text(
                "import os, signal, time\n"
                "from pathlib import Path\n"
                "signal.signal(signal.SIGINT, signal.SIG_IGN)\n"
                f"Path({str(pidfile)!r}).write_text(str(os.getpid()))\n"
                "time.sleep(30)\n"
            )
            runner = root / "runner.sh"
            runner.write_text(
                "trap 'exit 0' INT\n"
                f"{shlex.quote(sys.executable)} {shlex.quote(str(worker))} &\nwait\n"
            )
            pid = None
            try:
                self.assertTrue(_run_briefly(str(runner), 1))
                pid = int(pidfile.read_text())
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    try:
                        os.kill(pid, 0)
                    except ProcessLookupError:
                        return
                    # Linux may retain a killed orphan as a zombie until init
                    # reaps it. It is no longer running in that state.
                    stat = Path(f"/proc/{pid}/stat")
                    try:
                        if stat.read_text().rsplit(")", 1)[1].split()[0] == "Z":
                            return
                    except FileNotFoundError:
                        pass
                    time.sleep(0.05)
                self.fail("interrupt-ignoring descendant survived cleanup")
            finally:
                if pid is not None:
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    @patch("kexplore.core.probe.os.killpg")
    @patch("kexplore.core.probe.subprocess.Popen")
    def test_shell_exits_on_interrupt_but_group_still_gets_killed(self, popen, killpg):
        child = popen.return_value
        child.pid = 12345
        child.wait.side_effect = [subprocess.TimeoutExpired("sh", 1), 0, 0]
        self.assertTrue(_run_briefly("runner.sh", 1))
        self.assertEqual(
            killpg.call_args_list,
            [call(12345, signal.SIGINT), call(12345, signal.SIGKILL)],
        )

    @patch("kexplore.core.probe.os.killpg")
    @patch("kexplore.core.probe.subprocess.Popen")
    def test_background_members_are_cleaned_after_normal_shell_exit(
        self, popen, killpg
    ):
        popen.return_value.pid = 12345
        self.assertFalse(_run_briefly("runner.sh", 1))
        killpg.assert_called_once_with(12345, signal.SIGKILL)

    def test_launcher_stops_before_executing_command(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "ran"
            runner = Path(directory) / "runner.sh"
            runner.write_text(f"touch {shlex.quote(str(marker))}\n")
            child = _start_stopped(str(runner))
            try:
                self.assertFalse(marker.exists())
                child.send_signal(signal.SIGCONT)
                child.wait(timeout=5)
                self.assertTrue(marker.exists())
            finally:
                _kill_group(child)

    @patch("kexplore.core.probe.tool_available", return_value=True)
    @patch("kexplore.core.probe._start_stopped")
    @patch("kexplore.core.probe._kill_group")
    @patch(
        "kexplore.core.probe.subprocess.Popen", side_effect=OSError("tracer missing")
    )
    def test_attach_failure_kills_stopped_launcher(self, popen, kill, start, available):
        child = start.return_value
        child.pid = 12345
        result = trace_command("probe", "cmd")
        self.assertIn("tracer missing", result.error)
        kill.assert_called_once_with(child)
        child.send_signal.assert_not_called()

    @patch("kexplore.core.probe.tool_available", return_value=True)
    @patch("kexplore.core.probe._start_stopped")
    @patch("kexplore.core.probe._kill_group")
    @patch("kexplore.core.probe.subprocess.Popen")
    def test_resume_only_after_attachment(self, popen, kill, start, available):
        child = start.return_value
        child.pid = 12345
        tracer = popen.return_value
        tracer.poll.return_value = None
        tracer.stderr = Mock()
        tracer.stderr.__iter__ = Mock(return_value=iter(["Attached 2 probes\n"]))
        with patch("kexplore.core.probe._wait_briefly", return_value=False):
            trace_command("probe", "cmd")
        child.send_signal.assert_called_once_with(signal.SIGCONT)
        self.assertIn("@trace_tasks[(uint64)12345]", popen.call_args.args[0][-1])
        kill.assert_called_once_with(child)
        tracer.send_signal.assert_called_once_with(signal.SIGINT)


if __name__ == "__main__":
    unittest.main()

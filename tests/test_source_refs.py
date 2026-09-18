"""Static source scans and keyed stack parsing, without a running kernel."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kexplore.core import debuginfod
from kexplore.core.probe import parse_keyed_stacks, parse_stacks
from kexplore.core.source import KernelSource
from kexplore.core.source_refs import field_references, function_body, parameter_calls


class SourceTests(unittest.TestCase):
    def test_debug_cache_rejects_malformed_and_mismatched_files(self):
        for notes in ("", "Build ID: deadbeef"):
            with self.subTest(notes=notes), tempfile.TemporaryDirectory() as directory:
                entry = Path(directory) / "abcd"
                entry.mkdir()
                (entry / "debuginfo").write_bytes(b"nonempty but invalid")
                with patch.object(debuginfod, "cache_path", return_value=Path(directory)), \
                     patch.object(debuginfod.subprocess, "run", return_value=
                                  subprocess.CompletedProcess([], 0, notes, "")):
                    self.assertFalse(debuginfod.is_cached("abcd"))
                self.assertFalse((entry / "debuginfo").exists())
                self.assertTrue((entry / "debuginfo.invalid").exists())

    def test_debug_cache_accepts_matching_build_id(self):
        with tempfile.TemporaryDirectory() as directory:
            entry = Path(directory) / "abcd"
            entry.mkdir()
            (entry / "debuginfo").write_bytes(b"debug image")
            with patch.object(debuginfod, "cache_path", return_value=Path(directory)), \
                 patch.object(debuginfod.subprocess, "run", return_value=
                              subprocess.CompletedProcess([], 0, "Build ID: abcd", "")):
                self.assertTrue(debuginfod.is_cached("abcd"))
            self.assertTrue((entry / "debuginfo").exists())

    def test_local_source_uses_installed_dwarf_without_network(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            header = root / "kernel/sched/sched.h"
            header.parent.mkdir(parents=True)
            header.write_text("/* Scheduling state. */\nstruct rq {\n int count; /* runnable */\n};\n")
            source = KernelSource(
                build_id="test", source_root=directory, source_prefix="/build/linux"
            )
            with (
                patch("kexplore.core.debuginfod.local_vmlinux", return_value=root / "vmlinux"),
                patch.object(source, "_find") as fetch,
                patch.object(source, "declaration", return_value=("/build/linux/kernel/sched/sched.h", 2)),
            ):
                self.assertTrue(source.available)
                self.assertEqual(source.debuginfo, str(root / "vmlinux"))
                doc = source.document("rq", frozenset({"count"}))
                self.assertEqual(doc.summary, "Scheduling state.")
                self.assertEqual(doc.members, {"count": "runnable"})
                self.assertEqual(doc.local_path, str(header.resolve()))
                fetch.assert_not_called()

    def test_local_source_rejects_escape_and_does_not_fetch_missing_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "source"
            root.mkdir()
            outside = Path(directory) / "outside.c"
            outside.write_text("outside")
            (root / "escape.c").symlink_to(outside)
            source = KernelSource(build_id="test", source_root=str(root))
            with patch.object(source, "_find") as fetch:
                for path in ("../outside.c", str(outside), "escape.c", "missing.c"):
                    self.assertIsNone(source.local_file(path))
                fetch.assert_not_called()

    def test_trace_source_timeout_reports_unavailable(self):
        source = KernelSource(build_id="test", source_timeout=5)
        with patch(
            "kexplore.core.source.subprocess.run",
            side_effect=subprocess.TimeoutExpired("debuginfod-find", 5),
        ) as run:
            self.assertIsNone(source._find("source", "test.c"))
            self.assertEqual(run.call_args.kwargs["timeout"], 5)

    def test_expired_source_deadline_starts_no_subprocess(self):
        source = KernelSource(build_id="test", deadline=1)
        with (
            patch("kexplore.core.source.time.monotonic", return_value=2),
            patch("kexplore.core.source.subprocess.run") as run,
        ):
            self.assertIsNone(source._find("source", "test.c"))
        run.assert_not_called()

    def test_body_ignores_comment_braces_and_preserves_nested_blocks(self):
        lines = [
            "void f(struct task *task) {",
            "/* } task->fake */",
            "if (flag) { use(task->flags); }",
            "}",
            "void other() { task->wrong; }",
        ]
        body = function_body(lines, 1)
        self.assertEqual(
            field_references(body, {"task": "struct task *"}),
            [("task.flags", "struct task *")],
        )

    def test_follow_only_unchanged_parameters(self):
        body = "first(task, nested(1, 2)); second(0, task); ops->indirect(task); third(local); cast((struct task *)task);"
        calls = parameter_calls(body, {"task": "struct task *"})
        self.assertEqual(calls, {"first": {0}, "second": {1}})

    def test_keyed_stacks_retain_distinct_interfaces(self):
        raw = "@read_stack[/, 1, stat,\nseq_read_iter+0\nvfs_read+204\n]: 2\n@entry_stack[kprobe:inet_diag_dump,\ninet_diag_dump+0\n]: 3\n@serves0[\ndo_task_stat+0\n]: 1\n"
        keyed = parse_keyed_stacks(raw)
        self.assertEqual(
            keyed["read_stack"][0],
            ("/, 1, stat", ["seq_read_iter+0", "vfs_read+204"], 2),
        )
        self.assertEqual(keyed["entry_stack"][0][0], "kprobe:inet_diag_dump")
        self.assertEqual(parse_stacks(raw), {"serves0": [(["do_task_stat+0"], 1)]})


if __name__ == "__main__":
    unittest.main()

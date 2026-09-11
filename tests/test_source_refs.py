"""Static source scans and keyed stack parsing, without a running kernel."""

from __future__ import annotations

import subprocess
import unittest
from unittest.mock import patch

from kexplore.core.probe import parse_keyed_stacks, parse_stacks
from kexplore.core.source import KernelSource
from kexplore.core.source_refs import field_references, function_body, parameter_calls


class SourceTests(unittest.TestCase):
    def test_trace_source_timeout_reports_unavailable(self):
        source = KernelSource(build_id="test", source_timeout=5)
        with patch(
            "kexplore.core.source.subprocess.run",
            side_effect=subprocess.TimeoutExpired("debuginfod-find", 5),
        ) as run:
            self.assertIsNone(source._find("source", "test.c"))
            self.assertEqual(run.call_args.kwargs["timeout"], 5)

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

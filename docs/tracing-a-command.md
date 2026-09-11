# Tracing a userspace command into the kernel

`u` replaces the type column with a userspace command. `t` runs that command
under bpftrace, shows the kernel interfaces it used, and connects their source
references to the selected structure field when there is evidence for a link.
The result is a normal frame: `s` on a stack row opens its source location.

The trace follows the command's launcher and descendants. It retains each
observed interface instead of choosing the busiest file or netlink handler.
Source references and catalog associations remain static evidence; they do not
prove that a particular field supplied a column of the command's output.

## The four stages

| Stage | What it shows | Evidence |
| ----- | ------------- | -------- |
| 1. command | Runnable command, originating row, process scope | User selection |
| 2. files opened | Open calls, grouped by path | Discovery run |
| 3. kernel interfaces | A section per procfs path or non-file handler, with its own counts and stack | Discovery, then a serving-function pass when possible |
| 4. field references | Direct parameter references, one level of helpers, catalog fields | Static source analysis and catalog |

A selected-field row before the interface stacks states whether that field appears
in direct source, a helper, or the catalog. If no association is found, it says
`not established`. Even a match does not establish a runtime field access.

## Process attribution and execution

`core/probe.py` starts a shell in a new process group. Before executing the
command script, the shell stops itself with SIGSTOP. The parent waits for the
stopped state, so it has a PID before attaching any probes.

The bpftrace program seeds a thread-ID membership map with that PID and follows
process lifecycle events:

```text
BEGIN                 add the stopped launcher's ID
sched_process_fork    add the child ID when the parent is tracked
sched_process_exec    transfer membership from old_pid to the execing thread
sched_process_exit    remove the exiting ID
```

Every measurement probe uses `@trace_tasks[tid]`. This includes forked children,
threads, and both sides of a pipeline even when their names differ. An unrelated
process with the same `comm` is excluded, and an exited thread's ID is removed
before it can be reused. This does not trace work delegated to unrelated kernel
worker threads or services outside the command's process tree.

The launcher resumes with SIGCONT only after bpftrace reports `Attached`.
`BEGIN` is not the readiness signal: it runs before the other probes attach,
as specified in the [bpftrace language documentation](https://bpftrace.org/docs/release_023/language).
No `comm` predicate or `cpid` builtin is used.

Each run is capped at five seconds. The shell and its process group are cleaned
up on completion, timeout, and attachment failure. Cleanup retains the group ID
and sends SIGKILL even if the shell has already exited after SIGINT, so a
pipeline member that ignores SIGINT is stopped. Descendants that deliberately
leave the process group are outside that cleanup guarantee. The tracer is also
stopped when command execution fails.

The command runs through a shell script so quoting and pipelines retain their
shell meaning. Tracing includes that shell's startup activity and its opens of
the script and shared libraries. There is no per-program shortcut for `ps`.

## Discovery and per-interface stacks

Discovery always runs, including when the command names a catalogued file.
The probes are:

```text
do_sys_openat2        open calls, including relative filenames
vfs_read             procfs reads, including non-seq files
seq_read_iter        procfs seq_file reads and their show pointers
vfs_readlink         symlink reads
iterate_dir          directory iteration
vfs_statx            stat calls
rtnetlink_rcv_msg    netlink requests
inet_diag_dump       socket diagnostic dumps
__netlink_dump_start netlink dumps
```

`do_sys_openat2` is used because `sys_enter_openat` was listed but did not fire
on the development kernel. The netlink set includes both request and dump
handlers: a request naming one device need not start a dump. These are probe
choices for the interfaces being inspected, not a claim that every kernel has
the same attachable functions.

Procfs read probes check the filesystem magic before interpreting dentry names.
They record the final three dentry components with their stacks. The seq_file
probe also records its show and private pointers with the same components.
A show pointer from a different file cannot supply the selected file's leaf.

Known dentry patterns become paths such as `/proc/<pid>/stat` or
`/proc/net/softnet_stat`. Deeper paths retain an explicit `/proc/…/` suffix;
that is not a reconstructed full path. PID paths are grouped, and identical
stacks from grouped paths are combined. A read can appear at both `vfs_read`
and `seq_read_iter`; the displayed discovery count uses the larger grouped
count, not their sum. Counts include read calls that return EOF.

Each observed procfs path becomes an interface section. Every observed non-file
handler also gets a section. Netlink sections are listed first, but they do not
discard file sections. Non-file counts are handler calls, not necessarily
separate logical requests: one request can pass through multiple handlers.

For a known procfs path, `catalog/procfs.py` supplies its serving function.
Otherwise, discovery uses the function pointer from that path's seq_file.
For `proc_single_show`, the private pointer is interpreted as the inode and
drgn reads `PROC_I(inode)->op.proc_show`. This is a live-memory lookup after the
run; if the inode has disappeared, a deeper function may not resolve.

All resolvable, unambiguous file-serving functions are probed together in a
second run. A per-thread current-file map is set at `vfs_read` and
`seq_read_iter`, then cleared on return. A leaf probe records a stack only when
that file context matches its interface. PID components are generalized so the
new command's `/proc/self` path can match its discovery counterpart.

The file map stores the innermost observed read context. It does not reconstruct
arbitrarily nested reads. The two passes are separate executions, so a file or
function observed in discovery may be absent from the second run. Discovery
stacks remain visible when no serving stack is obtained or the second pass fails.

A catalog candidate outside procfs, such as a named sysfs attribute, is labelled
as a candidate whose path was not observed in discovery. Its function can still
be probed, but those calls are labelled as uncorrelated with the candidate path.

Each interface reports its discovery count and the count of the displayed
stack separately. If there are several distinct stacks, it says how many and
shows the most frequent. This is not the total number of calls to the function.

Discovery records stacks in a map keyed by the three dentry components
together with the stack, so a grouped PID path starts with one entry per PID
whose file was read. `_merge_stacks` sums the entries whose frame sequences are
identical before the row is built, so the reported number of distinct stacks
counts frame sequences and not PIDs. Frames retain their instruction offsets,
so two sequences differing only in an offset are counted separately.

## What an interface row is keyed by

Stage 3 rows are not syscalls, and the count of rows is not a count of
syscalls. A row is keyed one of two ways:

| Key | Row label | What the count measures |
| --- | --------- | ----------------------- |
| Kernel function | `vfs_statx`, `iterate_dir`, a netlink handler | Calls to that function |
| Procfs path | `/proc/<pid>/cmdline` | Reads of files reconstructing to that path |

Neither key is a syscall, and the relation between the two is many-to-many.
One probed function serves several syscalls: `vfs_statx` is reached from more
than one stat call, and `iterate_dir` from the getdents family. Every
path-keyed row, on the other hand, is reached through `read`, so those rows are
all the same syscall as each other and are separated by the file instead.

The syscall is therefore not recoverable from the row label. It is recoverable
from the displayed stack when the stack contains an `__arm64_sys_*` frame, and
only for the calls on that stack. A stack that reaches the probed function
directly from `invoke_syscall`, with no syscall frame between them, does not
name the call that entered the kernel.

## Ambiguous symbols and source locations

A static function name can occur in multiple translation units. `m_show` is an
example. The measured address identifies the discovered function, but a kprobe
by that name cannot identify which definition to attach to.

Such a function stays separate from its file's discovery read stack and is
labelled `not probed`. It is not appended as a measured stack frame. Its own
source location is resolved from the recorded address. A printed stack frame
with an ambiguous symbol name has no guessed source link.

Other stack frames retain their instruction offsets. `vfs_read+204` resolves
at the runtime symbol address plus 204, with the KASLR relocation subtracted
before calling `addr2line`. The link is to that instruction's source location,
not just the function entry. Missing debugging information leaves the location
unavailable.

## Selected-field evidence

The TUI passes the parent structure tag and field name, for example
`task_struct.flags`, through the deferred trace frame. Stage 1 retains the
original row name, and the selected-field row summarizes matches across all
interfaces:

| Evidence | Meaning |
| -------- | ------- |
| Direct source reference | The function's body refers to this field through a typed parameter |
| Source reference via a helper | A direct helper receiving a structure parameter refers to the field |
| Catalog association | A catalog command associates the field with this path |
| Not established | None of these sources establishes an association |

Source matches also say whether the serving function itself was probed. A
helper is not probed merely because its source was scanned. A catalog candidate
is not promoted to a measured field access.

For `on_cpu`, the catalog currently offers `ps -o psr= -p <pid>`. The trace
retains both stat and status interfaces when they are observed, but does not
claim that the processor column proves a read of `task_struct.on_cpu`. That
field-to-output relationship still needs independent validation.

## One level of source helpers

The scan takes a function's source body and its parameter names and types from
DWARF. It strips comments and string/character literals, balances braces, and
reports expressions such as `task->flags` as `task_struct.flags`.

It then follows direct calls where an argument is an unchanged structure
parameter, for example `task_utime(task)`. Argument positions map to the helper's
DWARF parameter names. Only those helper parameters contribute field references.
Nested argument parentheses are handled; a local variable, cast, member
expression, or indirect function-pointer call is not treated as an unchanged
parameter.

The traversal stops after one helper level and considers at most twelve helper
names per function, in name order. It reports unavailable helpers and the number
of additional calls omitted by that limit. Inline functions without a standalone
symbol, duplicate symbols, or missing source/DWARF are unavailable rather than
silently counted as empty analyses.

Field rows distinguish `source`, `source via <helper>; helper not probed`, and
`catalog`. Catalog fields already found in source are not repeated. References
include writes and conditional source branches regardless of whether those
branches executed. This is not a C data-flow analysis, preprocessing pass, or
measurement of field reads. Missing source is distinguished from an available
scan that finds no references.

## Examples and validation

The following scenarios are covered by regression tests:

| Command or scenario | Expected evidence |
| ------------------- | ----------------- |
| `cat /proc/loadavg /proc/uptime` | Separate paths, functions, and file-correlated serving stacks |
| `ss -tanp` | Netlink and procfs sections retained together |
| `ps -o psr= -p 1` | Stat and status retained; selected `on_cpu` relationship not asserted |
| `findmnt` | Ambiguous `m_show` reported separately from the measured read stack |
| `vmstat -n 1` | Observation interval capped and process group cleaned up |
| Python command with a differently named child | Child opens included |
| Unrelated Python reader running concurrently | Its opens excluded |
| Two-sided `cat` pipeline | Opens from both sides included |
| Descendant ignoring SIGINT | SIGKILL cleanup still stops it after shell exit |

These test live behavior on the configured kernel; interface counts vary with
the command, process set, and kernel build. `tc` is still not covered by a live
command-specific regression.

## Cost, scope, and code

A trace executes the command once for discovery and, when file leaf probes can
attach, once more for all those serving functions together. It does not run the
command separately for each interface. Netlink-only traces need no second pass.
The view is built in a worker while a placeholder remains on screen.

Source resolution calls `addr2line` against the cached kernel debugging file.
Locations are cached per address for the session. Helper scans add source and
DWARF lookups. Source downloads for this trace view have a five-second timeout
per request; unavailable source is labelled explicitly. This does not impose
a five-second limit on the whole view build. Trace results themselves are not
cached.

The probe set covers the listed interfaces, not every kernel API. Open counts
are calls, including failures, and relative filenames do not retain their
directory descriptor. Procfs path reconstruction retains only three dentry
components and uses bpftrace's string limits; suffixes are not globally unique.
The selected field's value is not tracked through userspace parsing.

```text
core/probe.py              stopped launcher, process tracking, cleanup, stack parsing
core/source_refs.py        source body extraction, references, direct helper calls
catalog/procfs.py           path-to-function and field associations
operations/command_trace.py discovery, per-interface stacks, field evidence
view/frames.py             deferred trace frame and selected-field context
tui/app.py                 t key and originating structure field
tests/test_probe_cleanup.py stopped launcher and cleanup regressions
tests/test_source_refs.py   source scans and keyed stack parsing
tests/test_trace_attribution.py live process-tree filtering
tests/test_command_trace.py UI, interfaces, source links, and selected-field evidence
```

Run `python3 tests/run_all.py --host` for host checks. Run `./run.sh --check`
and `./run.sh --test` for catalog validation and the full suite in the configured
live-kernel backend.

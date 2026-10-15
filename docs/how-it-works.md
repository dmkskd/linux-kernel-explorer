# How it works

kexplore is a front end over five tools:

| tool        | responsibility                                          |
| ----------- | ------------------------------------------------------- |
| drgn        | reads kernel memory and resolves DWARF types             |
| debuginfod  | fetches the vmlinux debuginfo and the matching source    |
| pahole      | the file and line where a struct is declared             |
| addr2line   | the file and line a kernel address belongs to            |
| bpftrace    | what happens over an interval, rather than at an instant |

## [drgn](https://drgn.readthedocs.io/)

`/proc/kcore` is a privileged, ELF-formatted view of the running kernel's
virtual address space. drgn reads it to retrieve live kernel objects, then uses
DWARF type information to give those bytes names, types, and fields:

```python
prog = drgn.program_from_kernel()
rq = per_cpu(prog["runqueues"], 3)
rq.curr.comm.string_()        # b'python3'
```

Types come from the kernel's DWARF, so `rq.curr` is a real `struct task_struct *`
and members are resolved by name. The explorer runs in the same process as drgn,
so a row in the UI holds an actual `drgn.Object` rather than a copy. That is why
`:` can drop into a REPL with the object under the cursor already bound.

drgn also ships Linux-specific helpers (`for_each_task`, `for_each_vma`,
`SOCKET_I`, `rbtree_inorder_for_each_entry`), which the curated maps use instead
of re-implementing list and tree walks.

Reading `/proc/kcore` requires root, which is why the explorer runs as root in
the VM.

## [debuginfod](https://sourceware.org/elfutils/Debuginfod.html)

The recommended Fedora lab uses debuginfod instead of requiring a local
debug package. Fedora also supports manual kernel debug-package installation.
`DEBUGINFOD_URLS` points at Fedora's server, which serves both:

- **DWARF**, fetched automatically by drgn on first type lookup. Without this
  drgn cannot resolve `struct task_struct` at all.
- **source files**, fetched with `debuginfod-find source <build-id> <path>`.

Both are keyed by the kernel's GNU build-id, read from `/sys/kernel/notes`, so
what you get matches the running kernel exactly rather than approximately.

### The cache

Downloads land in the cache as `<build-id>/debuginfo`. The location is
`$DEBUGINFOD_CACHE_PATH`, else `$XDG_CACHE_HOME/debuginfod_client`, else
`~/.cache/debuginfod_client`, so `/root/.cache/...` under `sudo`.

The client writes to `debuginfo.XXXXXX` first and renames only on a complete
transfer. There is no resume. A ~700MB vmlinux therefore has to arrive in one
uninterrupted transfer. Quitting the explorer mid-download loses all of it and
leaves a partial file that the next run ignores. `kexplore --prefetch` does that
download outside the UI, where an interruption costs only the download itself.
It also deletes abandoned partials: those untouched for four hours, so a
transfer still running elsewhere is left alone.

Two client behaviours:

- The cache is only consulted when at least one URL is configured. Emptying
  `DEBUGINFOD_URLS` gives `ENOSYS`, not an offline mode. `--offline` therefore
  sets the URL to a closed port: cache hits are served from disk, misses fail
  immediately with `ECONNREFUSED`.
- Retention is governed by `max_unused_age_s` (a week), `cache_clean_interval_s`
  and `cache_miss_s` (ten minutes), plain files inside the cache directory.
  Startup raises the first two to a year, so the vmlinux is not pruned between
  sessions. It raises the negative cache to a day. That matters because the
  source-prefix probe tries up to three candidate source roots and only one can
  match; without it the misses go back to the server on every run.

## [pahole](https://github.com/acmel/dwarves)

drgn 0.2.0 does not expose `DW_AT_decl_file` / `DW_AT_decl_line` on a `Type`, so
there is no way to ask it which file a struct came from. `pahole` reads the same
DWARF and reports it:

```
$ pahole -C rq --show_decl_info vmlinux
/* <16cbf06> kernel/sched/sched.h:1131 */
```

With the file and line, the source is fetched via debuginfod and a small scanner
walks the struct body attaching comments to members. The scanner is loose and
validated against the member names drgn already knows, so anything it misparses
is discarded rather than shown wrongly.

That is where the struct summaries and per-field documentation come from: the
kernel's own comments, for this build.

## [addr2line](https://sourceware.org/binutils/docs/binutils/addr2line.html)

Maps an address to the source line that produced it, using the DWARF line
table in the same debuginfo. Walkthrough steps name functions, and `s` opens
the source of the one under the cursor:

```
$ addr2line -f -e vmlinux 0xffff8000817202f0
schedule
kernel/sched/core.c:7279
```

The debuginfo holds link-time addresses while the running kernel is relocated
by KASLR, so a runtime address returns `??:0` until the offset is subtracted:

```
KASLR offset = runtime _stext - link-time _stext      (link-time from nm)
addr2line -e vmlinux $(( runtime_addr - offset ))
```

## [bpftrace](https://bpftrace.org/)

drgn reads state. It cannot answer "how long did tasks wait for a CPU in the
last two seconds", because that is a property of events over time. The `measure`
groups run a short bpftrace program in the foreground and parse its output.

Two rules the measurements follow:

- **Name the endpoints.** A map is called `us_from_sched_wakeup_to_oncpu`, not
  `runq_latency`, so the number is checkable.
- **Use `fentry`/`fexit`, not `kprobe`/`kretprobe`.** Measured on the same
  function in the same run, kretprobe added 2-4us of its own overhead while
  fentry cost roughly 100ns. At these scales the instrument would otherwise
  dominate.

Nothing runs in the background and nothing accumulates between runs.

## Other kernels

The browser needs matching DWARF, either in a standard local debug-package
location or in the debuginfod cache. Kernel source is optional. An installed
local debug image is also reused for source declaration and address lookups.
Use `--source-root` for an extracted matching source tree and `--source-prefix`
when DWARF records a different build-time root. These options belong to
kexplore; drgn's separate `-s` option is not a kexplore option.

Without source, structure browsing and curated explanations still work;
source-derived comments and code links are unavailable. A supplied source
revision is not automatically verified. See [installation](installation.md).

## Tracing a userspace command into the kernel

`u` replaces the type column with a userspace command. `t` runs that command
under bpftrace, shows the kernel interfaces it used, and connects their source
references to the selected structure field when there is evidence for a link.
The result is a normal frame: `s` on a stack row opens its source location.

The trace follows the command's launcher and descendants. It retains each
observed interface instead of choosing the busiest file or netlink handler.
Source references and catalog associations remain static evidence; they do not
prove that a particular field supplied a column of the command's output.

### The four stages

| Stage | What it shows | Evidence |
| ----- | ------------- | -------- |
| 1. command | Runnable command, originating row, process scope | User selection |
| 2. files opened | Open calls, grouped by path | Discovery run |
| 3. kernel interfaces | A section per procfs path or non-file handler, with its own counts and stack | Discovery, then a serving-function pass when possible |
| 4. field references | Direct parameter references, one level of helpers, catalog fields | Static source analysis and catalog |

A selected-field row before the interface stacks states whether that field appears
in direct source, a helper, or the catalog. If no association is found, it says
`not established`. Even a match does not establish a runtime field access.

### Process attribution and execution

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

### Discovery and per-interface stacks

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

### What an interface row is keyed by

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

### Ambiguous symbols and source locations

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

### Selected-field evidence

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

### One level of source helpers

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

### Examples and validation

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

### Cost, scope, and code

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

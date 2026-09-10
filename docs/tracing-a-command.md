# Tracing a userspace command into the kernel

`u` replaces the type column with the command that reads the same value without
this tool: `on_cpu` becomes `ps -o psr= -p 1`. `t` runs that command and reports
the kernel code that served it.

This document describes the mechanism, states which parts are measured and which
are read from a table, and lists the constraints on the implementation.

Measurements below come from one 4-vCPU lima VM running Fedora 44, kernel 7.1.8
on aarch64. The commands are general; the counts are not.

## How it works

`t` on a row carrying a command performs six steps.

1. The command runs once, with bpftrace attached beforehand. It runs in its own
   process group and is killed after a few seconds if it has not exited, so
   `vmstat -n 1` is traced over an interval rather than waited on.

2. Seven probes record which kernel interface the command used: file read,
   symlink read, directory listing, stat, and three netlink handlers. For a file
   read, the probe also records the dentry, which supplies the path when the
   command does not name one. This is how `ps` is found to read
   `/proc/<pid>/stat`.

3. The serving function is identified. For a file it comes from the seq_file the
   kernel holds on the open file; for netlink it is the handler that fired.
   `catalog/procfs.py` maps a dozen `/proc` paths to their functions, which lets
   those commands skip steps 1 and 2.

4. The command runs a second time with a probe on that function, recording the
   kernel stack that reaches it.

5. Each stack frame is resolved to a source location through the debuginfo the
   structure browser already uses: `vfs_read+204  fs/read_write.c:555`.

6. The serving function's source is scanned for the struct fields it reads.
   Parameter names and types come from DWARF, so `task->flags` in the body is
   reported as `task_struct.flags`.

Steps 1 to 5 are measured per run, with no per-command configuration. Step 6
combines a source scan with the catalog's field list, and each row states which
of the two it came from.

## The four stages

| stage | source | measured |
| ----- | ------ | -------- |
| 1. command | the string in the row | n/a |
| 2. files opened | `kprobe:do_sys_openat2`, filtered by comm | yes |
| 3. kernel entry point | discovery, then `kprobe:<leaf>` + `kstack(12)` | yes |
| 4. what it reads | the function's source, and the catalog | in part |

The discovery pass probes each interface the catalog's commands use:

```
seq_read_iter        a file read: ps, grep, awk, cat
vfs_readlink         readlink /proc/<pid>/exe, ls -l on a symlink
iterate_dir          ls of a directory
vfs_statx            stat, and ls -l per entry
rtnetlink_rcv_msg    ip
inet_diag_dump       ss
__netlink_dump_start any netlink dump
```

`catalog/procfs.py` is an optimisation rather than a requirement. A file with no
entry there resolves its serving function from the seq_file: `cat /proc/loadavg`
reaches `loadavg_proc_show` without a table entry.

## A worked example

The row is `on_cpu` in a `task_struct`, showing `ps -o psr= -p 1`.

### Stage 2: files opened

```sh
sudo bpftrace -e 'kprobe:do_sys_openat2 /comm == "ps"/ {
    @opens[str(uptr(arg1))] = count(); }' -c '/bin/sh /tmp/cmd.sh'
```
```
@opens[/proc/1]: 1
@opens[/proc/self]: 1
@opens[/proc/self/status]: 2
```

`ps` opens the task directory, then opens files inside it by name against that
directory descriptor. Those are recorded as a bare `stat`, because the probe
does not record the descriptor. Rows opened relative to a descriptor are
marked.

### Stage 3, part one: the file read

The path is not on the command line. The dentry at read time contains it:

```sh
sudo bpftrace -e 'kprobe:seq_read_iter /comm == "ps"/ {
    $d = ((struct kiocb *)arg0)->ki_filp->f_path.dentry;
    @read[str($d->d_parent->d_name.name), str($d->d_name.name)] = count(); }' \
  -c '/bin/sh /tmp/cmd.sh'
```
```
@read[1, stat]: 1
@read[1, status]: 3
@read[cpu, possible]: 1
```

A numeric parent denotes `/proc/<pid>/`. `path_from_dentry` reconstructs the
path, and `SERVED_BY` maps it to a function. This step is skipped when the
command names its file, as `grep VmPTE /proc/1/status` does.

### Stage 3, part two: the stack

```sh
sudo bpftrace -e 'kprobe:do_task_stat /comm == "ps"/ {
    @[kstack(12)] = count(); }' -c '/bin/sh /tmp/cmd.sh'
```
```
do_task_stat+0
proc_single_show+100
seq_read_iter+292
seq_read+236
vfs_read+204
ksys_read+108
__arm64_sys_read+32
```

The call path runs from the bottom entry upwards: syscall, VFS, seq_file, proc,
the formatting function. Frames are resolved with `nm` and `addr2line` against
the cached debuginfo, with the KASLR offset subtracted, as `core/source.py` does
for walkthrough steps.

```
do_task_stat -> fs/proc/array.c:468
```

### Stage 4: fields read

Two lists, each labelled with its source.

The first is scanned from the function's body, with parameter types taken from
DWARF:

```
task_struct.blocked   task_struct.exit_code   task_struct.exit_signal
task_struct.flags     task_struct.maj_flt     task_struct.min_flt
task_struct.pending   task_struct.policy      task_struct.rt_priority
task_struct.signal    task_struct.start_boottime
```

The second is the reverse lookup in `FIELD_COMMANDS`, restricted to entries the
scan did not find: `gtime`, `prio`, `stime`, `utime`. Those four are read
through helpers. `utime` is obtained via `task_utime()` and never appears as
`task->utime` in the function's text, so a scan of that text cannot report it.
The stage is labelled incomplete for this reason.

## Sources for stage 4

The field numbering in `FIELD_COMMANDS` predates any check against the kernel.
The source confirms it:

```c
seq_put_decimal_ll(m, " ", tty_pgrp);     /* field 8  */
seq_put_decimal_ull(m, " ", task->flags); /* field 9  */
seq_put_decimal_ull(m, " ", min_flt);     /* field 10 */
seq_put_decimal_ull(m, " ", cmin_flt);    /* field 11 */
```

Field 9 is `task->flags`, printed unmasked, and `maj_flt` is field 12 because
`cmin_flt` occupies 11.

Two derivations were considered. The first, the function's DWARF signature,
names the structures passed in:

```
do_task_stat      (struct seq_file *, struct pid_namespace *, struct pid *,
                   struct task_struct *, int)
proc_pid_status   (struct seq_file *, struct pid_namespace *, struct pid *,
                   struct task_struct *)
```

It yields nothing for seq_file show functions, which take an iterator cursor:

```
show_map          (struct seq_file *, void *)
meminfo_proc_show (struct seq_file *, void *)
show_stat         (struct seq_file *, void *)
```

The second, a scan of the function body, is what stage 4 implements. It depends
on the signature for types: `task` is treated as a `struct task_struct *`
because DWARF records that type, not because of the identifier.

The scan reports only fields reached through the function's parameters. A local
variable does not identify what the caller supplied, so `vfs_statx`, which
dereferences a local `struct path`, reports none.

## Constraints

**`tracepoint:syscalls:sys_enter_openat` does not fire on this kernel.**
`bpftrace -l` lists it and it counts nothing, including for `cat`, while
`sys_enter_read` counts normally in the same run. `kprobe:do_sys_openat2` is
used instead. A design based on syscall tracepoints would report that `ps` opens
no files.

**`bpftrace -c` is unsuitable, for two reasons.** It splits its argument and
execs the result without a shell, so `awk '{...}' /proc/1/stat` reaches awk with
its quotes as literal arguments. It also waits for the command to exit, which
`vmstat -n 1` never does. The command is therefore started separately, after
bpftrace prints `Attached` on stderr, and killed after a few seconds.

**Killing the child leaves the rest of a pipeline running.**
`timeout -s INT 3 sh -c 'vmstat -n 1 | awk …'` returns 124 on time and leaves
`vmstat` running, because the signal reaches the shell rather than the processes
it started. The command runs in its own process group, and the group is killed.

**A kprobe records callers, not callees.** A probe on `vfs_read` yields the
syscall path above it and nothing about what it calls, so the serving function
must be known or discovered before the stack is taken.

**A `single_open` file's `->op->show` is always `proc_single_show`.** Reading
the seq_file's show pointer therefore stops one frame above the specific
function. `ksym()` on that pointer also crashes bpftrace 0.24.2 with SIGTRAP;
the raw address is recorded instead and resolved by drgn.

**A static function's name need not be unique.** `findmnt` reads
`/proc/self/mountinfo`, served by `m_show`, and kallsyms holds two symbols of
that name:

```
ffffd7019c719628 t m_show
ffffd7019caeb0f8 t m_show
ERROR: Unable to attach probe: kprobe:m_show.
```

The address recorded from the seq_file identifies which one ran. The stack is
taken at the caller, `seq_read_iter`, the function is appended to it and marked
as unprobed, and its source location is resolved from the address rather than
the name.

**`/proc/net` is a symlink to `/proc/self/net`.** A file below it has a pid
directory as its grandparent in the dentry chain, which `path_from_dentry`
accounts for.

## Cost

One traced run when the file is named in the command, two when it must be
discovered. `ps -e` completes in about two seconds. The frame is built in a
worker with a placeholder on screen, as measurements are. Resolving twelve stack
frames requires twelve `addr2line` calls against a 700MB vmlinux, cached per
address.

## Code

```
catalog/procfs.py      the path-to-function map, path_from_dentry, fields_from
core/probe.py          trace_command, parse_stacks
operations/            command_trace.py: the four stages and the discovery pass
view/frames.py         command_trace_frame and command_trace_plan (deferred)
tui/app.py             the t key, and what counts as a command under the cursor
tests/                 test_command_trace.py: the key, the stages, s on a row
```

## Resolved questions

Each of these was open when this document was first written and is now
implemented.

**Netlink.** `ss` and `ip` read no files. They divide into two cases: naming a
device turns a dump into a single request, so `__netlink_dump_start` alone would
report that `ip -s link show eth0` does nothing.

```
ss -tanH               __netlink_dump_start 2   inet_diag_dump 4
ip link                __netlink_dump_start 1
ip -s link show eth0   __netlink_dump_start 0   rtnetlink_rcv_msg 2
```

The discovery pass probes all three and takes the most frequent, giving
`inet_diag_dump` for `ss` and `rtnetlink_rcv_msg` for `ip`. `tc` is not
installed on this VM and remains untested.

**Interfaces other than reads.** One probe each:

```
readlink /proc/self/ns/net   vfs_readlink 1
ls -l /proc/1/fd             iterate_dir  2
stat -L /proc/1/fd/0         vfs_statx    4
```

No lower point common to all of them was looked for: seven probes attach in
well under a second.

**The serving function without the table.** A `single_open` file leaves the
inode in `m->private`:

```c
static int proc_single_show(struct seq_file *m, void *v)
{
	struct inode *inode = m->private;
	...
	ret = PROC_I(inode)->op.proc_show(m, ns, pid, task);
```

The probe records `m->op->show` and `m->private`. When the first is
`proc_single_show`, the second is unwrapped in drgn:

```
container_of(inode, "struct proc_inode", "vfs_inode").op.proc_show
  -> 0xffffd7019cba9898  proc_tgid_stat
```

A file with its own iterator requires no unwrapping. `SERVED_BY` is now an
optimisation rather than a prerequisite.

**Stage 4 from source.** Implemented, paired with the catalog list, with only
the entries the scan missed shown from the catalog.

**Several files read by one command.** Partly resolved. The file named by the
command is traced, other files are listed with their counts, and a netlink
request outranks both. Ranking by read count alone selects the wrong target in
two measured cases:

* `grep VmPin /proc/1/status` reads its own `/proc/self/maps` twice during
  startup and the named file once, which selected `/proc/<pid>/maps` and
  `show_map`. A path named in the command now takes precedence.
* `ss -tanp` opens `/proc/<pid>/fd/` and reads `/proc/<pid>/stat` 35 times to
  attribute sockets to processes, which selected the file path over the netlink
  dump. A netlink handler now takes precedence, and the reads are reported in
  the evidence line.

Tracing more than one interface per run would require a stack per interface and
a frame that can present several.

## Open questions

**Helper calls are not followed.**

The scan reads the serving function's body only. `do_task_stat` gets `utime`
from `task_utime()` and the task state from `get_task_state()`, so neither
appears.

Following one level of calls would find them. That needs a rule for which calls
are worth following. Not implemented.

**Nothing caches a trace result.**

A trace costs one or two runs of the command, plus one `addr2line` call per
stack frame. The `addr2line` results are cached per address for the session, in
`KernelSource.function_location`. The trace itself is not cached.

Whether it should be is unclear. The result depends on the command, on the
kernel, and on what else was running at the time.

**Process filtering is by name, not by pid.**

Probes match on `comm`, the process name in `task_struct.comm`. The name used is
the first word of the command, cut to 15 characters.

Three effects follow:

* `ss -tanmH | grep -o …` is filtered on `ss`. Nothing `grep` does is recorded.
* An unrelated `ss` running at the same time is recorded as if it were the
  traced one.
* A command that does its work in a child of another name records nothing.

A pid filter would be exact. It is not available: a bpftrace script is fixed
before it runs, and the command has no pid until after the probes are attached.
bpftrace's `cpid` builtin reports 0 on this build.

The command could be started stopped, which gives it a pid before bpftrace
starts, then continued once the probes are attached. Not implemented.

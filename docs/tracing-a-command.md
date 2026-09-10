# Tracing a userspace command into the kernel

`u` turns the type column into the command that reads the same value on a box
without this tool: `on_cpu` becomes `ps -o psr= -p 1`. That is where most
explanations stop, and it hides the part worth seeing. `ps` is not asking the
kernel for a process list. It opens a few hundred files, and every read runs a
kernel function that formats a `task_struct`.

`t` runs the command under the cursor and reports what the kernel did with it.
The two keys are a pair. `u` is what you would type, `t` is what happens when
you do.

## How it works

Press `t` on a row showing a command, and this happens:

1. **The command is run**, once, with bpftrace already attached and watching.
   It runs in its own process group and is killed after a few seconds if it has
   not finished, so `vmstat -n 1` is traced for a slice instead of hanging.

2. **Probes record which door into the kernel it used.** There are seven, one
   per interface the catalog's commands actually use: reading a file, reading a
   link, listing a directory, stat, and three netlink handlers. Whichever fires
   is the answer. For a file read the probe also records the dentry, so the
   path is known even when the command never named it, which is how `ps` is
   found to be reading `/proc/<pid>/stat`.

3. **The function that serves it is identified.** For a file this is the show
   function the kernel is holding on the open file; for netlink it is the
   handler that fired. A dozen `/proc` paths are also written down in
   `catalog/procfs.py`, which lets those skip step 1 and 2 entirely, but the
   table is a shortcut and not a requirement.

4. **The command is run a second time**, with a probe on that function, to
   record the kernel stack that reaches it. That stack is the answer to "what
   runs when I type this": syscall, VFS, filesystem, the function itself.

5. **Every frame is turned into a source location**, through the same debuginfo
   the structure browser uses, so each line of the stack reads
   `vfs_read+204  fs/read_write.c:555`.

6. **The leaf's own source is scanned** for the struct fields it reads, using
   the parameter names and types from DWARF so `task->flags` is reported as
   `task_struct.flags`. The catalog's own list of fields for that file is shown
   beside it, and the entries the scan missed are exactly the ones the function
   reads through a helper.

Steps 1 to 5 are measured on the machine in front of you, every time. Nothing
about them is written down per command. Step 6 is half measured and half table,
and the screen says which row is which.

The rest of this document is the detail: a worked example, where each list
comes from, the traps found while building it, and what is still open.

Everything below was produced on one 4-vCPU lima VM running Fedora 44, kernel
7.1.8 on aarch64. The commands transfer; the counts do not.

## The four stages

| stage | how it is obtained | measured |
| ----- | ------------------ | -------- |
| 1. command | the string already in the row | n/a |
| 2. files opened | `kprobe:do_sys_openat2`, filtered by comm | yes |
| 3. kernel entry point | discovery, then `kprobe:<leaf>` + `kstack(12)` | yes |
| 4. what it reads | the leaf's source, and the catalog beside it | in part |

The discovery pass probes every interface the catalog's commands use, and the
leaf comes back from whichever fired:

```
seq_read_iter        a file read: ps, grep, awk, cat
vfs_readlink         readlink /proc/<pid>/exe, ls -l on a symlink
iterate_dir          ls of a directory
vfs_statx            stat, and ls -l per entry
rtnetlink_rcv_msg    ip
inet_diag_dump       ss
__netlink_dump_start any netlink dump
```

`catalog/procfs.py` still maps a dozen `/proc` paths to the function serving
them, but it is a shortcut rather than a requirement: it spares the discovery
run when the command names its file. A file with no entry there resolves its
leaf from the seq_file the kernel is holding, so `cat /proc/loadavg` reaches
`loadavg_proc_show` with nothing written down for it.

## A worked example

The row is `on_cpu` in a `task_struct`, which shows `ps -o psr= -p 1`.

### Stage 2: what it opens

```sh
sudo bpftrace -e 'kprobe:do_sys_openat2 /comm == "ps"/ {
    @opens[str(uptr(arg1))] = count(); }' -c '/bin/sh /tmp/cmd.sh'
```
```
@opens[/proc/1]: 1
@opens[/proc/self]: 1
@opens[/proc/self/status]: 2
```

`ps` opens the task directory, then opens the files inside it by name against
that directory descriptor, so most rows arrive as a bare `stat`. The probe does
not record the descriptor, and the rows that came in relative say so.

### Stage 3, first half: which file was read

The path is not on the command line and cannot be recovered from it. The dentry
at read time has it:

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

A numeric parent means `/proc/<pid>/`. `path_from_dentry` rebuilds the path from
those two names, and `SERVED_BY` turns it into a function. This step is skipped
when the command names its file, as `grep VmPTE /proc/1/status` does.

### Stage 3, second half: the stack that served it

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

Read bottom to top: syscall, VFS, seq_file, proc, the function that formats the
fields. Each frame is resolved through `nm` and `addr2line` against the cached
debuginfo, with the KASLR offset subtracted, which is what `core/source.py`
already does for walkthrough steps.

```
do_task_stat -> fs/proc/array.c:468
```

### Stage 4: what it reads

Two lists side by side, each labelled with where it came from.

The first is scanned out of the leaf's own source. The parameter names and
their types come from DWARF, so `task->flags` in the body of `do_task_stat` is
reported as `task_struct.flags` rather than guessed at:

```
task_struct.blocked   task_struct.exit_code   task_struct.exit_signal
task_struct.flags     task_struct.maj_flt     task_struct.min_flt
task_struct.pending   task_struct.policy      task_struct.rt_priority
task_struct.signal    task_struct.start_boottime
```

The second is the reverse lookup in `FIELD_COMMANDS` that used to be the whole
stage, and only the entries the scan did not find are shown: `gtime`, `prio`,
`stime`, `utime`. Those four are exactly the helper-mediated reads, which is
the useful part of the disagreement. `utime` arrives through `task_utime()` and
never appears as `task->utime` in the text.

The scan is labelled on screen as a floor rather than a list, for that reason.

## Sources for stage 4

```c
seq_put_decimal_ll(m, " ", tty_pgrp);     /* field 8  */
seq_put_decimal_ull(m, " ", task->flags); /* field 9  */
seq_put_decimal_ull(m, " ", min_flt);     /* field 10 */
seq_put_decimal_ull(m, " ", cmin_flt);    /* field 11 */
```

That confirms field 9 is `task->flags` printed unmasked, and that `maj_flt` is
field 12 because `cmin_flt` takes 11. Those numbers were in the table before
anything checked them.

The scan is the second of two candidates. The first, kept here because it
explains the shape of the one that was built:

**The function's DWARF signature.** Names the structures the leaf is handed:

```
do_task_stat      (struct seq_file *, struct pid_namespace *, struct pid *,
                   struct task_struct *, int)
proc_pid_status   (struct seq_file *, struct pid_namespace *, struct pid *,
                   struct task_struct *)
```

and says nothing for most seq_file show functions, which take the iterator's
cursor:

```
show_map          (struct seq_file *, void *)
meminfo_proc_show (struct seq_file *, void *)
show_stat         (struct seq_file *, void *)
```

So the signature says which structures are in play and the source says which
fields of them are read, and only the second is specific enough to be worth a
row. The signature is still what makes the scan safe: `task` is known to be a
`struct task_struct *` because DWARF says so, not because the name looks like
one.

**The field references in the leaf's source** is what stage 4 now does. It is a
floor rather than a list, and the screen says so.

## Traps

**`tracepoint:syscalls:sys_enter_openat` never fires on this kernel.** It is
listed by `bpftrace -l` and it counts nothing, even for `cat`, while
`sys_enter_read` on the same run counts fine. `kprobe:do_sys_openat2` works.
A design resting on syscall tracepoints would have silently reported that `ps`
opens nothing.

**`bpftrace -c` is not usable for this at all.** Two reasons, found one after
the other. It splits its argument and execs the result rather than running a
shell, so `awk '{...}' /proc/1/stat` arrives with its quotes as literal
arguments and awk dies before reading anything. And it waits for the command to
exit, which several commands in the catalog never do: `vmstat -n 1` prints a
line a second forever, and the frame sat on its placeholder until the timeout.
The command is now started here, once bpftrace's `Attached` line appears on
stderr, and killed after a few seconds if it is still running.

**Killing the child leaves the pipeline behind.** `timeout -s INT 3 sh -c
'vmstat -n 1 | awk …'` returns 124 on time and leaves `vmstat` running: the
signal reaches the shell, not the processes it started. The command runs in its
own process group and the group is killed, which is what stops the orphan.

**A kprobe reports callers, never callees.** A probe on `vfs_read` produces the
syscall path above it and nothing about what it will call, which is why the leaf
has to be known or discovered before the stack is worth taking.

**A `single_open` file's `->op->show` is always `proc_single_show`.** Reading
the seq_file's own show pointer looks like a way to discover the leaf
generically, and it stops one frame short of `do_task_stat`. Measuring the
file and looking the leaf up in the table is both more specific and less
clever. Note also that
`ksym()` on that pointer crashes bpftrace 0.24.2 with SIGTRAP; the raw address
works and drgn symbolises it.

**A static function's name is not unique, and a kprobe needs one that is.**
`findmnt` reads `/proc/self/mountinfo`, whose show function is `m_show`, and
kallsyms holds two of those:

```
ffffd7019c719628 t m_show
ffffd7019caeb0f8 t m_show
ERROR: Unable to attach probe: kprobe:m_show.
```

The whole trace failed on it. The address measured from the seq_file still says
which one ran, so the stack is now taken at the caller, `seq_read_iter`, and the
leaf is put back on the end of it, marked as known rather than probed, and
resolved from the address to `fs/namespace.c:1569` rather than from the name,
which would have been a coin toss between the two.

**`/proc/net` is a symlink to `/proc/self/net`.** A file under it has a pid
directory as its grandparent, which is visible in the dentry chain and is why
`path_from_dentry` has a rule for it.

## Cost

One traced run of the command when the file is named, two when it has to be
discovered. `ps -e` takes a couple of seconds; the frame is built in a worker
with a placeholder on screen, like a measurement. Resolving twelve stack frames
means twelve `addr2line` calls against a 700MB vmlinux, cached per address.

## Where it lives

```
catalog/procfs.py      the path-to-function map, path_from_dentry, fields_from
core/probe.py          trace_command (bpftrace -c), parse_stacks
operations/            command_trace.py: the four stages and the discovery pass
view/frames.py         command_trace_frame and command_trace_plan (deferred)
tui/app.py             the t key, and what counts as a command under the cursor
tests/                 test_command_trace.py: the key, the stages, s on a row
```

## Closed questions

Each of these was open when this document was first written, and each is now
answered by code. The evidence is above.

**Netlink.** `ss` and `ip` read no files, and both are traced. They are
two cases rather than one: naming a device turns a dump into a single request,
so a probe on `__netlink_dump_start` alone would have reported that
`ip -s link show eth0` does nothing.

```
ss -tanH               __netlink_dump_start 2   inet_diag_dump 4
ip link                __netlink_dump_start 1
ip -s link show eth0   __netlink_dump_start 0   rtnetlink_rcv_msg 2
```

The discovery pass probes all three and takes the busiest, which picks
`inet_diag_dump` for `ss` and `rtnetlink_rcv_msg` for `ip`. `tc` is not
installed on this VM, so it remains untested.

**Interfaces that are not reads.** One probe each:

```
readlink /proc/self/ns/net   vfs_readlink 1
ls -l /proc/1/fd             iterate_dir  2
stat -L /proc/1/fd/0         vfs_statx    4
```

Whether some lower point carries all of them was not pursued. Seven probes in
one script attach in well under a second, so the cost of asking each one
directly does not justify looking for a clever common point.

**The leaf without the table.** What a `single_open` file leaves in
`m->private` is the inode, not a proc entry:

```c
static int proc_single_show(struct seq_file *m, void *v)
{
	struct inode *inode = m->private;
	...
	ret = PROC_I(inode)->op.proc_show(m, ns, pid, task);
```

The probe records both `m->op->show` and `m->private`, and when the first is
`proc_single_show` the second is unwrapped in drgn:

```
container_of(inode, "struct proc_inode", "vfs_inode").op.proc_show
  -> 0xffffd7019cba9898  proc_tgid_stat
```

A file with its own iterator needs no unwrapping, which is how
`cat /proc/loadavg` reaches `loadavg_proc_show` with no table entry. `SERVED_BY`
is now a shortcut that skips the discovery run, not a prerequisite.

**Stage 4 from source.** The scan runs, paired with the catalog list, and only
the catalog entries the scan missed are shown. Those are the helper-mediated
reads.

**Many files, one answer.** Partly. The file the command names is traced,
the others are listed under stage 3 with their counts, and a netlink request
outranks both. Read counts alone rank the wrong thing twice over, and both
cases came from a screenshot rather than from reasoning:

* `grep VmPin /proc/1/status` reads its own `/proc/self/maps` twice while
  starting and the file it was asked about once, so the busiest file was
  `/proc/<pid>/maps` and the trace ended in `show_map`. A path the command
  names now wins regardless of count.
* `ss -tanp` opens `/proc/<pid>/fd/` and reads `/proc/<pid>/stat` for every
  process, 35 reads, to put a process name beside each socket. Those reads are
  real, and they buried the netlink dump the command exists for. A netlink
  handler now outranks file reads, and the reads are named in the evidence line
  rather than dropped.

Tracing several properly would still mean a stack per interface and a frame
that can hold more than one.

## Still open

**One level into helpers.** `utime` arrives through `task_utime()` and the task
state through `get_task_state()`, both invisible to a scan of the leaf's own
body. Following one level of helper calls would find them, at the cost of
deciding which calls are worth following.

**Cost and caching.** A trace is one or two runs of the command under bpftrace,
seconds each, plus a dozen `addr2line` calls against a 700MB vmlinux. Those
calls are cached per address for the session, in
`KernelSource.function_location`, so a second trace through the same frames pays
only for the runs. No trace result is cached at all, and it is not obvious one
should be: the answer depends on the command, on the kernel, and on what else
was running.

**comm as the filter.** The filter is the first word of the pipeline, truncated
to 15 bytes. The shell wrapper does not share it: `sh` carries its own comm
until it execs, and the second stage of a pipeline carries its own program's
name, so `ss -tanmH | grep -o …` counts nothing that `grep` does. The
mis-attribution runs the other way: any unrelated process with the same name
during those seconds is counted as ours, and work done in a differently named
child is missed. bpftrace's `cpid` would be exact, and it compiles and reports 0
on this build, so a pid filter would mean starting the command outside `-c` and
racing the probe attach against it.

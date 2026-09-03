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

Fedora does not publish a `kernel-debuginfo` package for the stock kernel, so
there is no local DWARF. `DEBUGINFOD_URLS` points drgn at Fedora's debuginfod
server, which serves both:

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

The tool assumes a debuginfod server that serves the running kernel's DWARF and
source. On a distro without one, drgn needs a local `vmlinux` with debug info
(`-s /path/to/vmlinux`), and the source-derived documentation needs a matching
source tree. The structure browser degrades to working without comments; the
walkthrough view loses its source links.

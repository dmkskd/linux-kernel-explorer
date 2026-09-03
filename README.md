# kexplore

A terminal explorer for a live Linux kernel, built on
[drgn](https://drgn.readthedocs.io/).

Explore a live kernel as a map of connected structures. Start from a subsystem
or a single task, then follow its fields and curated relationships to its
threads, address space, VMAs, open files, sockets, and more. Operations walk the
same map along a path such as a page fault, naming the function behind each step
and opening what it touches.

## See it in use

| Browse the live kernel | Follow a structure's relationships |
| --- | --- |
| <a href="docs/images/structure-browser.png"><img src="docs/images/structure-browser.png" alt="Structure browser showing process and scheduler entry points" width="400"></a> | <a href="docs/images/task-graph.png"><img src="docs/images/task-graph.png" alt="Graph showing a task's threads, address space, files, and sockets" width="400"></a> |

Click an image to open it at full size.

```
process › 611 auditd › threads › 612 gmain › mm (address space) › VMAs
```

## Setup

```sh
./setup.sh          # create and provision the lima VM
./run.sh            # start the explorer
./run.sh --check    # resolve every entry against this kernel, no UI
./run.sh --help     # every option, and the environment it reads
```

`run.sh` executes inside the VM as root, because reading `/proc/kcore` requires
it. The repo is mounted from the host at the same path, so there is nothing to
sync.

The VM is called `kernel-lab`. `KEXPLORE_VM` overrides that, and both scripts
read it, so export it rather than setting it per command:

```sh
export KEXPLORE_VM=my-vm
```

## Views

Two tabs in the sidebar. **structures** browses the kernel's data structures;
**operations** traces the code paths that read and modify them.

- **structures**: the subsystem tree. Open a struct, follow its fields.
  Curated links (`→`) add edges that are not fields, such as a task's threads,
  and each one shows where it comes from (`task->signal->thread_head`).
  Field documentation is the kernel's own comments for this exact build.
- **operations**: what the kernel does, using live data. Some are ordered
  steps (task wakeup, page fault), each naming a function resolved to
  `file:line` and opening the structures it touches. Others are analyses of a
  single moment: which task EEVDF would pick and why, what a thread shares with
  its parent that a fork copies, how many pages a child still shares.

Subsystems also have a **measure** group: run a tracer for a couple of seconds
and show the result. Nothing runs in the background.

## Working offline

The kernel's DWARF is a few hundred megabytes fetched from Fedora's debuginfod
server. libdebuginfod writes it to a temporary name and only renames it into the
cache when the transfer *completes* (there is no resume), so quitting the
explorer mid-fetch throws the whole download away and the next run starts over.

Fetch it once in a single uninterrupted transfer, and later runs read it from
the cache:

```sh
./run.sh --prefetch          # downloads to completion, with progress
./run.sh --offline           # cache only; never touches the network
```

The cache is keyed by the kernel's build-id, so an upgraded kernel means a new
download of the same size; the superseded copy stays on disk. Retention is a
year without a read, not indefinite.

`--offline` (or `KEXPLORE_OFFLINE=1`) points debuginfod at a closed port. That
is not the same as unsetting `DEBUGINFOD_URLS`, which makes the client return
`ENOSYS` without ever consulting the cache; with a dead URL the cache is still
searched first and only genuine misses fail: instantly, instead of stalling on
a roaming connection.

`--prefetch` also deletes abandoned partial downloads, which cost the size of a
vmlinux each. libdebuginfod separately prunes any cached file it has not read
for a week and re-probes a cache miss after ten minutes. Both are too short to
hold a deliberately prefetched vmlinux, so startup raises the retention limit to
a year and the re-probe interval to a day.

## Docs

[docs/](docs/README.md): how the tools underneath work, the code layout and
tests, and what is not built yet.

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

A recorded session, start to a traced command:

[![asciicast](https://asciinema.org/a/REPLACE_WITH_CAST_ID.svg)](https://asciinema.org/a/REPLACE_WITH_CAST_ID)

```
process › 611 auditd › threads › 612 gmain › mm (address space) › VMAs
```

## Requirements

kexplore reads a live Linux kernel as root (opening `/proc/kcore` requires
it), and it needs that kernel's DWARF debug info. Both are what the
requirements below are about; the Python code itself needs nothing but the
packages listed here.

Everything is installed by `./setup.sh`, except the two host tools you need
before it can run: `limactl` on macOS, a container runtime for the docker
backend.

### macOS

There is no native option: macOS has no Linux kernel to read. Everything runs
in a Fedora VM, and the kernel you explore is that VM's.

| Need | Detail |
| --- | --- |
| lima 2.0.0 or newer | `brew install lima`. `minimumLimaVersion` in `lima/kexplore.yaml` |
| disk and RAM for the VM | 4 CPUs, 8 GiB memory, 40 GiB disk, plus a ~1 GB Fedora 44 cloud image download |
| the repo under your home directory | the VM mounts `~` read-only at the same path, and run.sh runs kexplore from the host path it resolved |
| a few hundred MB of kernel DWARF | fetched into the VM on the first run, from Fedora's debuginfod server |
| asciinema | only for `./run.sh --record`: `brew install asciinema`. It records on the host, around whichever backend ran |

Everything inside the VM (drgn, elfutils, pahole, binutils, textual, bpftrace)
is provisioned by `./setup.sh`; nothing but lima is installed on the mac.

### Linux

Three backends. `detect.sh` resolves one on every run, and
`KEXPLORE_BACKEND=lima|native|docker` pins the choice.

| Distro | Backend | Why |
| --- | --- | --- |
| Fedora | native | the only distro whose debuginfod server carries kernel DWARF *and* source, and drgn uses debuginfod for the kernel only there |
| any, with docker or rootful podman | docker | a container reads the host kernel; the host keeps only the runtime, an image and a cache volume |
| Ubuntu 24.04+, Debian 12+ | native, second-class | automated by `setup.sh` with a warning |
| Arch, self-built kernels, anything else | lima | no fetchable kernel debug info, so a Fedora VM supplies both kernel and symbols |

**native (Fedora)**, installed by `setup.sh`:

```sh
sudo dnf install -y drgn elfutils-debuginfod-client dwarves binutils python3-textual
sudo dnf install -y bpftrace bcc-tools perf   # optional: the "measure" groups
sudo sysctl -w kernel.sched_schedstats=1      # several scheduler views need it
```

drgn must be 0.1.0 or newer; the check is whether
`drgn.helpers.linux.mm.vma_name` imports, not a version string. Running the
explorer needs sudo.

**docker**: docker, or podman as root (`sudo podman` counts). Rootless podman
cannot work at all: `/proc/kcore` needs `CAP_SYS_RAWIO` in the initial user
namespace, which a rootless container never has. The container shares the host
kernel, so the host's debug info situation applies unchanged.

**native (Ubuntu/Debian)**: not a supported setup, but `setup.sh` automates it.
The C tools come from apt, drgn from PyPI (apt's is older than 0.1.0,
LP#2106030), and the kernel's symbols from a multi-GB `linux-image-*-dbgsym`
package added through a new apt repo, because neither distro's debuginfod
serves kernel debug info. Struct documentation is disabled: neither serves
kernel source either.

**lima on Linux**: same Fedora VM as on macOS. limactl comes from
<https://github.com/lima-vm/lima/releases>.

### Anywhere

- Root on the machine that attaches to the kernel (the VM, the host, or the
  container), for `/proc/kcore`.
- Under Secure Boot lockdown the kernel refuses `/proc/kcore` even to root.
  Use the VM.
- `python3` on the host, for `python3 tests/run_all.py`: the tests that need
  no kernel. Everything else runs where the backend does.
- The lima VM is named `kernel-lab`. `KEXPLORE_VM` overrides it, and both
  `run.sh` and `setup.sh` read it.

## Setup

```sh
./setup.sh                                    # prepare the backend, then verify it end to end
./run.sh                                      # start the explorer
./run.sh --tutorial list                      # list curated live guided tutorials
./run.sh --tutorial memory                    # launch directly into a live guided tutorial
./run.sh --record demo.cast --tutorial memory # record the tutorial with asciinema
./run.sh --check                              # resolve every entry against this kernel, no UI
./run.sh --help                               # every option, and the environment it reads
```

`setup.sh` installs the machine and stops there: the kernel's debug info is
`run.sh`'s, start to finish (see [Working offline](#working-offline)).

With nothing usable installed, `setup.sh` asks which backend to set up and
runs it to completion. `run.sh` never asks; it tells you to run `setup.sh`
first.

## Views

Three tabs in the sidebar (switch between them with `v` or by clicking):

- **structures**: the subsystem tree. Open a struct, follow its fields.
  Curated links (`→`) add edges that are not fields, such as a task's threads,
  and each one shows where it comes from (`task->signal->thread_head`).
  Field documentation is the kernel's own comments for this exact build.
- **operations**: what the kernel does, using live data. Some are ordered
  steps (task wakeup, page fault), each naming a function resolved to
  `file:line` and opening the structures it touches. Others are analyses of a
  single moment: which task EEVDF would pick and why, what a thread shares with
  its parent that a fork copies, how many pages a child still shares.
- **tutorials**: live guided walkthroughs across the running kernel. Each tutorial
  dynamically discovers active processes, threads, memory mappings, or runqueues,
  driving an interactive stepper session: a commentary banner provides kernel
  context and userspace commands, while the main view opens the live kernel data
  structures with key fields highlighted. Use `n` / `Space` to step forward,
  `p` to step back, `Enter` to follow into child structures, and `Backspace` to return.

Subsystems also have a **measure** group: run a tracer for a couple of seconds
and show the result. Nothing runs in the background.

## Working offline

The kernel's DWARF is a few hundred megabytes fetched from Fedora's debuginfod
server. The first run downloads it to completion, with progress, before
attaching; later runs start from the cache. libdebuginfod writes to a temporary
name and only renames it into the cache when the transfer *completes* (there is
no resume), so interrupting the download discards it and the next run starts
over. `--no-prefetch` restores the old behavior: attach first, download lazily.

```sh
./run.sh --prefetch          # download to completion and exit (before a trip)
./run.sh --offline           # cache only; never touches the network
```

The cache is keyed by the kernel's build-id, so an upgraded kernel means a new
download of the same size; the superseded copy stays on disk. Retention is a
year without a read, not indefinite. Where the cache lives depends on the
backend: the VM's disk for lima, `~/.cache/debuginfod_client` for native, and
the `kexplore-debuginfod` volume for docker.

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

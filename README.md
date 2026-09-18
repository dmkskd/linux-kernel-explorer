# kexplore

A terminal explorer for learning and experimenting with a live Linux kernel,
built on [drgn](https://drgn.readthedocs.io/).

Use a disposable VM or a dedicated lab machine. **kexplore is not intended for
production servers.** On macOS, the Fedora VM is the recommended learning
environment: it runs a real Linux kernel you can inspect and exercise.

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

Download or clone this repository first (Git is needed only for cloning).
`./setup.sh` installs backend dependencies. On macOS, install `limactl` first;
the container backend requires Docker or rootful Podman first. Native Linux
installation requires Bash, Python 3.11 or newer, and sudo or equivalent
administrator access. Setup currently uses sudo.

### macOS

There is no native option: macOS has no Linux kernel to read. The explorer runs in a Linux VM and reads that VM's kernel. Fedora is the
default; Ubuntu 26.04 and Debian 13 are experimental alternatives.

| Need | Detail |
| --- | --- |
| lima 2.0.0 or newer | `brew install lima`. `minimumLimaVersion` in `lima/kexplore.yaml` |
| disk and RAM for the VM | 4 CPUs, 8 GiB memory, 40 GiB disk, plus a ~1 GB Fedora 44 cloud image download |
| the repo under your home directory | the VM mounts `~` read-only at the same path, and run.sh runs kexplore from the host path it resolved |
| a few hundred MB of kernel DWARF | fetched into the VM on the first run, from Fedora's debuginfod server |
| asciinema | only for `./run.sh --record`: `brew install asciinema`. It records on the host, around whichever backend ran |

Everything inside the VM (drgn, elfutils, pahole, binutils, textual, bpftrace)
is provisioned by `./setup.sh`; nothing but lima is installed on the mac.

### Choosing a distro on macOS

Use a new terminal and select a Lima lab profile:

```sh
export KEXPLORE_BACKEND=lima
export KEXPLORE_DISTRO=ubuntu  # fedora (default), ubuntu (26.04), debian (13)
./setup.sh
./run.sh --check
./run.sh
```

Default VM names are `kernel-lab`, `kernel-lab-ubuntu-26-04`, and `kernel-lab-debian`.
`KEXPLORE_VM` overrides the name; unset an old override before switching profiles.
The launcher refuses a VM whose distro or release differs from the selected
profile (Fedora 44, Ubuntu 26.04, Debian 13). Existing labs are not upgraded
to another distribution release.
Ubuntu/Debian install matching local DWARF and isolated Python dependencies;
matching kernel source trees are downloaded and configured automatically.
Downloads and extraction can take several minutes and require several GB.
Set `KEXPLORE_DOWNLOAD_SOURCE=0` before `./setup.sh` to skip optional source
downloads. Required debug symbols are still installed. See
[source download options](docs/installation.md#optional-source-downloads).
Each VM has its own packages and debug cache.

### Linux

Three backends. `detect.sh` resolves one on every run, and
`KEXPLORE_BACKEND=lima|native|docker` pins the choice.

| Distro | Backend | Why |
| --- | --- | --- |
| Fedora | native | recommended lab distro; automatic kernel DWARF and matching source through debuginfod |
| any, with docker or rootful podman | docker | a container reads the host kernel; the host keeps only the runtime, an image and a cache volume |
| Ubuntu 24.04+, Debian 12+ | native, experimental | automated recipe; exact release and kernel must be validated |
| Arch, self-built kernels, anything else | lima | recommended learning environment; custom kernels need their own matching DWARF for native use |

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

**native (Ubuntu/Debian lab machines)**: experimental; `setup.sh` automates
an installation but fresh installs on every accepted release are not validated.
C tools and Textual come from apt. The current recipe installs drgn from PyPI
into system Python and adds debug-package repositories. It installs
`linux-image-$(uname -r)-dbgsym` on Ubuntu or
`linux-image-$(uname -r)-dbg` on Debian. These packages can require several GB;
availability depends on the exact running kernel, flavour and repository.

Source files are optional, separate from DWARF. Ubuntu/Debian source can be
obtained from distribution source packages; it is not automatically integrated
through our debuginfod path. See [local source installation](docs/installation.md).

**lima on Linux**: same Fedora VM as on macOS. limactl comes from
<https://github.com/lima-vm/lima/releases>.

### Anywhere

- Root on the machine that attaches to the kernel (the VM, the host, or the
  container), for `/proc/kcore`.
- Under Secure Boot lockdown the kernel refuses `/proc/kcore` even to root.
  Use the VM.
- `python3` on the host, for `python3 tests/run_all.py`: the tests that need
  no kernel. Everything else runs where the backend does.
- The default Fedora VM is named `kernel-lab`. `KEXPLORE_VM` overrides it, and both
  `run.sh` and `setup.sh` read it.

## Setup

```sh
./setup.sh                                    # prepare the backend and check installed tools
./run.sh                                      # start the explorer
./run.sh --tutorial list                      # list curated live guided tutorials
./run.sh --tutorial memory                    # launch directly into a live guided tutorial
./run.sh --record demo.cast --tutorial memory # record the tutorial with asciinema
./run.sh --check                              # resolve every entry against this kernel, no UI
./run.sh --help                               # every option, and the environment it reads
```

`setup.sh` checks installed tools, not whether the kernel can be explored.
Fedora debug information is fetched on first run; the Ubuntu/Debian recipe
installs a local debug package during setup. Run `./run.sh --check` to verify
actual catalog access. See [installation details](docs/installation.md) and
[Working offline](#working-offline).

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

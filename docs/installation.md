# Installation

Run `kexplore` only on disposable virtual machines or dedicated lab machines,
never on production servers.

## Requirements

`kexplore` reads kernel memory through `/proc/kcore`, which requires root
(`CAP_SYS_RAWIO`), and resolves types from DWARF debug info built for the
running kernel. It therefore has to run on the machine whose kernel it
inspects.

The structure browser needs Python 3.11 or later, drgn 0.1.0 or later and
Textual 4.0.0. Source comments and the `s` source view additionally need a
source tree matching the running kernel, the measure groups need bpftrace, and
the per-task scheduler statistics need `kernel.sched_schedstats=1`.

## Backends

`kexplore` supports three backends:

| Backend | `kexplore` runs in | Kernel inspected | Hosts |
| --- | --- | --- | --- |
| `lima` | a lima virtual machine | the virtual machine's | macOS; Linux distributions other than Fedora, Ubuntu and Debian |
| `native` | the host | the host's | Fedora; Ubuntu 24.04 or later and Debian 12 or later (experimental) |
| `docker` | a container on the host, built by `setup.sh` | the host's | Linux with docker or podman running as root |

`KEXPLORE_BACKEND` selects the backend; when it is unset, `detect.sh` chooses
one for the host. Kernels in lockdown mode and rootless podman are not
supported.

## Changes made by `setup.sh`

`setup.sh` lists the changes below before making them. On the `native` backend
it also asks for confirmation before installing packages.

### `lima`

On the host, `setup.sh` creates the lima virtual machine and installs nothing.
lima 2.0.0 or later must already be installed, and the repository must be
under `~`, which the virtual machine mounts read-only at the same path.

| `KEXPLORE_DISTRO` | Virtual machine | Image | CPUs, memory, disk |
| --- | --- | --- | --- |
| `fedora` (default) | `kernel-lab` | Fedora 44 cloud image, about 1 GB | 4, 8 GiB, 40 GiB |
| `ubuntu` (experimental) | `kernel-lab-ubuntu-26-04` | Ubuntu 26.04, lima template | 4, 8 GiB, 60 GiB |
| `debian` (experimental) | `kernel-lab-debian` | Debian 13, lima template | 4, 8 GiB, 60 GiB |

`KEXPLORE_VM` overrides the virtual machine name. `setup.sh` and `run.sh`
refuse a virtual machine whose distribution or release differs from
`KEXPLORE_DISTRO`.

Inside the virtual machine, `setup.sh` installs drgn, elfutils, dwarves,
binutils and bpftrace, installs Textual 4.0.0 into `/opt/kexplore`, and sets
`kernel.sched_schedstats=1`. On `ubuntu` and `debian` it also installs the
kernel debug package and, unless `KEXPLORE_DOWNLOAD_SOURCE=0`, the kernel
source, several GB in total. On `debian`, if the image's kernel has no debug
package, it installs the cloud kernel and its debug package and reboots the
virtual machine.

### `native`

On Fedora, `setup.sh` runs, as root:

```sh
dnf install -y drgn elfutils-debuginfod-client dwarves binutils uv
dnf install -y bpftrace bcc-tools perf      # failure is not fatal
```

On Ubuntu and Debian, it runs `apt-get install` for debuginfod, elfutils,
dwarves, binutils, python3-textual and python3-pip, installs drgn into the
system Python with `pip3 install --break-system-packages`, adds a debug-symbol
APT source (`/etc/apt/sources.list.d/ddebs.sources` on Ubuntu,
`/etc/apt/sources.list.d/debian-debug.list` on Debian), and installs
`linux-image-$(uname -r)-dbgsym` (Ubuntu) or `linux-image-$(uname -r)-dbg`
(Debian), several GB.

On all three distributions it installs Textual 4.0.0 into `/opt/kexplore`,
writes `kernel.sched_schedstats = 1` to `/etc/sysctl.d/99-schedstats.conf`, and
applies it with `sysctl -w`.

### `docker`

`setup.sh` builds the image `kexplore` from `Containerfile`, pulling the Fedora
base image on the first build. Nothing is installed on the host. `run.sh` mounts
a volume named `kexplore-debuginfod` for the debug info cache, and the host's
`/usr/lib/debug` and `/lib/modules` read-only. The image does not contain
bpftrace.

## Verifying the installation

After installing, `setup.sh` checks that `drgn`, `debuginfod-find`, `pahole`,
`addr2line`, `nm` and `readelf` are on `PATH`, that Textual and drgn import,
that `drgn.helpers.linux.mm.vma_name` exists (drgn 0.1.0 or later), and whether
bpftrace is installed. It does not attach to the kernel.

`./run.sh --check` attaches to the kernel and resolves every catalog entry
without starting the UI. `run.sh` installs nothing; if the backend is not set
up, it exits and names `setup.sh`.

```sh
./run.sh --check                              # attach and resolve every catalog entry
./run.sh                                      # start kexplore
./run.sh --tutorial list                      # list the guided tutorials
./run.sh --record demo.cast --tutorial memory # record a tutorial with asciinema
./run.sh --help                               # all options and environment variables
```

## Debug info

On Fedora, `run.sh` downloads the `vmlinux` DWARF, a few hundred MB, from
Fedora's debuginfod server on its first run, and source files on demand. On
Ubuntu and Debian, `setup.sh` installs the debug package instead. Packages come
from the current APT candidates, so installations on different days can
differ.

The debuginfod cache is keyed by build ID; each kernel update causes a new
download. The cache is on the virtual machine's disk for `lima`, in
`~/.cache/debuginfod_client` for `native`, and in the `kexplore-debuginfod`
volume for `docker`.

## Kernel source

Source is optional. On `lima` with `ubuntu` or `debian`, `setup.sh` downloads
the source revision recorded in the debug package; if APT no longer has it,
Ubuntu retrieves it from Launchpad and Debian stops. `KEXPLORE_DOWNLOAD_SOURCE=0`
skips the download, and `=1` adds it on a later run.

A local tree must match the source package version of the running kernel (on
Ubuntu, `apt source linux-image-unsigned-$(uname -r)`):

```sh
./run.sh --source-root /path/to/linux
./run.sh --source-root /path/to/linux --source-prefix /build/original/linux
```

`--source-prefix` (`KEXPLORE_SOURCE_PREFIX`) maps the build path recorded in
DWARF to the tree. Paths are resolved where `kexplore` runs; on `docker`, an
absolute `KEXPLORE_SOURCE_ROOT` is mounted read-only. A local tree is not
checked against the running kernel and does not fall back to debuginfod.

## Offline use

```sh
./run.sh --prefetch   # download the DWARF and exit
./run.sh --offline    # use only the cache; also KEXPLORE_OFFLINE=1
```

libdebuginfod does not resume interrupted downloads, so `run.sh` downloads the
DWARF before attaching; `--no-prefetch` attaches first. `--prefetch` deletes
partial downloads. `kexplore` raises libdebuginfod's cache retention from one
week to one year and its cache-miss retry interval from ten minutes to one day.
`--offline` sets the debuginfod URL to a closed port, so the cache is still
read; an unset `DEBUGINFOD_URLS` makes the client return `ENOSYS` without
reading it.

## Known limitations

- `ubuntu` and `debian` installations are not reproducible: images, APT
  repositories and Python dependency versions change.
- Ubuntu removes older debug packages, and `setup.sh` has no kernel/debug-package
  recovery for Ubuntu.
- The `native` Ubuntu and Debian installation modifies the system Python.
- Fresh installations are validated on Apple Silicon only.

References: [drgn: getting debugging symbols](https://drgn.readthedocs.io/en/latest/getting_debugging_symbols.html),
[Ubuntu kernel build](https://ubuntu.com/kernel/docs/how-to/develop-customise/build-kernel/),
[Debian kernel compilation](https://www.debian.org/doc/manuals/debian-handbook/sect.kernel-compilation.en.html).

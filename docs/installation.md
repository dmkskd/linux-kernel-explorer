# Installing a kernel learning lab

kexplore is for learning and experimentation in disposable VMs and dedicated
lab machines. It is not intended for production servers.

## Recommended setup

On macOS, install Lima 2.0.0 or newer, download or clone this repository under
your home directory, then run:

```sh
./setup.sh
./run.sh --check
./run.sh
```

Setup provisions the Fedora VM. The explorer reads that VM's real running
kernel. On Linux, a Fedora VM is also a supported learning environment; native
Fedora installation reads the lab host's kernel. Ubuntu 24.04+ and Debian 12+
have experimental automated native recipes, not a validated guarantee for
every release or kernel variant.

## Distro profiles on macOS

`KEXPLORE_DISTRO=fedora|ubuntu|debian` selects the Lima profile. Fedora remains
recommended. Ubuntu 26.04 and Debian 13 are experimental alternatives using
Lima's bundled image templates, with 60 GiB disks for local debug packages.
The corresponding template must be present in your Lima installation.
The Ubuntu profile uses 26.04. Existing 24.04 VMs are preserved and are not
silently reused: unset KEXPLORE_VM or choose a new VM name to create 26.04.
Release selection does not replace validation against the actual kernel.
Ubuntu and Debian profiles use changing distribution repositories, not frozen
lab releases; every kernel variant and architecture is not yet validated.

```sh
export KEXPLORE_BACKEND=lima
export KEXPLORE_DISTRO=debian
unset KEXPLORE_VM
./setup.sh
./run.sh --check
./run.sh
```

Default names are `kernel-lab` (Fedora), `kernel-lab-ubuntu-26-04` and
`kernel-lab-debian`. Explicit `KEXPLORE_VM` names still work, but the selected
distro must match that VM. Switching profiles does not convert existing VMs.
Ubuntu/Debian Lima provisioning uses `/opt/kexplore` for isolated Python
packages and installs exact-running-kernel DWARF. This is separate from the
older experimental native recipe. Source trees remain optional.

## Required and optional artifacts

| Capability | Requirements |
| --- | --- |
| Structure browser | Python 3.11+, compatible drgn and Textual, root access to `/proc/kcore`, matching kernel DWARF |
| Current launcher/tool checks | `debuginfod-find`, `pahole`, `addr2line`, `nm`, `readelf` |
| Source comments and code links | Matching source tree, DWARF declaration/line information, pahole and binutils |
| Measurements | bpftrace and the required kernel tracing facilities and permissions |
| Some scheduling views | `kernel.sched_schedstats=1` |

Kernel headers and System.map do not replace DWARF. Source is not needed to
browse structures, and installing source does not require rebuilding the
kernel. Lockdown can prevent live kernel access even as root.

Fedora fetches DWARF by build ID and source on demand through debuginfod.
Ubuntu Lima setup also downloads and extracts the exact kernel source revision
recorded by the unsigned debug package. If APT has removed that revision,
setup fetches its retained source artifacts from Launchpad. The tree and DWARF
build path mapping are configured inside the VM automatically for run.sh.
Allow additional disk space and download time for this source tree.
Ubuntu uses a matching `linux-image-<release>-dbgsym` package; Debian uses
`linux-image-<release>-dbg`. The installer adds their debug repositories.
In a Debian Lima lab, if the image's kernel symbols are unavailable, setup
selects the cloud kernel debug metapackage's concrete kernel, checks that both
package versions match, installs the pair, and restarts the VM automatically.
It verifies the booted release and live kernel type resolution before reporting
Ready. This uses current APT candidates, not a frozen, reproducible lab release.
Native installation still requires artifacts for the exact running kernel.
The Debian 13 lab's 6.12 kernel currently has catalog compatibility gaps in
mutex waiters (`task_struct.blocked_on`), futex private hash tables
(`mm_struct.futex_phash`), and block hardware queues
(`request_queue.queue_hw_ctx`). Installation and source browsing work, but
these views need kernel layout compatibility before the whole catalog works.
Cloud flavours, older kernels and custom builds need matching
artifacts, not just a source tree with the same upstream version.

Allow space for downloads, extracted DWARF and the debug index in memory.
Ubuntu/Debian debug packages can occupy several GB. Setup checks tools;
`./run.sh --check` verifies that actual catalog entries resolve.

## Optional source downloads

Ubuntu and Debian Lima labs download matching kernel sources by default.
Downloads and extraction can take several minutes and use several GB of disk
space. To skip this optional step before creating a lab or rerunning setup:

```sh
export KEXPLORE_DOWNLOAD_SOURCE=0
./setup.sh
./run.sh
```

This skips source downloads only. Matching debug symbols are required for live
structure browsing and are still installed; those packages can also occupy
several GB. A lab without sources supports structure browsing, but source
comments and the `s` source view are unavailable. Previously installed sources
are preserved and remain usable. This option applies to Ubuntu/Debian Lima
setup; Fedora obtains sources on demand through debuginfod.

To install matching sources later:

```sh
export KEXPLORE_DOWNLOAD_SOURCE=1
./setup.sh
./run.sh
```

The installer uses the exact source revision recorded in the debug package,
not the latest upstream kernel. It reuses an already configured matching tree.
Debian uses its distribution source repositories, including security updates.
If the exact Debian revision is absent, setup stops rather than installing
unrelated sources. Ubuntu can also retrieve retained revisions from Launchpad.

## Optional local source

Enable distribution source repositories and obtain the exact source package
version used to build the running kernel. On Ubuntu, the starting command is:

```sh
apt source linux-image-unsigned-$(uname -r)
```

Some variants use a different source package. Select the exact package version,
not whichever newer version APT currently offers. Debian also provides source
packages and linux-source archives; match the distribution patch revision.

Then use an extracted source tree:

```sh
./run.sh --source-root /absolute/path/to/linux-source
```

When DWARF records absolute build paths, supply their root:

```sh
./run.sh --source-root /absolute/path/to/linux-source --source-prefix /build/original/linux
```

Paths are interpreted where the explorer runs. The Fedora Lima setup mounts
your home directory at the same path. For a container, use an absolute
`KEXPLORE_SOURCE_ROOT` so the launcher mounts an external tree read-only:

```sh
KEXPLORE_BACKEND=docker KEXPLORE_SOURCE_ROOT=/absolute/path/to/linux-source ./run.sh
```

`KEXPLORE_SOURCE_PREFIX` is the environment equivalent of `--source-prefix`.
Explicit local trees are authoritative: missing files do not fall back to
network source. The app labels the source revision as unverified. A wrong
revision can produce misleading comments or code locations.

## Containers and offline use

A container shares the Linux host's kernel. Its Fedora userspace does not make
the host kernel Fedora. The launcher selects the host distro's symbol server
and mounts installed `/usr/lib/debug` and `/lib/modules` directories read-only.
Install matching host debug packages when automatic downloads are unsuitable.
The existing container image does not include tracing tools.

`./run.sh --prefetch` prepares kernel DWARF, not a complete source tree.
Use `--offline` with installed or cached DWARF. Source requests can use cached
files, or a supplied local source tree. Kernel updates require matching new
artifacts. The app is currently run from this repository, not installed via pip.

References: [drgn symbol installation](https://drgn.readthedocs.io/en/latest/getting_debugging_symbols.html),
[Ubuntu source instructions](https://ubuntu.com/kernel/docs/how-to/develop-customise/build-kernel/),
[Debian kernel sources](https://www.debian.org/doc/manuals/debian-handbook/sect.kernel-compilation.en.html).

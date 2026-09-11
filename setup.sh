#!/usr/bin/env bash
# Prepare the machine kexplore runs on: the VM or container, the packages,
# and the sysctl. Then list what is installed.
#
# What this script never does is touch the kernel's debug info. That is a
# few hundred MB keyed to the running kernel's build-id, and run.sh owns it
# end to end (download, cache repair, --prefetch, --offline). Splitting it
# across both scripts is what let setup.sh fail on a cache entry that
# run.sh knows how to delete.
#
#   ./setup.sh                        # resolve the backend and set it up
#   KEXPLORE_BACKEND=docker ./setup.sh
#
# Backend resolution lives in detect.sh, shared with run.sh:
#   lima    create and provision the VM (the only option on macOS)
#   native  check the tools on this Linux host; offer to install them
#   docker  build the image and list the tools in it
#
# Idempotent: everything here can run again over an existing setup.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Must match run.sh, which sources the same file and so resolves the same
# backend.
. "$REPO/detect.sh"

VM="${KEXPLORE_VM:-kernel-lab}"
IMAGE=kexplore

usage() {
  cat <<USAGE
usage: ./setup.sh [-h|--help]

Set up the machine kexplore runs on (lima, native or docker). Safe to run
again: an existing setup is re-checked rather than recreated.

What this installs:

  the VM or container image, kexplore's packages (drgn, elfutils, pahole,
  binutils, python3-textual), and kernel.sched_schedstats=1

What it leaves to run.sh:

  the running kernel's debug info, a few hundred MB fetched from a
  debuginfod server. ./run.sh downloads it on the first attach, or
  ./run.sh --prefetch does it on its own.

The backend is detected (lima on macOS; native on Linux when kexplore's
packages are installed, docker when docker or podman is), or pinned:

  KEXPLORE_BACKEND   lima, native or docker
  KEXPLORE_VM        lima backend: name of the VM (currently: $VM)

run.sh reads the same variables, so set them for both or neither.
USAGE
}

if [[ $# -gt 0 ]]; then
  case "$1" in
    -h|--help) usage; exit 0 ;;
    *) usage >&2; echo >&2; echo "unknown argument: $1" >&2; exit 2 ;;
  esac
fi

# Every phase announces itself, so a long wait is always attributable to the
# line above it.
step() {
  echo
  echo "== $* =="
}

# Check the tools are installed, inside the VM (lima), with sudo (native) or
# in the container (docker).
#
# Deliberately stops at the tools. Whether drgn can resolve kernel types
# depends on the vmlinux DWARF, and everything that manages that download
# lives in kexplore itself: clearing the zero-length cache entries
# libdebuginfod writes for a 404, discarding partial transfers, pinning the
# retention, --prefetch and --offline. Resolving types here would run that
# fetch with none of it, and failed on exactly those poisoned entries. The
# DWARF belongs to run.sh; this script never touches it.
read -r -d '' VERIFY <<'EOF' || true
set -u
fail=0
for tool in drgn debuginfod-find pahole addr2line nm; do
  if command -v "$tool" >/dev/null; then
    printf "  ok       %s\n" "$tool"
  else
    printf "  MISSING  %s\n" "$tool"; fail=1
  fi
done
python3 -c 'import textual' 2>/dev/null \
  && echo "  ok       python3-textual" \
  || { echo "  MISSING  python3-textual"; fail=1; }
command -v bpftrace >/dev/null \
  && echo "  ok       bpftrace (measurements enabled)" \
  || echo "  absent   bpftrace (measurements will be unavailable)"

# No kernel is attached here: this only asks whether the module imports and
# is new enough for kexplore.
python3 - <<'PYCHECK'
import sys
try:
    import drgn  # noqa: F401
except ImportError:
    print("  FAILED   drgn python module not importable")
    sys.exit(1)
try:
    # kexplore's floor: vma_name appeared in drgn 0.1.0. A feature check,
    # not a version parse.
    from drgn.helpers.linux.mm import vma_name  # noqa: F401
except ImportError:
    print("  FAILED   drgn is older than 0.1.0 (no vma_name helper)")
    sys.exit(1)
print("  ok       drgn python module")
PYCHECK
exit $(( fail + $? ))
EOF

setup_lima() {
  if ! command -v limactl >/dev/null; then
    if [ "$(uname -s)" = Darwin ]; then
      echo "limactl not found. Install it with: brew install lima" >&2
    else
      echo "limactl not found. Install lima from" >&2
      echo "https://github.com/lima-vm/lima/releases" >&2
    fi
    exit 1
  fi

  if limactl list --quiet 2>/dev/null | grep -qx "$VM"; then
    step "VM '$VM' exists; starting it if it is not running"
    limactl start "$VM" >/dev/null
  else
    step "Creating VM '$VM' (downloads a Fedora cloud image, about 1 GB)"
    limactl start --name="$VM" "$REPO/lima/kexplore.yaml"
  fi

  step "Checking the tools inside '$VM'"
  limactl shell "$VM" sudo bash -s <<<"$VERIFY"
}

confirm_install() {
  if [ ! -t 0 ]; then
    echo "No terminal to confirm with; run the commands above yourself." >&2
    exit 1
  fi
  read -r -p "Install? [y/N] " answer
  case "$answer" in
    y|Y|yes) ;;
    *) echo "Not installing. Re-run ./setup.sh when ready."; exit 1 ;;
  esac
}

set_schedstats() {
  echo 'kernel.sched_schedstats = 1' | sudo tee /etc/sysctl.d/99-schedstats.conf >/dev/null
  sudo sysctl -w kernel.sched_schedstats=1 >/dev/null
}

# Debian/Ubuntu, automated but unsupported: apt for the C tools, PyPI for
# drgn (apt's is older than kexplore's 0.1.0 floor), and the distro's debug
# symbol package for the kernel DWARF (their debuginfod serves none,
# LP#2106030). Struct documentation is disabled: neither distro serves
# kernel source.
repair_ubuntu_ddebs() {
  local codename="$1" source
  # Only accept a plain Ubuntu codename before using it in a regex.
  [[ "$codename" =~ ^[a-z]+$ ]] || return 1
  for source in /etc/apt/sources.list /etc/apt/sources.list.d/*.list /etc/apt/sources.list.d/*.sources; do
    [ -f "$source" ] || continue
    grep -q 'ddebs\.ubuntu\.com' "$source" || continue
    grep -Eq "\b${codename}${codename}\b" "$source" || continue
    echo "Repairing duplicated Ubuntu codename in $source (backup: $source.kexplore-backup)"
    sudo sed -i.kexplore-backup "s/\b${codename}${codename}\b/${codename}/g" "$source"
  done
  return 0
}

install_native_deb() {
  local id codename dbgsym
  # pip falls back to a source build when no compatible wheel is available
  # (for example, on a newly released Python). Include development headers,
  # not just the runtime tools, for that path.
  local -a packages=(
    debuginfod elfutils dwarves binutils python3-textual python3-pip
    build-essential pkgconf python3-dev python3-setuptools
    libdebuginfod-dev libelf-dev libdw-dev libkdumpfile-dev liblzma-dev
    libpcre2-dev libjson-c-dev zlib1g-dev
  )
  # shellcheck disable=SC1091
  id=$(. /etc/os-release && echo "${ID:-}")
  # shellcheck disable=SC1091
  codename=$(. /etc/os-release && echo "${VERSION_CODENAME:-${UBUNTU_CODENAME:-}}")
  if [ "$id" = ubuntu ]; then
    dbgsym="linux-image-$(uname -r)-dbgsym"
  else
    dbgsym="linux-image-$(uname -r)-dbg"
  fi

  echo "This is not a supported setup. What it takes here:"
  echo
  echo "  # runtime tools and dependencies for building drgn from source"
  echo "  sudo apt-get install -y ${packages[*]}"
  echo "  sudo pip3 install --break-system-packages --upgrade drgn   (apt's drgn is older than 0.1.0)"
  if [ "$id" = ubuntu ]; then
    echo "  # new apt repo ddebs.ubuntu.com, for the kernel debug symbols"
  else
    echo "  # new apt repo deb.debian.org/debian-debug, for the kernel debug symbols"
  fi
  echo "  sudo apt-get install -y $dbgsym   (several GB)"
  echo
  echo "kernel.sched_schedstats=1 is set too, as with the supported distros."
  echo
  confirm_install

  # A malformed entry from an earlier run blocks even the first update,
  # before we get a chance to install the keyring and write ddebs.sources.
  if [ "$id" = ubuntu ]; then
    repair_ubuntu_ddebs "$codename"
  fi
  sudo apt-get update
  sudo apt-get install -y "${packages[@]}"
  if [ "$id" = ubuntu ]; then
    sudo apt-get install -y ubuntu-dbgsym-keyring
    sudo tee /etc/apt/sources.list.d/ddebs.sources >/dev/null <<EOF
Types: deb
URIs: http://ddebs.ubuntu.com/
Suites: $codename $codename-updates $codename-proposed
Components: main restricted universe multiverse
Signed-by: /usr/share/keyrings/ubuntu-dbgsym-keyring.gpg
EOF
  else
    # Debian ships kernel debug symbols in a separate archive, per drgn's
    # own documentation.
    sudo tee /etc/apt/sources.list.d/debian-debug.list >/dev/null <<EOF
deb http://deb.debian.org/debian-debug $codename-debug main
deb http://deb.debian.org/debian-debug $codename-proposed-updates-debug main
EOF
  fi
  sudo pip3 install --break-system-packages --upgrade drgn
  sudo apt-get update
  sudo apt-get install -y "$dbgsym"
  sudo apt-get install -y bpftrace || true
  set_schedstats
}

install_native() {
  local id=""
  if [ -r /etc/os-release ]; then
    # shellcheck disable=SC1091
    id=$(. /etc/os-release && echo "${ID:-}")
  fi

  if ! PKGCMD="$(kexplore_package_command)"; then
    if kexplore_deb_installable; then
      install_native_deb
      return
    fi
    echo "kexplore cannot be installed automatically on this system (ID='$id')." >&2
    echo "Use a Fedora VM: KEXPLORE_BACKEND=lima ./setup.sh" >&2
    exit 1
  fi
  # bpftrace and friends are optional (they enable the "measure" groups),
  # so their failure is not fatal.
  case "$id" in
    fedora) OPTCMD="dnf install -y bpftrace bcc-tools perf" ;;
  esac

  echo "The native backend installs packages on this host:"
  echo
  echo "  $PKGCMD"
  echo "  $OPTCMD   (optional: measurements)"
  echo
  echo "It also sets kernel.sched_schedstats=1 (several scheduler views need"
  echo "per-task scheduler statistics, which are off by default)."
  echo
  confirm_install

  # shellcheck disable=SC2086
  sudo $PKGCMD
  # shellcheck disable=SC2086
  sudo $OPTCMD || true
  set_schedstats
}

setup_native() {
  if kexplore_native_ready; then
    echo "kexplore's packages are already installed."
  else
    install_native
  fi

  if [ "$(sysctl -n kernel.sched_schedstats 2>/dev/null || echo 0)" != "1" ]; then
    echo
    echo "note: kernel.sched_schedstats is off; several scheduler views need it."
    echo "      'sudo sysctl -w kernel.sched_schedstats=1' enables it until reboot."
  fi

  step "Checking the tools on this machine"
  sudo bash -s <<<"$VERIFY"
}

setup_docker() {
  if ! RUNTIME="$(kexplore_container_runtime)"; then
    echo "the docker backend needs docker or rootful podman (rootless" >&2
    echo "podman cannot read /proc/kcore: that needs CAP_SYS_RAWIO in the" >&2
    echo "initial user namespace). Install one and re-run ./setup.sh." >&2
    exit 1
  fi

  step "Building image '$IMAGE' (pulls the Fedora base on first run)"
  # shellcheck disable=SC2086
  $RUNTIME build -t "$IMAGE" -f "$REPO/Containerfile" "$REPO"

  step "Checking the tools inside the image"
  # No kernel and no cache volume here: this container only lists tools.
  # shellcheck disable=SC2086
  $RUNTIME run --rm -i "$IMAGE" bash -s <<<"$VERIFY"
}

echo "Detected: $(kexplore_os_name)"

if [ -n "${KEXPLORE_BACKEND:-}" ]; then
  # A pinned backend that fails to resolve already printed why.
  BACKEND="$(kexplore_backend)" || exit 1
elif ! BACKEND="$(kexplore_backend 2>/dev/null)"; then
  # Nothing to run against. Offer only what this script can drive to
  # completion on this machine; no option here is homework. run.sh never
  # prompts; it reports this state and exits.
  echo
  if PKGCMD="$(kexplore_package_command)"; then
    # Fedora: a clean native install is available.
    echo "kexplore needs drgn, elfutils, pahole, binutils and python3-textual."
    echo
    echo "  1) install them on this machine:"
    echo "       sudo $PKGCMD"
    echo "  2) create a Fedora VM with lima and run kexplore in it"
    MENU=native
  elif kexplore_deb_installable; then
    # Debian/Ubuntu: automatable, but second-class (see install_native_deb).
    echo "kexplore runs best in a Fedora VM on $(kexplore_os_name): the distro"
    echo "serves no kernel debug info and its drgn is older than 0.1.0."
    echo
    echo "  1) create a Fedora VM with lima and run kexplore in it (recommended)"
    echo "  2) install on this machine anyway (kernel dbgsym, several GB, drgn"
    echo "     from PyPI; struct documentation disabled)"
    MENU=deb
  else
    # Anything else: no native route, automated or otherwise.
    echo "kexplore cannot be installed natively on $(kexplore_os_name): the"
    echo "kernel's debug info is not fetchable, or the packaged drgn is too"
    echo "old. The route here is a Fedora VM, whose kernel has both."
    echo
    echo "  1) create a Fedora VM with lima and run kexplore in it"
    MENU=lima
  fi
  echo
  if [ ! -t 0 ]; then
    echo "No terminal to ask which; re-run on a terminal." >&2
    exit 1
  fi
  read -r -p "Choice: " choice
  case "$MENU:$choice" in
    native:1) BACKEND=native ;;
    native:2) BACKEND=lima ;;
    deb:1)    BACKEND=lima ;;
    deb:2)    BACKEND=native ;;
    lima:1)   BACKEND=lima ;;
    *) echo "unknown choice: $choice" >&2; exit 2 ;;
  esac
elif [ "$BACKEND" = docker ] && PKGCMD="$(kexplore_package_command)"; then
  # Auto-resolution picked the container because a runtime exists, but this
  # distro supports a clean native install, which is the better default on
  # Fedora. Ask rather than silently building an image.
  echo
  echo "kexplore can run natively on this machine, or in a container."
  echo
  echo "  1) install the packages on this machine (recommended):"
  echo "       sudo $PKGCMD"
  echo "  2) run kexplore in a container ($(kexplore_container_runtime))"
  echo
  if [ ! -t 0 ]; then
    echo "No terminal to ask which; re-run on a terminal." >&2
    exit 1
  fi
  read -r -p "Choice: " choice
  case "$choice" in
    1) BACKEND=native ;;
    2) ;;
    *) echo "unknown choice: $choice" >&2; exit 2 ;;
  esac
fi

if [ -n "${KEXPLORE_BACKEND:-}" ]; then
  echo "Backend: $BACKEND (pinned by KEXPLORE_BACKEND)"
elif [ "$BACKEND" = lima ]; then
  echo "Backend: lima (macOS host; a Linux VM is required)"
elif [ "$BACKEND" = native ] && kexplore_native_ready; then
  echo "Backend: native (detected Linux with kexplore's packages installed)"
elif [ "$BACKEND" = docker ]; then
  echo "Backend: docker (detected Linux with a container runtime, packages not installed)"
else
  echo "Backend: $BACKEND (your choice above)"
fi

case "$BACKEND" in
  lima) setup_lima ;;
  native) setup_native ;;
  docker) setup_docker ;;
esac

step "Ready ($BACKEND)"
cat <<NEXT
The machine is set up. This script did not touch the kernel's debug info:
it is a few hundred MB, keyed to the running kernel, and run.sh is what
fetches, caches and repairs it.

  ./run.sh           the explorer. The first run downloads the debug info
                     once, with progress; later runs start from the cache
  ./run.sh --help    the rest, including how to work with no network
NEXT

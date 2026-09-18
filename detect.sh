# Where kexplore runs. Sourced by run.sh and setup.sh so the two can never
# resolve differently:
# shellcheck shell=bash
#
#   lima    a lima VM (the only option on macOS, also fine on Linux)
#   native  the host Linux kernel, with the tools installed locally
#   docker  a container that reads the host kernel through /proc/kcore
#
# KEXPLORE_BACKEND pins the choice. Unset, it is resolved fresh on every
# invocation: lima on macOS, native on Linux when the tools are already
# installed, docker when a container runtime is, and an error otherwise.

# True when the native backend can run: the tools kexplore shells out to,
# plus the two Python modules. drgn is checked by importing vma_name, the
# 0.1.0 floor: an importable-but-older drgn (what apt ships) does not count.
kexplore_native_ready() {
  command -v debuginfod-find >/dev/null \
    && command -v pahole >/dev/null \
    && command -v addr2line >/dev/null \
    && command -v nm >/dev/null \
    && command -v readelf >/dev/null \
    && python3 -c 'from drgn.helpers.linux.mm import vma_name' >/dev/null 2>&1 \
    && python3 -c 'from textual.widgets import DataTable; from importlib.metadata import version; assert int(version("textual").split(".")[0]) >= 1' >/dev/null 2>&1
}

# Echoes the container runtime command ("docker", "podman", "sudo podman")
# when one can run a kcore-capable container, and fails otherwise. Rootless
# podman is excluded on purpose: /proc/kcore needs CAP_SYS_RAWIO in the
# initial user namespace, which a rootless container never has.
kexplore_container_runtime() {
  if command -v docker >/dev/null && docker info >/dev/null 2>&1; then
    echo docker
  elif command -v podman >/dev/null; then
    if [ "$(id -u)" -eq 0 ]; then
      echo podman
    elif sudo -n podman info >/dev/null 2>&1; then
      echo "sudo podman"
    else
      return 1
    fi
  else
    return 1
  fi
}

# Echoes the resolved backend: lima, native or docker. Fails when this
# machine has no way to run kexplore; the message already says so, callers
# just propagate the exit.
kexplore_backend() {
  case "${KEXPLORE_BACKEND:-}" in
    lima|docker)
      echo "$KEXPLORE_BACKEND"
      return 0
      ;;
    native)
      if [ "$(uname -s)" != Linux ]; then
        echo "KEXPLORE_BACKEND=native needs a Linux host; this is $(uname -s)" >&2
        return 1
      fi
      echo native
      return 0
      ;;
    "") ;;
    *)
      echo "KEXPLORE_BACKEND must be lima, native or docker (currently: $KEXPLORE_BACKEND)" >&2
      return 1
      ;;
  esac
  case "$(uname -s)" in
    Darwin)
      echo lima
      ;;
    Linux)
      if kexplore_native_ready; then
        echo native
      elif kexplore_container_runtime >/dev/null; then
        echo docker
      else
        echo "kexplore: prerequisites not installed" >&2
        return 1
      fi
      ;;
    *)
      echo "unsupported OS: $(uname -s)" >&2
      return 1
      ;;
  esac
}

# The extra flags a container needs so drgn can read the host kernel. Both
# runtimes mount /proc/kcore as /dev/null by default, and the read then
# succeeds returning nothing, so drgn fails only later and confusingly.
# $1 is the runtime string from kexplore_container_runtime.
kexplore_container_kernel_flags() {
  case "$1" in
    *podman*) echo "--cap-add SYS_RAWIO --security-opt unmask=/proc/kcore" ;;
    *)        echo "--cap-add SYS_RAWIO --security-opt systempaths=unconfined" ;;
  esac
}

# A short name for the machine the scripts run on, for the detection line:
# the distro's PRETTY_NAME on Linux, "macOS" on a mac.
kexplore_os_name() {
  case "$(uname -s)" in
    Darwin) echo "macOS" ;;
    Linux)
      if [ -r /etc/os-release ]; then
        # shellcheck disable=SC1091
        ( . /etc/os-release && echo "${PRETTY_NAME:-Linux}" )
      else
        echo "Linux"
      fi
      ;;
    *) uname -s ;;
  esac
}

# The package install command for this machine's distro, so setup.sh's menu
# and its install step always show and run the same command. Only Fedora
# qualifies: it packages a current drgn and its debuginfod server carries
# kernel DWARF and source. drgn itself only uses debuginfod for the kernel
# on Fedora (elsewhere the transfers are too slow), Arch ships no kernel
# debug symbols through the route used here. Debian/Ubuntu use local kernel
# debug packages and the separate recipe in setup.sh. Anything else fails
# here and takes another route: the Debian/Ubuntu recipe in setup.sh, or
# the lima backend.
kexplore_package_command() {
  local id=""
  if [ -r /etc/os-release ]; then
    # shellcheck disable=SC1091
    id=$(. /etc/os-release && echo "${ID:-}")
  fi
  case "$id" in
    fedora)
      echo "dnf install -y drgn elfutils-debuginfod-client dwarves binutils python3-textual"
      ;;
    *)
      return 1
      ;;
  esac
}

# True when the unsupported Debian/Ubuntu recipe in setup.sh can run: new
# enough that apt carries python3-textual and the kernel debug package.
kexplore_deb_installable() {
  local id="" ver=""
  if [ -r /etc/os-release ]; then
    # shellcheck disable=SC1091
    id=$(. /etc/os-release && echo "${ID:-}")
    # shellcheck disable=SC1091
    ver=$(. /etc/os-release && echo "${VERSION_ID%%.*}")
  fi
  case "$id" in
    ubuntu) [ "${ver:-0}" -ge 24 ] 2>/dev/null ;;
    debian) [ "${ver:-0}" -ge 12 ] 2>/dev/null ;;
    *)      return 1 ;;
  esac
}

# The debuginfod server matching this machine's distro, the default for the
# native and container backends. Lima uses the selected lab distribution.
# Other distros can require local kernel debug packages.
kexplore_debuginfod_default() {
  local id=""
  if [ -r /etc/os-release ]; then
    # shellcheck disable=SC1091
    id=$(. /etc/os-release && echo "${ID:-}")
  fi
  case "$id" in
    fedora) echo "https://debuginfod.fedoraproject.org/" ;;
    ubuntu) echo "https://debuginfod.ubuntu.com" ;;
    debian) echo "https://debuginfod.debian.net" ;;
    arch)   echo "https://debuginfod.archlinux.org" ;;
    *)      echo "https://debuginfod.elfutils.org/" ;;
  esac
}

# The debuginfod server a given backend defaults to, so run.sh never spells a
# URL out itself. Lima uses the selected lab distribution; containers use the
# host distro because they read the host kernel.
kexplore_debuginfod_for_backend() {
  case "$1" in
    lima) kexplore_lima_server ;;
    *)           kexplore_debuginfod_default ;;
  esac
}

# Shared Lima profile selection. Preserve the original Fedora default VM.
kexplore_lima_distro() {
  case "${KEXPLORE_DISTRO:-fedora}" in
    fedora|ubuntu|debian) echo "${KEXPLORE_DISTRO:-fedora}" ;;
    *) echo "KEXPLORE_DISTRO must be fedora, ubuntu or debian" >&2; return 1 ;;
  esac
}

kexplore_lima_vm() {
  local distro
  distro="$(kexplore_lima_distro)" || return 1
  if [ -n "${KEXPLORE_VM:-}" ]; then
    echo "$KEXPLORE_VM"
  elif [ "$distro" = fedora ]; then
    echo kernel-lab
  elif [ "$distro" = ubuntu ]; then
    echo kernel-lab-ubuntu-26-04
  else
    echo "kernel-lab-$distro"
  fi
}

kexplore_lima_template() {
  local distro
  distro="$(kexplore_lima_distro)" || return 1
  case "$distro" in
    fedora) echo kexplore.yaml ;;
    *) echo "kexplore-$distro.yaml" ;;
  esac
}

kexplore_lima_server() {
  local distro
  distro="$(kexplore_lima_distro)" || return 1
  case "$distro" in
    fedora) echo https://debuginfod.fedoraproject.org/ ;;
    ubuntu) echo https://debuginfod.ubuntu.com ;;
    debian) echo https://debuginfod.debian.net ;;
  esac
}

# Refuse to repurpose an existing VM of a different distribution.
kexplore_check_lima_distro() {
  local expected actual release identity
  expected="$(kexplore_lima_distro)" || return 1
  # Variables intentionally expand inside the guest shell.
  # shellcheck disable=SC2016
  identity="$(limactl shell "$1" sh -c '. /etc/os-release; printf "%s %s\n" "$ID" "$VERSION_ID"')" || return 1
  read -r actual release <<<"$identity"
  if [ "$actual" != "$expected" ]; then
    echo "VM '$1' runs $actual, but KEXPLORE_DISTRO selects $expected." >&2
    echo "Choose a different KEXPLORE_VM or select the matching distro." >&2
    return 1
  fi
  if [ "$expected" = ubuntu ] && [ "$release" != 26.04 ]; then
    echo "VM '$1' runs Ubuntu $release; the Ubuntu profile requires 26.04." >&2
    echo "Unset KEXPLORE_VM to create a separate 26.04 lab; existing VMs are not upgraded." >&2
    return 1
  fi
  if { [ "$expected" = fedora ] && [ "$release" != 44 ]; } || \
     { [ "$expected" = debian ] && [ "$release" != 13 ]; }; then
    echo "VM '$1' runs $actual $release; this profile requires Fedora 44 or Debian 13 respectively." >&2
    echo "Choose a separate KEXPLORE_VM; existing VMs are not upgraded." >&2
    return 1
  fi
}

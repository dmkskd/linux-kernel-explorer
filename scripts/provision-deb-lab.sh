#!/usr/bin/env bash
# Only invoked explicitly by setup.sh for a dedicated Ubuntu/Debian Lima lab.
set -euo pipefail
[ "${KEXPLORE_LAB_PROVISION:-}" = 1 ] && [ "$(id -u)" = 0 ] || {
  echo "Run this through setup.sh for a dedicated lab VM." >&2
  exit 1
}
# shellcheck disable=SC1091
. /etc/os-release
# Package installations may overlap with cloud-init or another setup attempt.
apt-get() { command apt-get -o DPkg::Lock::Timeout=300 "$@"; }
case "$ID" in
  ubuntu|debian) ;;
  *) echo "This lab installer requires Ubuntu or Debian." >&2; exit 1 ;;
esac
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y debuginfod elfutils dwarves binutils python3-textual python3-venv \
  python3-dev build-essential pkgconf libelf-dev libdw-dev libdebuginfod-dev \
  libkdumpfile-dev liblzma-dev libpcre2-dev libjson-c-dev zlib1g-dev
# Keep pip packages inside this VM's dedicated environment.
if [ ! -x /opt/kexplore/bin/python3 ]; then
  python3 -m venv --system-site-packages /opt/kexplore
fi
if ! /opt/kexplore/bin/python3 -c 'from drgn.helpers.linux.mm import vma_name' 2>/dev/null; then
  /opt/kexplore/bin/python3 -m pip install 'drgn>=0.1.0,<0.3'
fi
if ! /opt/kexplore/bin/python3 -c 'from importlib.metadata import version; assert int(version("textual").split(".")[0]) >= 1' 2>/dev/null; then
  /opt/kexplore/bin/python3 -m pip install 'textual>=1,<9'
fi
release="$(uname -r)"
if [ "$ID" = ubuntu ]; then
  apt-get install -y ubuntu-dbgsym-keyring
  cat > /etc/apt/sources.list.d/kexplore-ddebs.sources <<EOF
Types: deb
URIs: http://ddebs.ubuntu.com/
Suites: $VERSION_CODENAME $VERSION_CODENAME-updates
Components: main restricted universe multiverse
Signed-By: /usr/share/keyrings/ubuntu-dbgsym-keyring.gpg
EOF
  debug_package="linux-image-$release-dbgsym"
else
  cat > /etc/apt/sources.list.d/kexplore-debug.list <<EOF
deb http://deb.debian.org/debian-debug $VERSION_CODENAME-debug main
EOF
  debug_package="linux-image-$release-dbg"
fi
apt-get update
restart_required=0
if ! apt-cache show "$debug_package" >/dev/null 2>&1; then
  if [ "$ID" != debian ]; then
    echo "Matching kernel DWARF package is unavailable: $debug_package" >&2
    exit 1
  fi
  echo "Symbols for the image's kernel are unavailable; selecting a matching Debian cloud kernel pair."
  arch="$(dpkg --print-architecture)"
  debug_package="$(apt-cache depends "linux-image-cloud-$arch-dbg" | awk '/Depends: linux-image-.*-dbg$/ {print $2; exit}')"
  [[ "$debug_package" == linux-image-*-dbg ]] || {
    echo "No Debian cloud kernel symbol package is available." >&2; exit 1;
  }
  release="${debug_package#linux-image-}"
  release="${release%-dbg}"
  kernel_package="linux-image-$release"
  kernel_version="$(apt-cache policy "$kernel_package" | awk '/Candidate:/ {print $2}')"
  debug_version="$(apt-cache policy "$debug_package" | awk '/Candidate:/ {print $2}')"
  [ -n "$kernel_version" ] && [ "$kernel_version" != '(none)' ] && \
    [ "$kernel_version" = "$debug_version" ] || {
      echo "No matching kernel and symbol package versions are available." >&2; exit 1;
    }
  echo "Installing $release, kernel and symbols version $kernel_version. Setup will restart this lab VM."
  apt-get install -y "$kernel_package=$kernel_version" "$debug_package=$debug_version"
  restart_required=1
fi
apt-get install -y "$debug_package"
if [ "${KEXPLORE_DOWNLOAD_SOURCE:-1}" = 1 ]; then
  bash "$(dirname "${BASH_SOURCE[0]}")/install-lab-source.sh" "$release"
else
  echo "Optional source download skipped (KEXPLORE_DOWNLOAD_SOURCE=0). Debug symbols remain required."
fi
apt-get install -y bpftrace || echo "Optional measurements unavailable."
echo 'kernel.sched_schedstats = 1' > /etc/sysctl.d/99-schedstats.conf
sysctl -w kernel.sched_schedstats=1
mkdir -p /var/lib/kexplore
printf '%s\n' "$release" > /var/lib/kexplore/kernel-release
# A distinct status asks the host to restart this dedicated lab, not the host.
if [ "$restart_required" = 1 ]; then
  exit 20
fi

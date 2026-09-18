#!/usr/bin/env bash
# Called by the dedicated lab provisioner, never on the macOS host.
set -euo pipefail
# shellcheck disable=SC1091
. /etc/os-release
[[ "$ID" = ubuntu || "$ID" = debian ]] && [ "${KEXPLORE_LAB_PROVISION:-}" = 1 ] && [ "$(id -u)" = 0 ] || exit 1
apt-get() { command apt-get -o DPkg::Lock::Timeout=300 "$@"; }
release="${1:-$(uname -r)}"
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# The signed image is built by linux-signed; its unsigned debug package
# records the actual kernel source package and exact distro revision.
if [ "$ID" = ubuntu ]; then
  debug_package="linux-image-unsigned-$release-dbgsym"
else
  debug_package="linux-image-$release-dbg"
fi
source_package="$(dpkg-query -W -f='${source:Package}' "$debug_package")"
source_version="$(dpkg-query -W -f='${source:Version}' "$debug_package")"
[[ "$source_package" =~ ^[a-z0-9.+-]+$ ]] && \
  [[ "$source_version" =~ ^[a-zA-Z0-9.+:~_-]+$ ]] || {
    echo "Cannot determine the exact kernel source revision." >&2; exit 1;
  }
destination="/var/lib/kexplore/sources/$source_package-$source_version"
config=/var/lib/kexplore/source.env
if [ -f "$config" ]; then
  # shellcheck disable=SC1090
  . "$config"
  if [ "${LAB_SOURCE_RELEASE:-}" = "$release" ] && \
    [ "${LAB_SOURCE_VERSION:-}" = "$source_version" ] && \
    [ -f "${LAB_SOURCE_ROOT:-}/kernel/sched/sched.h" ]; then
    echo "Matching source already installed: $source_package $source_version"
    exit 0
  fi
fi
echo "Installing exact $ID kernel source: $source_package $source_version"
echo "Source download and extraction can take several minutes and several GB of disk space."
echo "For setup without source downloads, set KEXPLORE_DOWNLOAD_SOURCE=0."
if [ "$ID" = ubuntu ]; then
cat > /etc/apt/sources.list.d/kexplore-source.sources <<EOF
Types: deb-src
URIs: http://archive.ubuntu.com/ubuntu
Suites: $VERSION_CODENAME $VERSION_CODENAME-updates
Components: main restricted universe multiverse
Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg

Types: deb-src
URIs: http://security.ubuntu.com/ubuntu
Suites: $VERSION_CODENAME-security
Components: main restricted universe multiverse
Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg
EOF
elif ! apt-cache showsrc --only-source "$source_package" >/dev/null 2>&1; then
cat > /etc/apt/sources.list.d/kexplore-source.sources <<EOF
Types: deb-src
URIs: https://deb.debian.org/debian
Suites: $VERSION_CODENAME $VERSION_CODENAME-updates $VERSION_CODENAME-backports
Components: main
Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg

Types: deb-src
URIs: https://deb.debian.org/debian-security
Suites: $VERSION_CODENAME-security
Components: main
Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg
EOF
fi
apt-get update
apt-get install -y dpkg-dev xz-utils
mkdir -p "$destination"
# A tree is published only after extraction and all distro patches succeed.
# A leftover directory from an interrupted attempt is never a cache hit.
staging="$(mktemp -d "$destination/.extract.XXXXXX")"
# This directory contains only this attempt's downloads and extraction.
trap 'rm -rf -- "$staging"' EXIT
cd "$staging"
if ! apt-get source --only-source "$source_package=$source_version"; then
  if [ "$ID" != ubuntu ]; then
    echo "Exact Debian source revision is unavailable; refusing to use another revision." >&2
    exit 1
  fi
  echo "Exact source revision is no longer in APT; fetching retained Ubuntu artifacts from Launchpad."
  python3 "$script_dir/fetch-ubuntu-source.py" "$source_package" "$source_version" "$VERSION_CODENAME"
fi
root="$(find "$staging" -mindepth 1 -maxdepth 1 -type d | while read -r candidate; do
  if [ -f "$candidate/kernel/sched/sched.h" ]; then
    printf '%s\n' "$candidate"; break
  fi
done)"
[ -n "$root" ] || { echo "Downloaded source does not contain the kernel tree." >&2; exit 1; }
# Keep completed trees separate from abandoned extraction attempts.
published="$destination/complete-$(basename "$staging")"
mv "$root" "$published"
root="$published"
# Map absolute build paths in DWARF into the extracted source tree.
declaration="$(pahole -C task_struct --show_decl_info "/usr/lib/debug/boot/vmlinux-$release")"
prefix="$(printf '%s\n' "$declaration" | sed -n 's@.* \(/[^ ]*\)/include/linux/sched.h:[0-9]*.*@\1@p' | head -1)"
if [ -z "$prefix" ]; then
  # Debian records paths relative to the source tree in this build.
  printf '%s\n' "$declaration" | grep -q ' include/linux/sched.h:' || {
    echo "Cannot determine this kernel's source build path." >&2; exit 1;
  }
  prefix="$root"
fi
{
  printf 'LAB_SOURCE_RELEASE=%q\n' "$release"
  printf 'LAB_SOURCE_VERSION=%q\n' "$source_version"
  printf 'LAB_SOURCE_ROOT=%q\n' "$root"
  printf 'LAB_SOURCE_PREFIX=%q\n' "$prefix"
} > "$config.tmp"
mv "$config.tmp" "$config"
echo "Matching kernel source configured automatically: $root"

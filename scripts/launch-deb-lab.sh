#!/usr/bin/env bash
# Use the VM's installed source defaults, while preserving explicit overrides.
set -euo pipefail
if [ -z "${KEXPLORE_SOURCE_ROOT:-}" ] && [ -f /var/lib/kexplore/source.env ]; then
  # shellcheck disable=SC1091
  . /var/lib/kexplore/source.env
  if [ "$LAB_SOURCE_RELEASE" = "$(uname -r)" ]; then
    export KEXPLORE_SOURCE_ROOT="$LAB_SOURCE_ROOT"
    export KEXPLORE_SOURCE_PREFIX="${KEXPLORE_SOURCE_PREFIX:-$LAB_SOURCE_PREFIX}"
    export KEXPLORE_INSTALLED_SOURCE_VERSION="${LAB_SOURCE_VERSION:-}"
  else
    echo "Kernel source configuration belongs to another kernel. Run ./setup.sh to install matching sources." >&2
  fi
elif [ -z "${KEXPLORE_SOURCE_ROOT:-}" ]; then
  # shellcheck disable=SC1091
  . /etc/os-release
  if [[ "$ID" = ubuntu || "$ID" = debian ]]; then
    echo "Kernel sources are not configured in this VM. Optional: run ./setup.sh with KEXPLORE_DOWNLOAD_SOURCE=1 to install them." >&2
  fi
fi
exec "$@"

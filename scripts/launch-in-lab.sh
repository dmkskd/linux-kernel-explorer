#!/usr/bin/env bash
# Runs inside a lima lab, whatever its distribution, in front of the command
# run.sh wants to start. It decides the two things only the guest can see: the
# installed kernel source defaults, and which interpreter holds the pinned
# Textual. Explicit overrides from the host win over both.
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
# Run in the lab's pinned environment when this VM has one. The choice is made
# here rather than in run.sh because run.sh cannot see the guest filesystem,
# and a VM provisioned before the venv existed still has to work: it keeps the
# distribution's python3.
if [ "${1:-}" = python3 ] && [ -x /opt/kexplore/bin/python3 ]; then
  set -- /opt/kexplore/bin/python3 "${@:2}"
fi
exec "$@"

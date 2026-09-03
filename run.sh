#!/usr/bin/env bash
# Launch kexplore inside the lima VM against its live kernel.
#
# The repo lives on the mac and is virtiofs-mounted into the VM read-only at
# the same path, so there is nothing to sync -- edit here, run there.
# PYTHONDONTWRITEBYTECODE is required because that mount is read-only.
#
#   ./run.sh              # the explorer
#   ./run.sh --check      # resolve every entry against this kernel, no UI
#   ./run.sh --test       # the test suite, in the VM where it can attach
set -euo pipefail

# Must match setup.sh, which is what creates it.
VM="${KEXPLORE_VM:-kernel-lab}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

TARGET=(python3 -m kexplore "$@")
if [[ "${1:-}" == "--test" ]]; then
  shift
  # Absolute: limactl shell starts in whatever directory it lands in.
  TARGET=(python3 "$REPO/tests/run_all.py" "$@")
fi

exec limactl shell "$VM" sudo env \
  PYTHONDONTWRITEBYTECODE=1 \
  PYTHONPATH="$REPO" \
  DEBUGINFOD_URLS="${DEBUGINFOD_URLS:-https://debuginfod.fedoraproject.org/}" \
  KEXPLORE_OFFLINE="${KEXPLORE_OFFLINE:-}" \
  TERM="${TERM:-xterm-256color}" \
  COLORTERM="${COLORTERM:-truecolor}" \
  "${TARGET[@]}"

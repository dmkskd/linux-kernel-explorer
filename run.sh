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
#   ./run.sh --help       # every option, and the environment it reads
set -euo pipefail

# Must match setup.sh, which is what creates it.
VM="${KEXPLORE_VM:-kernel-lab}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# The option list mirrors kexplore/__main__.py, which is what actually parses
# everything except --test. Printing it here means --help works with the VM
# down, at the cost of keeping the two in step.
usage() {
  cat <<USAGE
usage: ./run.sh [--test] [options]

Run kexplore inside the lima VM, as root, against that VM's live kernel.

  --test         run the test suite instead of the explorer; remaining
                 arguments go to tests/run_all.py
  --check        resolve every subsystem entry and report, without the UI
  --prefetch     download the kernel debuginfo to completion and exit
  --offline      never contact a debuginfod server; use only the cache
  -c, --core F   explore the vmcore F instead of the live kernel
  -h, --help     this message

Everything other than --test is passed through to kexplore.

Environment:

  KEXPLORE_VM        name of the lima VM (currently: $VM)
  KEXPLORE_OFFLINE   set to 1 for --offline without passing the flag
  DEBUGINFOD_URLS    where to fetch kernel debuginfo from
                     (default: https://debuginfod.fedoraproject.org/)

setup.sh reads KEXPLORE_VM too, so set it for both or neither.
USAGE
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

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

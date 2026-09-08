#!/usr/bin/env bash
# Launch kexplore against a live kernel, as root. Where it runs is resolved
# by detect.sh: a lima VM on a mac, the host itself on Linux when the tools
# are installed there, or a container reading the host kernel through
# /proc/kcore when they are not.
#
# setup.sh installs the machine; this script owns everything about the
# kernel's debug info. The download, the repair of a poisoned cache, and
# --prefetch/--offline all happen inside kexplore, which only this script
# starts.
#
#   ./run.sh              # the explorer
#   ./run.sh --check      # resolve every entry against this kernel, no UI
#   ./run.sh --test       # the test suite, somewhere it can attach
#   ./run.sh --help       # every option, and the environment it reads
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Sourced, not copied: setup.sh reads the same file and so resolves the
# same backend.
. "$REPO/detect.sh"

# lima backend. Must match setup.sh, which is what creates the VM.
VM="${KEXPLORE_VM:-kernel-lab}"
# docker backend: the image setup.sh builds, and the volume holding the
# debuginfod cache. A kernel DWARF is hundreds of MB with no resume, so an
# ephemeral container would otherwise re-download it on every run.
IMAGE=kexplore
CACHE_VOLUME=kexplore-debuginfod

# The option list mirrors kexplore/__main__.py, which is what actually parses
# everything except --test. Printing it here means --help works with no
# backend set up, at the cost of keeping the two in step.
usage() {
  cat <<USAGE
usage: ./run.sh [--test] [options]

Run kexplore as root against a live kernel. Where it runs:

  lima      a lima VM (the only option on macOS)
  native    this Linux host, with kexplore's packages installed
  docker    a container reading the host kernel through /proc/kcore

  --test         run the test suite instead of the explorer; remaining
                 arguments go to tests/run_all.py
  --check        resolve every subsystem entry and report, without the UI
  --prefetch     download the kernel debuginfo to completion and exit; a cold
                 first run does this anyway, so this is for preparing offline
  --no-prefetch  attach without downloading debuginfo to completion first
  --offline      never contact a debuginfod server; use only the cache
  -c, --core F   explore the vmcore F instead of the live kernel
  -h, --help     this message

Everything other than --test is passed through to kexplore.

Environment:

  KEXPLORE_BACKEND   pin the backend: lima, native or docker
                     (currently: ${KEXPLORE_BACKEND:-auto})
  KEXPLORE_VM        lima backend: name of the VM (currently: $VM)
  KEXPLORE_OFFLINE   set to 1 for --offline without passing the flag
  DEBUGINFOD_URLS    where to fetch the kernel's debug info from. Defaults
                     to Fedora's server for lima and docker, and to this
                     host's distro server for native

setup.sh reads the same variables, so set them for both or neither.
USAGE
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

TARGET=(python3 -m kexplore "$@")
if [[ "${1:-}" == "--test" ]]; then
  shift
  # Absolute: neither limactl shell nor a container starts in the repo.
  TARGET=(python3 "$REPO/tests/run_all.py" "$@")
fi

BACKEND="$(kexplore_backend)" || {
  echo "run ./setup.sh to set up a backend" >&2
  exit 1
}

WHY=detected
if [ -n "${KEXPLORE_BACKEND:-}" ]; then
  WHY="pinned by KEXPLORE_BACKEND"
fi
# Same label column as kexplore's own startup lines, so the whole preamble
# reads as one block.
case "$BACKEND" in
  lima)   printf '%-11s%s\n' backend "lima ($WHY, VM '$VM')" >&2 ;;
  docker) printf '%-11s%s\n' backend "docker ($WHY, image '$IMAGE')" >&2 ;;
  *)      printf '%-11s%s\n' backend "$BACKEND ($WHY)" >&2 ;;
esac

case "$BACKEND" in
  lima)
    if ! limactl list --quiet 2>/dev/null | grep -qx "$VM"; then
      echo "no lima VM named '$VM'. Run ./setup.sh to create it" >&2
      exit 1
    fi
    if [ "$(limactl list --format '{{.Status}}' "$VM" 2>/dev/null)" != Running ]; then
      echo "lima VM '$VM' is not running. Run ./setup.sh to start it" >&2
      exit 1
    fi
    # The repo is virtiofs-mounted into the VM read-only at the same path, so
    # there is nothing to sync. PYTHONDONTWRITEBYTECODE is required because
    # that mount is read-only.
    exec limactl shell "$VM" sudo env \
      PYTHONDONTWRITEBYTECODE=1 \
      PYTHONPATH="$REPO" \
      DEBUGINFOD_URLS="${DEBUGINFOD_URLS:-$(kexplore_debuginfod_for_backend lima)}" \
      KEXPLORE_OFFLINE="${KEXPLORE_OFFLINE:-}" \
      TERM="${TERM:-xterm-256color}" \
      COLORTERM="${COLORTERM:-truecolor}" \
      "${TARGET[@]}"
    ;;
  native)
    if ! kexplore_native_ready; then
      echo "the native backend needs drgn, textual and elfutils" >&2
      echo "run ./setup.sh to install them" >&2
      exit 1
    fi
    # No PYTHONDONTWRITEBYTECODE here: the repo is a normal writable
    # directory, and no TERM forwarding: there is no SSH layer in between.
    exec sudo env \
      PYTHONPATH="$REPO" \
      DEBUGINFOD_URLS="${DEBUGINFOD_URLS:-$(kexplore_debuginfod_for_backend native)}" \
      KEXPLORE_OFFLINE="${KEXPLORE_OFFLINE:-}" \
      "${TARGET[@]}"
    ;;
  docker)
    if ! RUNTIME="$(kexplore_container_runtime)"; then
      echo "the docker backend needs docker or rootful podman (rootless" >&2
      echo "podman cannot read /proc/kcore)" >&2
      exit 1
    fi
    TTY=()
    if [ -t 0 ] && [ -t 1 ]; then TTY=(-it); fi
    # The repo is mounted read-only at the same path, so paths in TARGET need
    # no translation and PYTHONDONTWRITEBYTECODE is back. The kernel flags
    # matter: both runtimes mask /proc/kcore to /dev/null by default, and
    # the read then succeeds returning nothing.
    # shellcheck disable=SC2086,SC2046
    exec $RUNTIME run --rm "${TTY[@]}" \
      $(kexplore_container_kernel_flags "$RUNTIME") \
      -v "$REPO:$REPO:ro" -w "$REPO" \
      -v "$CACHE_VOLUME:/root/.cache/debuginfod_client" \
      -e PYTHONDONTWRITEBYTECODE=1 \
      -e PYTHONPATH="$REPO" \
      -e DEBUGINFOD_URLS="${DEBUGINFOD_URLS:-$(kexplore_debuginfod_for_backend docker)}" \
      -e KEXPLORE_OFFLINE="${KEXPLORE_OFFLINE:-}" \
      -e TERM="${TERM:-xterm-256color}" \
      -e COLORTERM="${COLORTERM:-truecolor}" \
      "$IMAGE" "${TARGET[@]}"
    ;;
esac

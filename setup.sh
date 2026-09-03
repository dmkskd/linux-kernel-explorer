#!/usr/bin/env bash
# Create the lima VM kexplore runs against.
#
#   ./setup.sh              # create and provision (default name: kernel-lab)
#   KEXPLORE_VM=foo ./setup.sh
#
# Idempotent: if the VM exists it is started and re-verified rather than
# recreated.
set -euo pipefail

# Must match run.sh, which is what launches the explorer in it.
VM="${KEXPLORE_VM:-kernel-lab}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if ! command -v limactl >/dev/null; then
  echo "limactl not found. Install it with: brew install lima" >&2
  exit 1
fi

if limactl list --quiet 2>/dev/null | grep -qx "$VM"; then
  echo "VM '$VM' exists; starting it if needed."
  limactl start "$VM" >/dev/null
else
  echo "Creating VM '$VM' (downloads a Fedora cloud image on first run)."
  limactl start --name="$VM" "$REPO/lima/kexplore.yaml"
fi

echo
echo "Verifying prerequisites inside '$VM':"
limactl shell "$VM" sudo env \
  DEBUGINFOD_URLS="https://debuginfod.fedoraproject.org/" \
  bash -s <<'EOF'
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

# The real test: can drgn resolve kernel types? This needs debuginfod to serve
# DWARF for the running kernel, which is the whole reason for using Fedora.
python3 - <<'PY'
import sys
try:
    import drgn
    prog = drgn.program_from_kernel()
    print(f"  ok       drgn resolved struct task_struct ({drgn.sizeof(prog.type('struct task_struct'))} bytes)")
except Exception as exc:
    print(f"  FAILED   drgn could not resolve kernel types: {exc}")
    sys.exit(1)
PY
exit $(( fail + $? ))
EOF

echo
echo "Ready. Run the explorer with:  ./run.sh"

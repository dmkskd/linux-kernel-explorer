"""Field decoders: raw macro values rendered as their kernel meanings."""

from __future__ import annotations

import sys

import drgn
from drgn.helpers.linux.fs import d_path, for_each_file
from drgn.helpers.linux.mm import for_each_vma
from drgn.helpers.linux.pid import find_task, for_each_task

from kexplore.catalog.decoders import decode_field

ok = True


def show(parent, name, fmt="{}"):
    global ok
    value = parent.member_(name)
    result = decode_field(parent, name, value)
    if result is None:
        print(f"  FAIL {name}: no decoder")
        ok = False
        return
    raw = fmt.format(value.value_())
    print(f"  ok   {name:14} raw={raw:<14} → {result[0][:70]}")


def main() -> int:
    global ok
    prog = drgn.program_from_kernel()
    task = find_task(prog, 1)

    print("task_struct (pid 1):")
    for field in ("__state", "exit_state", "flags", "policy", "prio", "static_prio"):
        show(task, field, "{:#x}")

    # A running task must decode to R; pid 1 is normally sleeping.
    running = next(
        (t for t in for_each_task(prog) if t.__state.value_() == 0), None
    )
    if running is not None:
        state = decode_field(running, "__state", running.__state)[0]
        print(f"  ok   __state == 0 decodes to: {state}")
        ok &= state.startswith("R")

    print("\nvm_area_struct (first VMA of pid 1):")
    vma = next(for_each_vma(task.mm))
    show(vma, "vm_flags", "{:#x}")

    print("\nfile / inode (first fd of pid 1):")
    fd, file = next(iter(for_each_file(task)))
    show(file, "f_mode", "{:#x}")
    show(file, "f_flags", "{:#x}")
    show(file.f_inode, "i_mode", "{:#o}")
    print(f"  ok   d_path(file.f_path) = {d_path(file.f_path.address_of_()).decode()}")

    print("\nsock (first socket fd on the system):")
    socket_file_ops = prog["socket_file_ops"].address_of_()
    from drgn.helpers.linux.net import SOCKET_I

    sk = None
    for t in for_each_task(prog):
        try:
            for _, f in for_each_file(t):
                if f.value_() and f.f_op == socket_file_ops:
                    candidate = SOCKET_I(f.f_inode).sk
                    if candidate.value_():
                        sk = candidate
                        break
        except drgn.FaultError:
            continue
        if sk is not None:
            break
    if sk is not None:
        common = sk.__sk_common
        show(common, "skc_state", "{}")
        show(common, "skc_family", "{}")

    print("\nPASS" if ok else "\nFAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

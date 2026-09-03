"""Create the clone scenarios so they can be inspected from the kernel side.

Holds four live tasks that differ only in what they share:

  * the parent
  * a thread      (CLONE_VM | CLONE_FILES | CLONE_FS | CLONE_SIGHAND | CLONE_THREAD)
  * a fork child  (no sharing; the address space is duplicated copy-on-write)
  * a vfork child would suspend the parent, so it is not held open here

The child deliberately does not exec, so its copy-on-write address space is
still shared with the parent and can be observed.

Run it detached, or it dies with the shell that started it:

    setsid nohup python3 tests/helpers/clone_demo.py 600 >/tmp/clone.log 2>&1 </dev/null &
"""

from __future__ import annotations

import ctypes
import os
import sys
import threading
import time

SECONDS = int(sys.argv[1]) if len(sys.argv) > 1 else 600

# A page of anonymous memory both parent and child will map. After fork it is
# shared copy-on-write until one of them writes.
SHARED = ctypes.create_string_buffer(b"kexplore-cow-demo" + b"\0" * 8175)


def spin() -> None:
    deadline = time.time() + SECONDS
    while time.time() < deadline:
        time.sleep(0.5)


thread = threading.Thread(target=spin, name="kexplore-thread", daemon=True)
thread.start()

child = os.fork()
if child == 0:
    # Do not exec: keep the inherited address space so COW stays observable.
    spin()
    os._exit(0)

address = ctypes.addressof(SHARED)
print(f"parent pid  {os.getpid()}")
print(f"fork child  {child}")
print(f"shared buffer at {address:#x} ({len(SHARED)} bytes)")
print(f"holding for {SECONDS}s")
sys.stdout.flush()

spin()
os.kill(child, 15)

"""Workload that creates threads and forks, so their cost can be compared.

Both paths reach kernel_clone(); the flags decide how much work it does. Run
this while a measurement is attached.

    python3 tests/helpers/clone_cost.py [rounds] [mb]

``mb`` sizes a dirty anonymous buffer before forking, because the parent's
resident size is what fork has to duplicate page tables for.
"""

from __future__ import annotations

import os
import sys
import threading
import time

ROUNDS = int(sys.argv[1]) if len(sys.argv) > 1 else 200
MB = int(sys.argv[2]) if len(sys.argv) > 2 else 64

# Touch every page so it is resident: fork must copy page tables for all of it.
ballast = bytearray(MB * 1024 * 1024)
for offset in range(0, len(ballast), 4096):
    ballast[offset] = 1

print(f"resident ballast: {MB} MiB, {ROUNDS} rounds each of fork and thread")
sys.stdout.flush()

start = time.perf_counter()
for _ in range(ROUNDS):
    pid = os.fork()
    if pid == 0:
        os._exit(0)
    os.waitpid(pid, 0)
fork_seconds = time.perf_counter() - start

start = time.perf_counter()
for _ in range(ROUNDS):
    thread = threading.Thread(target=lambda: None)
    thread.start()
    thread.join()
thread_seconds = time.perf_counter() - start

print(f"fork+wait:   {fork_seconds / ROUNDS * 1e6:8.1f} us per round")
print(f"thread+join: {thread_seconds / ROUNDS * 1e6:8.1f} us per round")

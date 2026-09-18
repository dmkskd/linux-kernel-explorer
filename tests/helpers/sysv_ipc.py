"""Create System V IPC objects and keep them busy, so the ipc branch has data.

An IPC object outlives its creator, which is why an idle machine can hold a
message queue whose last sender exited months ago. Three states do not
outlive it: a message sitting unreceived, a task blocked in semop(2), and a
segment with an attachment. This holds all three open for as long as it runs.

glibc's msgget/semget/shmget are called through ctypes, so the helper needs
only the standard library.

Run it detached, or it dies with the shell that started it:

    setsid nohup python3 tests/helpers/sysv_ipc.py 1800 >/tmp/ipc.log 2>&1 </dev/null &

then browse ipc > message queues, semaphore arrays, shared memory segments.
It removes what it created on the way out. After a SIGKILL the objects stay
behind, and ipcrm removes them.
"""

from __future__ import annotations

import ctypes
import os
import signal
import struct
import sys
import time

SECONDS = int(sys.argv[1]) if len(sys.argv) > 1 else 300


def _leave(signum, frame):
    """Exit through the removal code rather than around it.

    Python's default SIGTERM handling ends the process immediately, so a
    finally clause never runs and the objects are left behind for ipcrm.
    Raising SystemExit instead unwinds normally.
    """
    raise SystemExit(0)


signal.signal(signal.SIGTERM, _leave)
signal.signal(signal.SIGINT, _leave)

IPC_PRIVATE = 0
IPC_RMID = 0
IPC_CREAT = 0o1000
MODE = 0o600

libc = ctypes.CDLL(None, use_errno=True)


def call(function, *args):
    result = function(*args)
    if result == -1:
        errno = ctypes.get_errno()
        raise OSError(errno, os.strerror(errno), function.__name__)
    return result


# --- a message queue holding an unreceived message ------------------------
# msgsnd takes a caller-defined struct: a long type, then the payload. The
# type is what a receiver selects on, so it is part of the message, not a
# header the kernel adds.
msqid = call(libc.msgget, IPC_PRIVATE, IPC_CREAT | MODE)
payload = b"kexplore-test-message"
buffer = struct.pack("l", 1) + payload
call(libc.msgsnd, msqid, buffer, len(payload), 0)

# --- a semaphore array with a child blocked on it -------------------------
# semget gives an array of values, all zero. The child asks to decrement one
# of them, which cannot be applied while it is zero, so the child parks on
# the array's pending_alter list until this process exits.
semid = call(libc.semget, IPC_PRIVATE, 2, IPC_CREAT | MODE)


class Sembuf(ctypes.Structure):
    _fields_ = [
        ("sem_num", ctypes.c_ushort),
        ("sem_op", ctypes.c_short),
        ("sem_flg", ctypes.c_short),
    ]


blocked = os.fork()
if blocked == 0:
    # -1 on a semaphore that is 0: blocks until someone posts.
    operation = Sembuf(0, -1, 0)
    try:
        libc.semop(semid, ctypes.byref(operation), 1)
    except Exception:  # noqa: BLE001 - the child has nothing to report to
        pass
    os._exit(0)

# --- a shared memory segment, attached -----------------------------------
# shmat's return value is a pointer, not an int: say so, or a 64-bit address
# comes back truncated and the detach fails.
libc.shmat.restype = ctypes.c_void_p
shmid = call(libc.shmget, IPC_PRIVATE, 4096, IPC_CREAT | MODE)
address = libc.shmat(shmid, None, 0)
if address == ctypes.c_void_p(-1).value:
    raise OSError("shmat failed")

print(
    f"msqid {msqid} (1 message queued), semid {semid} (pid {blocked} blocked "
    f"in semop), shmid {shmid} (attached at {address:#x}); holding {SECONDS}s"
)
sys.stdout.flush()

try:
    time.sleep(SECONDS)
finally:
    libc.shmdt(ctypes.c_void_p(address))
    libc.msgctl(msqid, IPC_RMID, None)
    libc.shmctl(shmid, IPC_RMID, None)
    # Removing the array wakes the blocked child with EIDRM.
    libc.semctl(semid, 0, IPC_RMID, 0)
    os.waitpid(blocked, 0)

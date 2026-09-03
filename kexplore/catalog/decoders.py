"""Human-readable meanings for well-known raw fields.

``task_struct.__state`` shows as ``0`` because task states are ``#define``s,
not enums -- and the C preprocessor runs long before DWARF is emitted, so
there is nothing in the debug info to decode against. Fields whose type is a
real ``enum`` already render by name; everything here is the macro case.

That means these tables are hand-maintained kernel knowledge, with the usual
risk: a value renumbered upstream would decode wrongly and silently. So
decoders prefer a drgn helper whenever one exists (``get_task_state``,
``decode_page_flags``), and the raw value is always still displayed next to the
decoded text.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from drgn import Object
from drgn.helpers.common.format import decode_flags
from drgn.helpers.linux.mm import decode_page_flags
from drgn.helpers.linux.sched import get_task_state

from ..core import ctypes as ct


@dataclass(frozen=True)
class Decoder:
    doc: str
    decode: Callable[[Object, Object], str]


def _flags(pairs: tuple[tuple[str, int], ...], zero: str = "none"):
    """A decoder for a bitmask field, given (name, mask) pairs.

    ``decode_flags`` renders an empty mask as "0", which reads like a value
    rather than an absence, so zero gets its own wording.
    """

    def decode(parent: Object, value: Object) -> str:
        raw = value.value_()
        return zero if raw == 0 else decode_flags(raw, pairs, bit_numbers=False)

    return decode


def _lookup(table: dict[int, str], unknown: str = "?"):
    """A decoder for a field that holds one value from a known set."""
    return lambda parent, value: table.get(value.value_(), unknown)


# ---------------------------------------------------------------- task_struct

# include/linux/sched.h. Only the stable, commonly-seen flags -- decode_flags
# renders anything unrecognised as a bit number, so gaps are visible.
PF_FLAGS = (
    ("PF_VCPU", 0x00000001),
    ("PF_IDLE", 0x00000002),
    ("PF_EXITING", 0x00000004),
    ("PF_POSTCOREDUMP", 0x00000008),
    ("PF_IO_WORKER", 0x00000010),
    ("PF_WQ_WORKER", 0x00000020),
    ("PF_FORKNOEXEC", 0x00000040),
    ("PF_SUPERPRIV", 0x00000100),
    ("PF_DUMPCORE", 0x00000200),
    ("PF_SIGNALED", 0x00000400),
    ("PF_MEMALLOC", 0x00000800),
    ("PF_USED_MATH", 0x00002000),
    ("PF_USER_WORKER", 0x00004000),
    ("PF_NOFREEZE", 0x00008000),
    ("PF_KSWAPD", 0x00020000),
    ("PF_MEMALLOC_NOFS", 0x00040000),
    ("PF_MEMALLOC_NOIO", 0x00080000),
    ("PF_LOCAL_THROTTLE", 0x00100000),
    ("PF_KTHREAD", 0x00200000),
    ("PF_RANDOMIZE", 0x00400000),
    ("PF_NO_SETAFFINITY", 0x04000000),
    ("PF_MEMALLOC_PIN", 0x10000000),
    ("PF_SUSPEND_TASK", 0x80000000),
)

EXIT_STATES = (("EXIT_DEAD", 0x0010), ("EXIT_ZOMBIE", 0x0020))

SCHED_POLICIES = {
    0: "SCHED_NORMAL (CFS/EEVDF)",
    1: "SCHED_FIFO (realtime)",
    2: "SCHED_RR (realtime)",
    3: "SCHED_BATCH",
    5: "SCHED_IDLE",
    6: "SCHED_DEADLINE",
    7: "SCHED_EXT (BPF)",
}

# MAX_RT_PRIO: priorities 0-99 are realtime, 100-139 map to nice -20..19.
MAX_RT_PRIO = 100
DEFAULT_PRIO = 120


def _priority(parent: Object, value: Object) -> str:
    prio = value.value_()
    if prio < MAX_RT_PRIO:
        return f"realtime priority {MAX_RT_PRIO - 1 - prio}"
    return f"nice {prio - DEFAULT_PRIO}"


# ------------------------------------------------------------------------ mm

VM_FLAGS = (
    ("VM_READ", 0x00000001),
    ("VM_WRITE", 0x00000002),
    ("VM_EXEC", 0x00000004),
    ("VM_SHARED", 0x00000008),
    ("VM_MAYREAD", 0x00000010),
    ("VM_MAYWRITE", 0x00000020),
    ("VM_MAYEXEC", 0x00000040),
    ("VM_MAYSHARE", 0x00000080),
    ("VM_GROWSDOWN", 0x00000100),
    ("VM_LOCKED", 0x00002000),
    ("VM_IO", 0x00004000),
    ("VM_SEQ_READ", 0x00008000),
    ("VM_RAND_READ", 0x00010000),
    ("VM_DONTCOPY", 0x00020000),
    ("VM_DONTEXPAND", 0x00040000),
    ("VM_ACCOUNT", 0x00100000),
    ("VM_NORESERVE", 0x00200000),
    ("VM_HUGETLB", 0x00400000),
    ("VM_DONTDUMP", 0x04000000),
)


def _vm_flags(parent: Object, value: Object) -> str:
    """Prefix the decoded flags with the rwxp permission triple."""
    raw = value.value_()
    perms = "".join(
        letter if raw & bit else "-"
        for letter, bit in (("r", 0x1), ("w", 0x2), ("x", 0x4))
    )
    perms += "s" if raw & 0x8 else "p"
    return f"{perms}  {decode_flags(raw, VM_FLAGS, bit_numbers=False)}"


# ----------------------------------------------------------------------- vfs

S_IFMT = 0o170000
FILE_TYPES = {
    0o140000: "socket",
    0o120000: "symlink",
    0o100000: "regular file",
    0o060000: "block device",
    0o040000: "directory",
    0o020000: "char device",
    0o010000: "fifo",
}


def _i_mode(parent: Object, value: Object) -> str:
    mode = value.value_()
    kind = FILE_TYPES.get(mode & S_IFMT, "unknown")
    return f"{kind}, mode {mode & 0o7777:04o}"


O_FLAGS = (
    ("O_WRONLY", 0o1),
    ("O_RDWR", 0o2),
    ("O_CREAT", 0o100),
    ("O_EXCL", 0o200),
    ("O_NOCTTY", 0o400),
    ("O_TRUNC", 0o1000),
    ("O_APPEND", 0o2000),
    ("O_NONBLOCK", 0o4000),
    ("O_DSYNC", 0o10000),
    ("O_DIRECT", 0o200000),
    ("O_LARGEFILE", 0o400000),
    ("O_DIRECTORY", 0o40000),
    ("O_NOFOLLOW", 0o100000),
    ("O_CLOEXEC", 0o2000000),
)

FMODE_FLAGS = (
    ("FMODE_READ", 0x0001),
    ("FMODE_WRITE", 0x0002),
    ("FMODE_LSEEK", 0x0004),
    ("FMODE_PREAD", 0x0008),
    ("FMODE_PWRITE", 0x0010),
    ("FMODE_EXEC", 0x0020),
    ("FMODE_NDELAY", 0x0040),
    ("FMODE_EXCL", 0x0080),
)

SB_FLAGS = (
    ("SB_RDONLY", 1),
    ("SB_NOSUID", 2),
    ("SB_NODEV", 4),
    ("SB_NOEXEC", 8),
    ("SB_SYNCHRONOUS", 16),
    ("SB_MANDLOCK", 64),
    ("SB_DIRSYNC", 128),
    ("SB_NOATIME", 1024),
    ("SB_NODIRATIME", 2048),
    ("SB_SILENT", 32768),
    ("SB_POSIXACL", 1 << 16),
    ("SB_LAZYTIME", 1 << 25),
)

# --------------------------------------------------------------------- socket

TCP_STATES = {
    1: "TCP_ESTABLISHED",
    2: "TCP_SYN_SENT",
    3: "TCP_SYN_RECV",
    4: "TCP_FIN_WAIT1",
    5: "TCP_FIN_WAIT2",
    6: "TCP_TIME_WAIT",
    7: "TCP_CLOSE",
    8: "TCP_CLOSE_WAIT",
    9: "TCP_LAST_ACK",
    10: "TCP_LISTEN",
    11: "TCP_CLOSING",
    12: "TCP_NEW_SYN_RECV",
}

ADDRESS_FAMILIES = {
    0: "AF_UNSPEC",
    1: "AF_UNIX",
    2: "AF_INET",
    10: "AF_INET6",
    16: "AF_NETLINK",
    17: "AF_PACKET",
    40: "AF_VSOCK",
    44: "AF_XDP",
}

IP_PROTOCOLS = {0: "IPPROTO_IP", 1: "ICMP", 6: "TCP", 17: "UDP", 132: "SCTP", 136: "UDPLITE"}

# ----------------------------------------------------------------------- slab

SLAB_FLAGS = (
    ("SLAB_CONSISTENCY_CHECKS", 0x00000100),
    ("SLAB_RED_ZONE", 0x00000400),
    ("SLAB_POISON", 0x00000800),
    ("SLAB_HWCACHE_ALIGN", 0x00002000),
    ("SLAB_CACHE_DMA", 0x00004000),
    ("SLAB_STORE_USER", 0x00010000),
    ("SLAB_PANIC", 0x00040000),
    ("SLAB_TYPESAFE_BY_RCU", 0x00080000),
    ("SLAB_ACCOUNT", 0x04000000),
)


DECODERS: dict[tuple[str, str], Decoder] = {
    ("task_struct", "__state"): Decoder(
        "Task state. TASK_RUNNING is 0; these are #defines, so absent from DWARF.",
        lambda parent, value: get_task_state(parent),
    ),
    ("task_struct", "exit_state"): Decoder(
        "EXIT_ZOMBIE: dead but not reaped. EXIT_DEAD: being removed.",
        _flags(EXIT_STATES, zero="not exited"),
    ),
    ("task_struct", "flags"): Decoder("PF_* per-task flags.", _flags(PF_FLAGS)),
    ("task_struct", "policy"): Decoder("Scheduling policy.", _lookup(SCHED_POLICIES)),
    ("task_struct", "prio"): Decoder("Effective priority.", _priority),
    ("task_struct", "static_prio"): Decoder("Priority from nice, ignoring boosts.", _priority),
    ("task_struct", "normal_prio"): Decoder("Priority without PI boosting.", _priority),
    ("vm_area_struct", "vm_flags"): Decoder(
        "Mapping permissions and behaviour flags.", _vm_flags
    ),
    ("inode", "i_mode"): Decoder("File type and permission bits.", _i_mode),
    ("file", "f_flags"): Decoder("open(2) flags.", _flags(O_FLAGS)),
    ("file", "f_mode"): Decoder("FMODE_* access mode.", _flags(FMODE_FLAGS)),
    ("super_block", "s_flags"): Decoder("Mount flags (SB_*).", _flags(SB_FLAGS)),
    ("page", "flags"): Decoder(
        "PG_* page flags.", lambda parent, value: decode_page_flags(parent)
    ),
    ("sock_common", "skc_state"): Decoder(
        "TCP state machine; also used by other protocols.", _lookup(TCP_STATES)
    ),
    ("sock_common", "skc_family"): Decoder("Address family.", _lookup(ADDRESS_FAMILIES)),
    ("sock", "sk_protocol"): Decoder("IP protocol number.", _lookup(IP_PROTOCOLS)),
    ("kmem_cache", "flags"): Decoder("SLAB_* cache flags.", _flags(SLAB_FLAGS)),
}


def decode_field(parent: Object, name: str, value: Object) -> tuple[str, str] | None:
    """Decoded text and its explanation for ``parent.name``, if we know it.

    ``parent`` is passed as well as the field because some decoders need the
    whole object -- ``get_task_state()`` reads more than ``__state`` alone.
    """
    tag = ct.tag_of(parent.type_)
    if tag is None:
        return None
    decoder = DECODERS.get((tag, name))
    if decoder is None:
        return None
    text = ct.safe(lambda: str(decoder.decode(parent, value)), None)
    if text is None or not text:
        return None
    return text, decoder.doc

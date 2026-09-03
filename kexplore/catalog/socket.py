"""Sockets.

Unlike tasks, there is no single global list of sockets. They're reachable
from three different directions, and which one you want depends on the
question:

  * **From a process** -- a socket with an fd is a ``struct file`` whose
    ``f_op`` is ``socket_file_ops``. Its inode is embedded in a
    ``struct socket_alloc``, so ``SOCKET_I()`` walks back to the
    ``struct socket``. This is the view that answers "who owns this".

  * **From the protocol hash tables** -- per-netns TCP ehash/lhash2 and the
    UDP table. This is the only way to see sockets with *no* fd: TIME_WAIT,
    orphans, and anything whose owner already exited.

  * **From the unix table** -- ``net->unx.table.buckets``, since AF_UNIX has
    its own hashing entirely.

Layering note: ``struct socket`` is the VFS-facing half and ``struct sock``
the protocol half; ``socket->sk`` crosses between them, and ``sock`` is the
first member of ``inet_sock``/``tcp_sock``, so those are casts rather than
pointer hops.
"""

from __future__ import annotations

from drgn import Program
from drgn.helpers.linux.list import hlist_for_each_entry
from drgn.helpers.linux.net import SOCKET_I, sk_nulls_for_each

from ..core import ctypes as ct
from .registry import Entry, Subsystem, register
from .format import task_comm
from .walk import socket_files

# AF_UNIX bucket count (UNIX_HASH_SIZE); the table has no mask field to read.
UNIX_HASH_SIZE = 256


def process_sockets(prog: Program):
    """struct socket per open socket fd, labelled with its owner."""
    for task, fd, file in socket_files(prog):
        sock = SOCKET_I(file.f_inode)
        family = ct.safe(lambda: sock.sk.__sk_common.skc_family.value_(), "?")
        yield f"{task_comm(task)}[{task.pid.value_()}] fd {fd}  family {family}", sock


def process_socks(prog: Program):
    """The struct sock behind each socket fd -- the protocol-level object."""
    for task, fd, file in socket_files(prog):
        sk = SOCKET_I(file.f_inode).sk
        if sk.value_():
            yield f"{task_comm(task)}[{task.pid.value_()}] fd {fd}", sk


def _hashinfo(prog: Program):
    """Per-netns TCP hash table (global tcp_hashinfo is gone in modern kernels)."""
    return prog["init_net"].ipv4.tcp_death_row.hashinfo


def tcp_listening(prog: Program):
    hashinfo = _hashinfo(prog)
    for index in range(hashinfo.lhash2_mask.value_() + 1):
        for sk in sk_nulls_for_each(hashinfo.lhash2[index].nulls_head):
            port = sk.__sk_common.skc_num.value_()
            yield f"listen :{port}", sk


def tcp_established(prog: Program):
    hashinfo = _hashinfo(prog)
    for index in range(hashinfo.ehash_mask.value_() + 1):
        for sk in sk_nulls_for_each(hashinfo.ehash[index].chain):
            common = sk.__sk_common
            yield (
                f":{common.skc_num.value_()} → :{ct.safe(lambda: common.skc_dport.value_(), '?')}",
                sk,
            )


def udp_sockets(prog: Program):
    """UDP hash table.

    ``udp_hslot``'s first member is a union of ``head`` and ``nulls_head``;
    this kernel links them through the plain ``hlist_head``/``sk_node`` side.
    """
    table = prog["init_net"].ipv4.udp_table
    for index in range(table.mask.value_() + 1):
        slot = table.hash[index]
        for sk in hlist_for_each_entry(
            "struct sock", slot.head.address_of_(), "__sk_common.skc_node"
        ):
            yield f"udp :{sk.__sk_common.skc_num.value_()}", sk


def unix_sockets(prog: Program):
    buckets = prog["init_net"].unx.table.buckets
    for index in range(UNIX_HASH_SIZE):
        for sk in hlist_for_each_entry(
            "struct sock", buckets[index].address_of_(), "__sk_common.skc_node"
        ):
            yield f"unix inode {ct.safe(lambda: sk.sk_socket.file.f_inode.i_ino.value_(), '?')}", sk


register(
    Subsystem(
        key="socket",
        label="socket",
        doc="Sockets from three directions: process fds, protocol hashes, AF_UNIX.",
        entries=[
            Entry(
                "process_sockets",
                "sockets by process (struct socket)",
                "Every socket fd; the VFS-facing half. Follow 'sk' to cross over.",
                process_sockets,
            ),
            Entry(
                "process_socks",
                "sockets by process (struct sock)",
                "The protocol half of each socket fd -- cast to inet_sock/tcp_sock.",
                process_socks,
            ),
            Entry(
                "tcp_listen",
                "TCP listening",
                "Per-netns lhash2 table: sockets in LISTEN.",
                tcp_listening,
            ),
            Entry(
                "tcp_estab",
                "TCP established",
                "The ehash table -- includes sockets with no owning process.",
                tcp_established,
            ),
            Entry("udp", "UDP sockets", "The per-netns UDP hash table.", udp_sockets),
            Entry("unix", "unix sockets", "AF_UNIX has its own hash: net->unx.table.", unix_sockets),
        ],
    )
)

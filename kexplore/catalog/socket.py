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
from .format import task_comm
from .registry import Entry, Subsystem, register
from .walk import socket_files

# AF_UNIX bucket count (UNIX_HASH_SIZE); the table has no mask field to read.
UNIX_HASH_SIZE = 256


def _family_names(prog: Program) -> list[str]:
    """Address family names, read from the kernel's pf_family_names.

    "PF_UNIX/PF_LOCAL" becomes "unix": the first spelling, without the prefix.
    Empty when the array is not in this kernel's debug info.
    """
    try:
        names = prog["pf_family_names"]
        return [
            ct.safe(lambda n=name: n.string_().decode(), "")
            .split("/")[0]
            .removeprefix("PF_")
            .lower()
            for name in names
        ]
    except (KeyError, LookupError):
        return []


def _sock_kind(sk, families: list[str]) -> str:
    """Family and type of a struct sock, as in "inet6 stream"."""
    family = ct.safe(lambda: sk.__sk_common.skc_family.value_(), None)
    if family is None:
        return "?"
    name = families[family] if family < len(families) and families[family] else ""
    kind = name or f"family {family}"
    sock_type = ct.safe(lambda: sk.sk_type.value_(), None)
    type_name = ct.safe(
        lambda: next(
            e.name for e in sk.prog_.type("enum sock_type").enumerators
            if e.value == sock_type
        ),
        None,
    )
    if type_name:
        kind += " " + type_name.removeprefix("SOCK_").lower()
    return kind


def process_sockets(prog: Program):
    """struct socket per open socket fd, labelled with its owner."""
    families = _family_names(prog)
    for task, fd, file in socket_files(prog):
        sock = SOCKET_I(file.f_inode)
        kind = _sock_kind(sock.sk, families) if sock.sk.value_() else "no sock"
        yield f"{task_comm(task)}[{task.pid.value_()}] fd {fd}  {kind}", sock


def process_socks(prog: Program):
    """The struct sock behind each socket fd -- the protocol-level object."""
    families = _family_names(prog)
    for task, fd, file in socket_files(prog):
        sk = SOCKET_I(file.f_inode).sk
        if sk.value_():
            yield f"{task_comm(task)}[{task.pid.value_()}] fd {fd}  {_sock_kind(sk, families)}", sk


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
        parent="net",
        doc="Socket state indexed by process descriptors, protocol hash tables, and AF_UNIX tables.",
        entries=[
            Entry(
                "process_sockets",
                "sockets by process (struct socket)",
                "VFS struct socket instances referenced by process file descriptors.",
                process_sockets,
            ),
            Entry(
                "process_socks",
                "sockets by process (struct sock)",
                "Protocol-layer struct sock instances referenced by struct socket->sk.",
                process_socks,
            ),
            Entry(
                "tcp_listen",
                "TCP listening",
                "TCP_LISTEN sockets in each network namespace's lhash2 table.",
                tcp_listening,
            ),
            Entry(
                "tcp_estab",
                "TCP established",
                "Sockets in each network namespace's TCP ehash, including unowned sockets.",
                tcp_established,
            ),
            Entry("udp", "UDP sockets", "Sockets in each network namespace's UDP hash table.", udp_sockets),
            Entry("unix", "UNIX sockets", "Sockets in net->unx.table for each network namespace.", unix_sockets),
        ],
    )
)

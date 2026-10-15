"""Packet buffers.

An skb in flight through the network stack cannot be browsed: it exists for
microseconds, is referenced from a kernel stack or a per-CPU variable, and is
freed before drgn can read it. The kernel keeps no global list of live skbs,
and drgn on a live kernel is not synchronised with packet processing.

The skbs that can be browsed are those queued at one of three points:

  * **Socket queues**: ``sk_receive_queue`` holds data that has arrived but
    not been read, and ``sk_write_queue`` holds data sent but not
    acknowledged. A socket that is never read keeps its received skbs queued
    indefinitely.
  * **Per-CPU backlog**: ``softnet_data.input_pkt_queue`` and
    ``process_queue``, where RPS and NAPI queue packets between the driver and
    the protocol stack. Both are usually empty on an idle system.
  * **Qdisc queues**: packets waiting to be transmitted on a device.

Packets in flight require tracing instead, for example a kprobe on
``netif_receive_skb`` or ``__netif_receive_skb_core``.

A ``sk_buff_head`` is a circular doubly linked list whose head is not an skb,
so a walk ends when ``next`` points back to the head's address.
"""

from __future__ import annotations

import drgn
from drgn import Object, Program
from drgn.helpers.linux.cpumask import for_each_online_cpu
from drgn.helpers.linux.net import SOCKET_I, for_each_netdev, netdev_name
from drgn.helpers.linux.percpu import per_cpu

from ..core import ctypes as ct
from .format import as_text, task_comm
from .registry import Entry, Subsystem, register
from .walk import socket_files

MAX_SKBS = 64


def skb_list(head: Object):
    """Walk a struct sk_buff_head.

    ``head`` is the list anchor, not an skb; the walk stops when it returns to
    the anchor.
    """
    anchor = head.value_() if head.type_.kind == drgn.TypeKind.POINTER else head.address_
    node = head.next
    seen = 0
    while node.value_() and node.value_() != anchor and seen < MAX_SKBS:
        yield node
        node = node.next
        seen += 1


def qdisc_skb_list(head: Object):
    """Walk a struct qdisc_skb_head.

    A qdisc keeps an explicit head/tail NULL-terminated chain, where
    sk_buff_head is a circular list, so the two are walked differently.
    """
    node = head.head
    seen = 0
    while node.value_() and seen < MAX_SKBS:
        yield node
        node = node.next
        seen += 1


def _socket_socks(prog: Program):
    """(task, fd, sock) for every socket fd on the system.

    The protocol half. A socket whose sk is NULL is being torn down and has no
    queues to walk.
    """
    for task, fd, file in socket_files(prog):
        sk = SOCKET_I(file.f_inode).sk
        if sk.value_():
            yield task, fd, sk


def _queued(prog: Program, queue_name: str):
    """skbs sitting in a named socket queue, across all sockets."""
    for task, fd, sk in _socket_socks(prog):
        queue = ct.safe(lambda: sk.member_(queue_name), None)
        if queue is None or not ct.safe(lambda: queue.qlen.value_(), 0):
            continue
        owner = f"{task_comm(task)}[{task.pid.value_()}] fd {fd}"
        for index, skb in enumerate(skb_list(queue.address_of_())):
            yield f"{owner} #{index}  len {skb.len.value_()}", skb


def receive_queues(prog: Program):
    """Data that has arrived but not yet been read by userspace."""
    yield from _queued(prog, "sk_receive_queue")


def write_queues(prog: Program):
    """Data handed to the kernel but not yet fully sent/acked."""
    yield from _queued(prog, "sk_write_queue")


def backlog(prog: Program):
    """Per-CPU softnet backlog: between the driver and the protocol stack."""
    for cpu in for_each_online_cpu(prog):
        sd = per_cpu(prog["softnet_data"], cpu)
        for name in ("input_pkt_queue", "process_queue"):
            queue = sd.member_(name)
            for index, skb in enumerate(skb_list(queue.address_of_())):
                yield f"cpu{cpu} {name} #{index}  len {skb.len.value_()}", skb


def qdisc_queues(prog: Program):
    """Packets waiting to be transmitted on each device queue."""
    for dev in for_each_netdev(prog["init_net"].address_of_()):
        name = as_text(netdev_name(dev))
        for index in range(dev.num_tx_queues.value_()):
            qdisc = dev._tx[index].qdisc
            if not qdisc.value_():
                continue
            for position, skb in enumerate(qdisc_skb_list(qdisc.q)):
                yield f"{name} tx{index} #{position}  len {skb.len.value_()}", skb


def nonempty_queues(prog: Program):
    """Every socket queue that is not empty."""
    for task, fd, sk in _socket_socks(prog):
        for name in ("sk_receive_queue", "sk_write_queue", "sk_error_queue"):
            queue = ct.safe(lambda: sk.member_(name), None)
            qlen = ct.safe(lambda: queue.qlen.value_(), 0) if queue is not None else 0
            if qlen:
                yield (
                    f"{task_comm(task)}[{task.pid.value_()}] fd {fd} {name}: {qlen}",
                    queue.address_of_(),
                )


register(
    Subsystem(
        key="skb",
        label="skb",
        parent="net",
        doc="Queued struct sk_buff instances; packets outside queues require tracing.",
        entries=[
            Entry(
                "nonempty",
                "non-empty socket queues",
                "Socket queues with qlen > 0.",
                nonempty_queues,
            ),
            Entry(
                "receive",
                "socket receive queues",
                "struct sk_buff instances on sk_receive_queue.",
                receive_queues,
            ),
            Entry(
                "write",
                "socket write queues",
                "struct sk_buff instances on sk_write_queue.",
                write_queues,
            ),
            Entry(
                "backlog",
                "per-cpu backlog",
                "softnet_data input_pkt_queue and process_queue.",
                backlog,
            ),
            Entry(
                "qdisc",
                "qdisc transmit queues",
                "Packets queued for transmission per device tx queue.",
                qdisc_queues,
            ),
        ],
    )
)

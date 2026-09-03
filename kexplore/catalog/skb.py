"""Packet buffers.

Packets in flight are not browsable. An skb being processed by the network
stack exists for microseconds, lives on a kernel stack or in a CPU-local
variable, and is gone before you can look at it. There is no global list of
live skbs, and drgn on a live kernel is not synchronised with packet
processing anyway.

What you *can* browse is skbs that are sitting still, queued at a choke point:

  * **Socket queues** -- ``sk_receive_queue`` holds data that has arrived but
    not been read; ``sk_write_queue`` holds data sent but not acknowledged.
    These are the easiest to catch, because a socket nobody is reading will
    hold its skbs indefinitely.
  * **Per-CPU backlog** -- ``softnet_data.input_pkt_queue`` and
    ``process_queue``, where RPS/NAPI parks packets between the driver and the
    protocol stack. Usually empty on an idle system.
  * **Qdisc queues** -- packets waiting to be transmitted on a device.

If you need packets *in flight*, that's a tracing problem rather than a memory
inspection one: a kprobe on ``netif_receive_skb`` or ``__netif_receive_skb_core``.

A ``sk_buff_head`` is a circular doubly-linked list whose head is not an skb,
so the walk terminates when ``next`` points back at the head address.
"""

from __future__ import annotations

import drgn
from drgn import Object, Program
from drgn.helpers.linux.cpumask import for_each_online_cpu
from drgn.helpers.linux.net import SOCKET_I, for_each_netdev, netdev_name
from drgn.helpers.linux.percpu import per_cpu

from ..core import ctypes as ct
from .registry import Entry, Subsystem, register
from .format import as_text, task_comm
from .walk import socket_files

MAX_SKBS = 64


def skb_list(head: Object):
    """Walk a struct sk_buff_head.

    ``head`` is the list anchor, not an skb -- stop when we come back to it.
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

    Not the same shape as sk_buff_head: a qdisc keeps an explicit head/tail
    NULL-terminated chain rather than a circular list.
    """
    node = head.head
    seen = 0
    while node.value_() and seen < MAX_SKBS:
        yield node
        node = node.next
        seen += 1


def _socket_socks(prog: Program):
    """(task, fd, sock) for every socket fd on the system.

    The protocol half. A socket whose sk is NULL is being torn down and has
    nothing to queue.
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
    """Every socket queue that currently holds something -- where to look first."""
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
        doc="Queued packet buffers. Packets in flight are not browsable -- trace those.",
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
                "skbs that arrived but haven't been read by userspace.",
                receive_queues,
            ),
            Entry(
                "write",
                "socket write queues",
                "skbs sent but not yet acknowledged/completed.",
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

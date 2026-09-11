"""Networking: namespaces, devices, and queues."""

from __future__ import annotations

from drgn import Program
from drgn.helpers.linux.net import (
    for_each_net,
    for_each_netdev,
    netdev_for_each_tx_queue,
    netdev_name,
)

from .format import as_text
from .registry import Entry, Subsystem, register


def namespaces(prog: Program):
    for index, net in enumerate(for_each_net(prog)):
        yield f"net ns {index} ({net.ns.inum.value_()})", net


def netdevs(prog: Program):
    for dev in for_each_netdev(prog["init_net"].address_of_()):
        name = as_text(netdev_name(dev))
        yield f"{name}  ifindex {dev.ifindex.value_()}", dev


def tx_queues(prog: Program):
    for dev in for_each_netdev(prog["init_net"].address_of_()):
        name = as_text(netdev_name(dev))
        for index, queue in enumerate(netdev_for_each_tx_queue(dev)):
            yield f"{name} tx{index}", queue


register(
    Subsystem(
        key="net",
        label="net",
        doc="Network namespaces, net_device structures and their queues.",
        entries=[
            Entry(
                "init_net",
                "init_net",
                "Statically allocated initial network namespace.",
                lambda prog: prog["init_net"],
            ),
            Entry("namespaces", "net namespaces", "struct net instances in net_namespace_list.", namespaces),
            Entry("netdevs", "net devices", "struct net_device instances in init_net.", netdevs),
            Entry("txqueues", "TX queues", "struct netdev_queue instances for devices in init_net.", tx_queues),
        ],
    )
)

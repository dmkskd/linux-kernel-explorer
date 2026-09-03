"""The driver model: devices, buses, classes, and what is bound to what.

A ``net_device`` has a ``struct device``; that device sits on a bus; the bus
may have a driver bound. A ``gendisk`` is also a device, and its partitions
correspond to the ``super_block`` entries under vfs.

Enumeration goes through the kobject layer rather than any per-subsystem list:
``devices_kset``, ``bus_kset`` and ``class_kset`` are ksets whose ``list``
holds every registered kobject, and ``container_of`` gets back to the real
struct. sysfs walks the same ksets, so every device here has a
``/sys/devices/...`` path.
"""

from __future__ import annotations

from drgn import Program, container_of
from drgn.helpers.linux.block import disk_name, for_each_disk, for_each_partition, part_name
from drgn.helpers.linux.device import (
    dev_name,
    for_each_registered_blkdev,
    for_each_registered_chrdev,
)
from drgn.helpers.linux.list import list_for_each_entry
from drgn.helpers.linux.pci import for_each_pci_dev, pci_name

from ..core import ctypes as ct
from .registry import Entry, Subsystem, register
from .format import as_text


def kset_kobjects(kset):
    """Every kobject registered in a kset."""
    return list_for_each_entry("struct kobject", kset.list.address_of_(), "entry")


def _kobj_name(kobj) -> str:
    return ct.safe(lambda: kobj.name.string_().decode("utf-8", "replace"), "?")


def all_devices(prog: Program):
    """Every registered device, via devices_kset -- what /sys/devices shows."""
    for kobj in kset_kobjects(prog["devices_kset"]):
        device = container_of(kobj, "struct device", "kobj")
        bus = ct.safe(lambda: device.bus.name.string_().decode(), None)
        driver = ct.safe(lambda: device.driver.name.string_().decode(), None)
        suffix = f"  [{bus}]" if bus else ""
        bound = f" → {driver}" if driver else ""
        yield f"{as_text(dev_name(device))}{suffix}{bound}", device


def buses(prog: Program):
    """Registered buses.

    bus_kset holds each bus's subsys kset, so unwrapping is two container_of
    hops: kobject → kset → subsys_private, which owns the bus_type.
    """
    for kobj in kset_kobjects(prog["bus_kset"]):
        kset = container_of(kobj, "struct kset", "kobj")
        private = container_of(kset, "struct subsys_private", "subsys")
        yield _kobj_name(kobj), private


def classes(prog: Program):
    """Registered classes -- the /sys/class view."""
    for kobj in kset_kobjects(prog["class_kset"]):
        kset = container_of(kobj, "struct kset", "kobj")
        private = container_of(kset, "struct subsys_private", "subsys")
        yield _kobj_name(kobj), private


def bound_devices(prog: Program):
    """Only devices that actually have a driver bound."""
    for kobj in kset_kobjects(prog["devices_kset"]):
        device = container_of(kobj, "struct device", "kobj")
        if not ct.safe(lambda: device.driver.value_(), 0):
            continue
        driver = device.driver.name.string_().decode("utf-8", "replace")
        yield f"{as_text(dev_name(device))} → {driver}", device


def disks(prog: Program):
    for disk in for_each_disk(prog):
        yield as_text(disk_name(disk)), disk


def partitions(prog: Program):
    for part in for_each_partition(prog):
        yield as_text(part_name(part)), part


def pci_devices(prog: Program):
    for dev in for_each_pci_dev(prog):
        yield f"{as_text(pci_name(dev))}  {dev.vendor.value_():04x}:{dev.device.value_():04x}", dev


def char_devices(prog: Program):
    for major, name in for_each_registered_chrdev(prog):
        yield f"char {major:>3}  {as_text(name)}", None


def block_devices(prog: Program):
    for major, name in for_each_registered_blkdev(prog):
        yield f"block {major:>3}  {as_text(name)}", None


register(
    Subsystem(
        key="device",
        label="device",
        doc="The driver model: devices, buses, classes, and driver binding.",
        entries=[
            Entry("devices", "all devices", "Every registered device (devices_kset).", all_devices),
            Entry(
                "bound",
                "devices with a driver",
                "Devices whose driver pointer is non-NULL.",
                bound_devices,
            ),
            Entry("buses", "buses", "Registered bus types, via subsys_private.", buses),
            Entry("classes", "classes", "Registered classes -- the /sys/class view.", classes),
            Entry("pci", "PCI devices", "struct pci_dev, with vendor:device ids.", pci_devices),
            Entry("disks", "block disks", "struct gendisk per disk.", disks),
            Entry("partitions", "partitions", "struct block_device per partition.", partitions),
        ],
    )
)

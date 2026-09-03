"""Memory management: address spaces, zones, and the vmalloc arena."""

from __future__ import annotations

from drgn import Program
from drgn.helpers.linux.mm import for_each_vmap_area, vma_name
from drgn.helpers.linux.mmzone import for_each_online_pgdat
from drgn.helpers.linux.pid import find_task

from .registry import Entry, Subsystem, register


def pgdats(prog: Program):
    for pgdat in for_each_online_pgdat(prog):
        yield f"node {pgdat.node_id.value_()}", pgdat


def zones(prog: Program):
    for pgdat in for_each_online_pgdat(prog):
        node = pgdat.node_id.value_()
        for index in range(pgdat.nr_zones.value_()):
            zone = pgdat.node_zones[index]
            name = zone.name.string_().decode("utf-8", "replace")
            yield f"node{node} {name}", zone


def vmap_areas(prog: Program):
    for area in for_each_vmap_area(prog):
        start = area.va_start.value_()
        size = area.va_end.value_() - start
        yield f"{start:#x} ({size >> 10} KiB)", area


def init_vmas(prog: Program):
    """VMAs of pid 1 -- a concrete, always-present address space to explore."""
    from drgn.helpers.linux.mm import for_each_vma

    task = find_task(prog, 1)
    for vma in for_each_vma(task.mm):
        # vma_name() gives bytes for a named mapping (file path or [heap]-style
        # tag) and None for an anonymous one.
        name = vma_name(vma)
        label = name.decode("utf-8", "replace") if name else "anon"
        yield f"{vma.vm_start.value_():#x} {label}", vma


register(
    Subsystem(
        key="mm",
        label="mm",
        doc="Address spaces, NUMA nodes, zones and kernel virtual mappings.",
        entries=[
            Entry(
                "init_mm",
                "init_mm",
                "The kernel's own mm_struct -- page tables for kernel space.",
                lambda prog: prog["init_mm"],
            ),
            Entry("pgdat", "NUMA nodes (pglist_data)", "Per-node memory descriptors.", pgdats),
            Entry("zones", "zones", "DMA/Normal/Movable zones with their watermarks.", zones),
            Entry(
                "vmas_pid1",
                "VMAs of pid 1",
                "vm_area_struct list for init -- a real user address space.",
                init_vmas,
            ),
            Entry("vmap", "vmap areas", "The vmalloc arena, area by area.", vmap_areas),
        ],
    )
)

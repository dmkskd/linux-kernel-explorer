"""Physical pages, and the bridge from virtual mappings to them.

There is one ``struct page`` per physical frame, in a flat vmemmap array
indexed by pfn. The struct is mostly anonymous unions whose interpretation
depends on the page's current use, so the raw fields read poorly; the derived
rows (pfn, physical address, decoded flags) are the usable form.

A ``vm_area_struct`` describes what may be mapped; a page table walk
(``follow_page``) reports what is currently resident. That walk is the link
from virtual to physical.
"""

from __future__ import annotations

from drgn import Program
from drgn.helpers.linux.mm import (
    for_each_vma,
    follow_page,
    page_to_pfn,
    pfn_to_page,
    for_each_valid_pfn_and_page,
)
from drgn.helpers.linux.pid import find_task

from ..core import ctypes as ct
from .registry import Entry, Subsystem, register


def resident_pages(prog: Program):
    """Pages actually backing pid 1's address space."""
    task = find_task(prog, 1)
    mm = task.mm
    for vma in for_each_vma(mm):
        for addr in range(vma.vm_start.value_(), vma.vm_end.value_(), 4096):
            page = ct.safe(lambda a=addr: follow_page(mm, a), None)
            if page is None or not page.value_():
                continue
            yield f"{addr:#x}  pfn {page_to_pfn(page).value_()}", page


def valid_pages(prog: Program):
    """A window onto the vmemmap: the first valid pfns on the system."""
    for pfn, page in for_each_valid_pfn_and_page(prog):
        yield f"pfn {pfn}", page


def low_pages(prog: Program):
    """The first 512 frames from the lowest valid pfn."""
    base = 0
    for pfn, _ in for_each_valid_pfn_and_page(prog):
        base = pfn
        break
    for pfn in range(base, base + 512):
        page = ct.safe(lambda p=pfn: pfn_to_page(prog, p), None)
        if page is not None:
            yield f"pfn {pfn}", page


register(
    Subsystem(
        key="page",
        label="page",
        doc="Physical page frames, and how virtual mappings resolve onto them.",
        entries=[
            Entry(
                "resident",
                "resident pages of pid 1",
                "Page-table walk of every VMA: virtual address → struct page.",
                resident_pages,
            ),
            Entry(
                "vmemmap",
                "pages by pfn",
                "A window onto the vmemmap array, in physical frame order.",
                valid_pages,
            ),
            Entry(
                "low",
                "first 512 frames",
                "The bottom of physical memory, frame by frame.",
                low_pages,
            ),
        ],
    )
)

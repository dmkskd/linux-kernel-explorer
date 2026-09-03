"""Slab allocator: the caches every other subsystem allocates from."""

from __future__ import annotations

from drgn import Program
from drgn.helpers.linux.slab import for_each_slab_cache

from .registry import Entry, Subsystem, register


def caches(prog: Program):
    for cache in for_each_slab_cache(prog):
        name = cache.name.string_().decode("utf-8", "replace")
        yield f"{name}  (obj {cache.object_size.value_()}B)", cache


register(
    Subsystem(
        key="slab",
        label="slab",
        doc="kmem_cache list -- where task_struct, dentry and inode come from.",
        entries=[
            Entry(
                "caches",
                "slab caches",
                "Every kmem_cache: object size, order, per-cpu freelists.",
                caches,
            ),
        ],
    )
)

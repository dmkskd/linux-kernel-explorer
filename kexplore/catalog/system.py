"""System configuration facts.

Answers questions like "CFS or EEVDF" for the running kernel rather than for a
version in the documentation.

This kernel has no ``CONFIG_IKCONFIG``, so there is no embedded .config to
read. Facts are instead derived from structural evidence: which symbols exist,
which fields a struct has, and at what offset. Each Fact carries the evidence
that produced it.
"""

from __future__ import annotations

import drgn
from drgn import Program
from drgn.helpers.linux.cpumask import num_online_cpus, num_possible_cpus
from drgn.helpers.linux.mm import PFN_PHYS, totalram_pages
from drgn.helpers.linux.mmzone import for_each_online_pgdat
from drgn.helpers.linux.module import for_each_module
from drgn.helpers.linux.sched import loadavg
from drgn.helpers.linux.timekeeping import uptime_pretty

from ..core import arch, ctypes as ct
from .registry import Fact, FactEntry, Subsystem, register

# kernel/sched/core.c -- not emitted as a DWARF enum, so spelled out here.
PREEMPT_MODES = {
    -1: "undefined",
    0: "none",
    1: "voluntary",
    2: "full",
    3: "lazy",
}

# The sched_class instances, in the order the linker lays them out.
SCHED_CLASSES = (
    "stop_sched_class",
    "dl_sched_class",
    "rt_sched_class",
    "fair_sched_class",
    "ext_sched_class",
    "idle_sched_class",
)


def _has_symbol(prog: Program, name: str) -> bool:
    try:
        prog[name]
        return True
    except KeyError:
        return False
    except Exception:  # noqa: BLE001 - drgn raises ObjectNotFoundError
        return False


def _page_size(prog: Program) -> int:
    return PFN_PHYS(drgn.Object(prog, "unsigned long", 1)).value_()


def _uts(prog: Program, field: str) -> str:
    return prog["init_uts_ns"].name.member_(field).string_().decode("utf-8", "replace")


# ------------------------------------------------------------------ overview


def overview(prog: Program):
    yield Fact("kernel release", _uts(prog, "release"), "init_uts_ns.name.release")
    yield Fact("architecture", _uts(prog, "machine"), "init_uts_ns.name.machine")
    yield Fact("build", _uts(prog, "version"), "init_uts_ns.name.version")
    yield Fact("uptime", uptime_pretty(prog), "timekeeping helpers")

    one, five, fifteen = loadavg(prog)
    yield Fact("load average", f"{one:.2f} {five:.2f} {fifteen:.2f}", "calc_load_* averages")

    yield Fact(
        "CPUs",
        f"{num_online_cpus(prog)} online / {num_possible_cpus(prog)} possible",
        "cpu_online_mask, cpu_possible_mask",
    )
    page_size = _page_size(prog)
    total = totalram_pages(prog)
    yield Fact("page size", f"{page_size} bytes", "PFN_PHYS(1)")
    line, source = arch.cache_line_size(prog)
    yield Fact("cache line", f"{line} bytes", source)
    yield Fact(
        "total RAM",
        f"{total * page_size / (1 << 30):.1f} GiB ({total} pages)",
        "totalram_pages()",
    )
    yield Fact("modules loaded", str(sum(1 for _ in for_each_module(prog))), "the modules list")


# ----------------------------------------------------------------- scheduler


def scheduler(prog: Program):
    # Which fair-class algorithm. EEVDF replaced CFS inside the fair class in
    # 6.6 and brought new sched_entity fields with it; the struct is the
    # evidence, not the version number.
    entity = ct.member_names(prog.type("struct sched_entity"))
    eevdf_fields = [f for f in ("deadline", "vlag", "slice") if f in entity]
    if eevdf_fields:
        yield Fact(
            "fair class algorithm",
            "EEVDF",
            f"sched_entity has {', '.join(eevdf_fields)} (added with EEVDF in 6.6)",
        )
    elif "vruntime" in entity:
        yield Fact(
            "fair class algorithm",
            "CFS",
            "sched_entity has vruntime but none of deadline/vlag/slice",
        )

    if _has_symbol(prog, "sysctl_sched_base_slice"):
        slice_ns = prog["sysctl_sched_base_slice"].value_()
        yield Fact(
            "base slice",
            f"{slice_ns / 1_000_000:.2f} ms",
            "sysctl_sched_base_slice -- EEVDF's target slice",
        )
    if _has_symbol(prog, "sysctl_sched_latency"):
        yield Fact(
            "CFS latency target",
            f"{prog['sysctl_sched_latency'].value_() / 1_000_000:.2f} ms",
            "sysctl_sched_latency -- only exists on CFS kernels",
        )

    # Scheduling classes, in the priority order the linker gave them.
    present = []
    for name in SCHED_CLASSES:
        if _has_symbol(prog, name):
            present.append((prog[name].address_, name.replace("_sched_class", "")))
    present.sort()
    yield Fact(
        "class priority order",
        " > ".join(n for _, n in present),
        "sched_class instances sorted by address (linker section order)",
    )

    # sched_ext: compiled in is not the same as in use.
    if _has_symbol(prog, "ext_sched_class"):
        root = prog["scx_root"] if _has_symbol(prog, "scx_root") else None
        if root is not None and root.value_():
            name = ct.safe(lambda: root.ops.name.string_().decode(), "unknown")
            yield Fact(
                "sched_ext (BPF)", f"ACTIVE: {name}", "scx_root is non-NULL"
            )
        else:
            yield Fact(
                "sched_ext (BPF)",
                "compiled in, not loaded",
                "ext_sched_class exists but scx_root is NULL",
            )
    else:
        yield Fact("sched_ext (BPF)", "not compiled in", "no ext_sched_class symbol")

    if _has_symbol(prog, "preempt_dynamic_mode"):
        mode = prog["preempt_dynamic_mode"].value_()
        yield Fact(
            "preemption model",
            PREEMPT_MODES.get(mode, str(mode)),
            "preempt_dynamic_mode (CONFIG_PREEMPT_DYNAMIC: switchable at boot)",
        )

    yield Fact(
        "group scheduling",
        "on" if prog.type("struct task_group") else "off",
        "struct task_group exists (CONFIG_FAIR_GROUP_SCHED)",
    )
    # rq.donor exists either way: without proxy exec it's a union alias of
    # curr, so presence proves nothing and the offsets have to be compared.
    rq = prog.type("struct rq")
    if rq.has_member("donor") and rq.has_member("curr"):
        donor = drgn.offsetof(rq, "donor")
        curr = drgn.offsetof(rq, "curr")
        enabled = donor != curr
        yield Fact(
            "proxy execution",
            "on" if enabled else "off",
            f"rq.donor at {donor}, rq.curr at {curr} -- "
            + ("distinct fields" if enabled else "union alias, so not enabled"),
        )


# -------------------------------------------------------------------- memory


def memory(prog: Program):
    page_size = _page_size(prog)
    yield Fact("page size", f"{page_size} bytes", "PFN_PHYS(1)")

    # SLAB is gone since 6.8, but don't infer from the version -- SLUB's own
    # fields are the evidence, and struct array_cache would mean SLAB.
    cache = prog.type("struct kmem_cache")
    if cache.has_member("oo") and cache.has_member("min_partial"):
        yield Fact(
            "slab allocator",
            "SLUB",
            "struct kmem_cache has oo/min_partial; no struct array_cache (SLAB)",
        )
    else:
        yield Fact("slab allocator", "unrecognised", "no SLUB marker fields on kmem_cache")

    # The percpu fast path was reworked: kmem_cache_cpu gave way to sheaves.
    if cache.has_member("cpu_sheaves"):
        yield Fact(
            "slab percpu caching",
            "sheaves",
            "kmem_cache.cpu_sheaves/sheaf_capacity -- struct kmem_cache_cpu is gone",
        )
    elif cache.has_member("cpu_slab"):
        yield Fact(
            "slab percpu caching", "cpu_slab", "kmem_cache.cpu_slab (classic SLUB fast path)"
        )

    nodes = list(for_each_online_pgdat(prog))
    yield Fact("NUMA nodes", str(len(nodes)), "for_each_online_pgdat()")
    for pgdat in nodes:
        node = pgdat.node_id.value_()
        zone_names = []
        for index in range(pgdat.nr_zones.value_()):
            zone = pgdat.node_zones[index]
            if zone.present_pages.value_():
                zone_names.append(zone.name.string_().decode("utf-8", "replace"))
        yield Fact(f"node {node} zones", ", ".join(zone_names), "pgdat.node_zones")

    total = totalram_pages(prog)
    yield Fact("total RAM", f"{total * page_size / (1 << 30):.1f} GiB", "totalram_pages()")

    if _has_symbol(prog, "transparent_hugepage_flags"):
        yield Fact(
            "THP flags",
            hex(prog["transparent_hugepage_flags"].value_()),
            "transparent_hugepage_flags bitmask",
        )
    if _has_symbol(prog, "nr_swapfiles"):
        yield Fact("swap files", str(prog["nr_swapfiles"].value_()), "nr_swapfiles")


register(
    Subsystem(
        key="system",
        label="system",
        doc="What kind of kernel this is, derived from structural evidence.",
        entries=[
            FactEntry("overview", "overview", "Release, uptime, CPUs, memory, modules.", overview),
            FactEntry(
                "scheduler",
                "scheduler",
                "Which fair-class algorithm, class priority order, sched_ext, preemption.",
                scheduler,
            ),
            FactEntry("memory", "memory", "Allocator, NUMA topology, zones, THP, swap.", memory),
        ],
    )
)

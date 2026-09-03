"""Machine properties read from the kernel rather than assumed.

Everything here answers a question the source code answers with a compile-time
constant we cannot see: the DWARF keeps types and variables, not macros. So
each value is recovered from a variable or a feature register, with the
architecture's own fallback when nothing readable holds it.
"""

from __future__ import annotations

import drgn
from drgn import Program

# What L1_CACHE_BYTES is on every architecture Linux supports that we might
# meet, used only when the running kernel exposes nothing better.
_DEFAULTS = {
    "x86_64": 64,
    "i386": 64,
    "aarch64": 128,  # CONFIG_ARM64_L1_CACHE_SHIFT is 7, even where CWG is 64
    "ppc64le": 128,
    "s390x": 256,
    "riscv64": 64,
}
_FALLBACK = 64


def _machine(prog: Program) -> str:
    try:
        return prog["init_uts_ns"].name.machine.string_().decode()
    except Exception:  # noqa: BLE001 - a nameless machine just takes the default
        return ""


def _from_ctr_el0(prog: Program) -> int | None:
    """arm64: CTR_EL0.CWG, the coherency write granule, in bytes.

    This is the hardware's answer, which on many cores is 64 while the kernel
    was still built for 128.
    """
    try:
        ctr = prog["arm64_ftr_reg_ctrel0"].sys_val.value_()
    except Exception:  # noqa: BLE001 - not arm64, or no feature registers
        return None
    cwg = (ctr >> 24) & 0xF
    return 4 << cwg if cwg else None


def _from_boot_cpu_data(prog: Program) -> int | None:
    """x86: the alignment the kernel itself uses for cache-hot data."""
    try:
        size = prog["boot_cpu_data"].x86_cache_alignment.value_()
    except Exception:  # noqa: BLE001 - not x86
        return None
    return size or None


def cache_line_size(prog: Program) -> tuple[int, str]:
    """Bytes per cache line, and where the number came from.

    coherency_max_size is the kernel's own record of the largest line any CPU
    reported, but it is only set when the cache topology was described by DT or
    ACPI -- it stays 0 on plenty of machines, this VM included.
    """
    try:
        largest = prog["coherency_max_size"].value_()
    except Exception:  # noqa: BLE001 - older kernels lack the variable
        largest = 0
    if largest:
        return largest, "coherency_max_size"

    for source, reader in (
        ("CTR_EL0.CWG", _from_ctr_el0),
        ("boot_cpu_data.x86_cache_alignment", _from_boot_cpu_data),
    ):
        size = reader(prog)
        if size:
            return size, source

    machine = _machine(prog)
    if machine in _DEFAULTS:
        return _DEFAULTS[machine], f"L1_CACHE_BYTES for {machine}"
    return _FALLBACK, "assumed"

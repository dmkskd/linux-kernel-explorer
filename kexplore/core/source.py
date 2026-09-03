"""Pull real documentation out of the kernel's own source.

The DWARF we already load records where every struct was declared, and
debuginfod serves the exactly-matching source for this build. Together that
means field documentation can be the kernel's real comments for *this* kernel
version -- not a hand-written gloss that drifts, and not a guess.

Three steps:
  1. ``pahole --show_decl_info`` gives ``kernel/sched/sched.h:1131`` for a tag.
     (drgn 0.2.0 doesn't expose DW_AT_decl_file/decl_line on Type, or this
     would be a direct attribute read.)
  2. ``debuginfod-find source`` fetches that file, keyed by build-id.
  3. A small scanner walks the struct body attaching comments to members.

The scanner is loose, and validated against the member names drgn
already knows: anything it extracts that isn't a real member is discarded. That
makes ``#ifdef`` blocks, anonymous unions, attribute macros and function-pointer
declarations safe to get wrong.
"""

from __future__ import annotations

import functools
import os
import re
import struct
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

# Attribute macros that trail a member name and must not be mistaken for it.
_ATTRIBUTES = re.compile(
    r"\b(____cacheline_aligned(_in_smp)?|__aligned\s*\([^)]*\)|__packed"
    r"|__randomize_layout|__attribute__\s*\(\(.*?\)\)|____cacheline_internodealigned_in_smp)"
)
_FUNC_PTR = re.compile(r"\(\s*\*+\s*(\w+)\s*\)")
_MEMBER = re.compile(r"(\w+)\s*(?:\[[^\]]*\])*\s*(?::\s*\w+)?\s*;\s*$")
_STRUCT_OPEN = re.compile(r"\b(struct|union)\b[^;{]*\{")


@dataclass
class StructDoc:
    """Comments recovered for one struct."""

    tag: str
    decl_file: str = ""
    decl_line: int = 0
    summary: str = ""
    members: dict[str, str] = field(default_factory=dict)
    # Line number of each member's declaration, for jumping into the source.
    member_lines: dict[str, int] = field(default_factory=dict)
    local_path: str = ""
    error: str = ""

    @property
    def location(self) -> str:
        return f"{self.decl_file}:{self.decl_line}" if self.decl_file else ""


def _clean_comment(text: str) -> str:
    """Flatten a C comment block into one readable line."""
    text = re.sub(r"^/\*+|\*+/$", "", text.strip())
    lines = []
    for line in text.splitlines():
        line = re.sub(r"^\s*\*+\s?", "", line).strip()
        if line:
            lines.append(line)
    return " ".join(lines).strip()


def kernel_build_id() -> str | None:
    """Read the running kernel's GNU build-id from /sys/kernel/notes."""
    try:
        data = Path("/sys/kernel/notes").read_bytes()
    except OSError:
        return None

    offset = 0
    while offset + 12 <= len(data):
        name_size, desc_size, note_type = struct.unpack_from("<III", data, offset)
        name_end = offset + 12 + name_size
        name = data[offset + 12 : name_end].rstrip(b"\0")
        desc_start = offset + 12 + ((name_size + 3) & ~3)
        if name == b"GNU" and note_type == 3:  # NT_GNU_BUILD_ID
            return data[desc_start : desc_start + desc_size].hex()
        offset = desc_start + ((desc_size + 3) & ~3)
    return None


class KernelSource:
    """Locates and caches kernel source for the running build."""

    def __init__(self, release: str | None = None, build_id: str | None = None) -> None:
        self.release = release or os.uname().release
        self.build_id = build_id or kernel_build_id()
        self._debuginfo: str | None = None
        self._prefix: str | None = None
        self._available: bool | None = None

    # ------------------------------------------------------------- discovery

    @property
    def debuginfo(self) -> str | None:
        """Path to the cached vmlinux debuginfo, fetching it if needed."""
        if self._debuginfo is None and self.build_id:
            self._debuginfo = self._find("debuginfo", None)
        return self._debuginfo

    def _find(self, kind: str, path: str | None) -> str | None:
        if not self.build_id:
            return None
        argv = ["debuginfod-find", kind, self.build_id]
        if path:
            argv.append(path)
        try:
            result = subprocess.run(argv, capture_output=True, text=True, timeout=120)
        except (OSError, subprocess.TimeoutExpired):
            return None
        return result.stdout.strip() or None

    @property
    def source_prefix(self) -> str | None:
        """The absolute source root recorded in DWARF, e.g. /usr/src/debug/...

        Distro-specific, so it's probed once against a file that always exists
        rather than assumed.
        """
        if self._prefix is not None:
            return self._prefix or None

        version = self.release.split("-")[0]
        candidates = [
            f"/usr/src/debug/kernel-{version}/linux-{self.release}/",
            f"/usr/src/debug/linux-{self.release}/",
            f"/usr/src/linux-{self.release}/",
        ]
        for prefix in candidates:
            if self._find("source", prefix + "kernel/sched/sched.h"):
                self._prefix = prefix
                return prefix
        self._prefix = ""
        return None

    @property
    def available(self) -> bool:
        """True if both pahole and debuginfod source lookups work here."""
        if self._available is None:
            self._available = bool(
                self.build_id and self.debuginfo and self.source_prefix
            )
        return self._available

    # ---------------------------------------------------------------- lookups

    @functools.lru_cache(maxsize=256)  # noqa: B019 - cache lives with the instance
    def declaration(self, tag: str) -> tuple[str, int] | None:
        """Ask pahole where ``struct <tag>`` is declared."""
        if not self.debuginfo:
            return None
        try:
            result = subprocess.run(
                ["pahole", "-C", tag, "--show_decl_info", self.debuginfo],
                capture_output=True,
                text=True,
                timeout=120,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None

        # /* <16cbf06> kernel/sched/sched.h:1131 */
        match = re.search(r"/\* <[0-9a-f]+> ([^\s:]+):(\d+) \*/", result.stdout)
        if not match:
            return None
        return match.group(1), int(match.group(2))

    @functools.lru_cache(maxsize=64)  # noqa: B019
    def local_file(self, relative_path: str) -> str | None:
        """Path to the debuginfod-cached copy of a kernel source file."""
        prefix = self.source_prefix
        if not prefix:
            return None
        return self._find("source", prefix + relative_path)

    @functools.lru_cache(maxsize=64)  # noqa: B019
    def read(self, relative_path: str) -> list[str] | None:
        """Fetch a source file via debuginfod and return its lines."""
        local = self.local_file(relative_path)
        if not local:
            return None
        try:
            return Path(local).read_text(errors="replace").splitlines()
        except OSError:
            return None

    @functools.lru_cache(maxsize=1)  # noqa: B019
    def kaslr_offset(self, runtime_stext: int) -> int:
        """Difference between running and link-time addresses.

        The debuginfo records link-time addresses while the running kernel is
        relocated, so addr2line needs the runtime address shifted back or it
        reports ``??:0``.
        """
        if not self.debuginfo:
            return 0
        try:
            nm = subprocess.run(
                ["nm", self.debuginfo], capture_output=True, text=True, timeout=120
            ).stdout
        except (OSError, subprocess.TimeoutExpired):
            return 0
        for line in nm.splitlines():
            parts = line.split()
            if len(parts) == 3 and parts[2] == "_stext":
                return runtime_stext - int(parts[0], 16)
        return 0

    @functools.lru_cache(maxsize=256)  # noqa: B019
    def function_location(self, runtime_address: int, offset: int) -> tuple[str, int] | None:
        """Resolve a function's runtime address to (relative path, line)."""
        if not self.debuginfo:
            return None
        try:
            out = subprocess.run(
                ["addr2line", "-e", self.debuginfo, hex(runtime_address - offset)],
                capture_output=True,
                text=True,
                timeout=60,
            ).stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            return None
        if not out or out.startswith("??"):
            return None
        path, _, line = out.partition(":")
        prefix = self.source_prefix or ""
        if prefix and path.startswith(prefix):
            path = path[len(prefix) :]
        try:
            return path, int(line.split()[0])
        except ValueError:
            return None

    # ---------------------------------------------------------------- parsing

    def document(self, tag: str, member_names: frozenset[str]) -> StructDoc:
        """Recover the summary comment and per-member comments for ``tag``."""
        doc = StructDoc(tag)

        declaration = self.declaration(tag)
        if not declaration:
            doc.error = f"no declaration info for struct {tag}"
            return doc
        doc.decl_file, doc.decl_line = declaration

        lines = self.read(doc.decl_file)
        if lines is None:
            doc.error = f"could not fetch {doc.decl_file}"
            return doc

        index = doc.decl_line - 1
        if not (0 <= index < len(lines)):
            doc.error = f"{doc.location} out of range"
            return doc

        doc.summary = _preceding_comment(lines, index)
        doc.members, doc.member_lines = _member_comments(lines, index, member_names)
        doc.local_path = self.local_file(doc.decl_file) or ""
        return doc


def _preceding_comment(lines: list[str], index: int) -> str:
    """The comment block immediately above line ``index``."""
    end = index - 1
    while end >= 0 and not lines[end].strip():
        end -= 1
    if end < 0 or not lines[end].strip().endswith("*/"):
        return ""

    start = end
    while start >= 0 and "/*" not in lines[start]:
        start -= 1
    if start < 0:
        return ""
    return _clean_comment("\n".join(lines[start : end + 1]))


def _member_name(code: str) -> str | None:
    """Extract the declared identifier from one member line."""
    code = _ATTRIBUTES.sub("", code).strip()
    func = _FUNC_PTR.search(code)
    if func:
        return func.group(1)
    match = _MEMBER.search(code)
    return match.group(1) if match else None


def _member_comments(
    lines: list[str], start: int, member_names: frozenset[str]
) -> tuple[dict[str, str], dict[str, int]]:
    """Walk a struct body, attaching leading and trailing comments to members.

    Only names drgn already reported as members are kept, so a mis-parse costs
    a missing comment rather than a wrong one.
    """
    comments: dict[str, str] = {}
    member_lines: dict[str, int] = {}
    pending: list[str] = []
    in_block = False
    block: list[str] = []
    depth = 0

    for offset, raw in enumerate(lines[start:]):
        line = raw
        line_number = start + offset + 1

        # Continuation of a multi-line comment.
        if in_block:
            block.append(line)
            if "*/" in line:
                in_block = False
                pending.append(_clean_comment("\n".join(block)))
                block = []
            continue

        stripped = line.strip()
        if stripped.startswith("/*") and "*/" not in stripped:
            in_block = True
            block = [line]
            continue

        # Split off a trailing same-line comment: it documents this member.
        trailing = ""
        inline = re.search(r"/\*(.*?)\*/\s*$", stripped)
        if inline:
            trailing = _clean_comment(inline.group(0))
            stripped = stripped[: inline.start()].strip()
        elif "//" in stripped:
            head, _, tail = stripped.partition("//")
            trailing, stripped = tail.strip(), head.strip()

        if not stripped:
            if trailing:
                pending.append(trailing)
            continue
        if stripped.startswith("#"):
            continue  # preprocessor: keep any pending comment, it still applies

        depth += stripped.count("{") - stripped.count("}")

        # An opening brace with no terminating ';' is a nested aggregate, not a
        # member declaration; drgn flattens those so the names still line up.
        if _STRUCT_OPEN.search(stripped) and not stripped.endswith(";"):
            pending.clear()
            continue

        if stripped.endswith(";"):
            name = _member_name(stripped)
            if name and name in member_names:
                member_lines.setdefault(name, line_number)
                parts = [p for p in (" ".join(pending).strip(), trailing) if p]
                if parts:
                    comments[name] = "; ".join(parts)
            pending.clear()

        if depth <= 0 and "}" in stripped:
            break

    return comments, member_lines

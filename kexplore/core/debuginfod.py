"""Keep the debuginfod cache between runs and allow lookups from the cache only.

libdebuginfod downloads the kernel's DWARF, several hundred MB, to a temporary
file (``debuginfo.XXXXXX``) and renames it to ``debuginfo`` only when the
transfer completes; it does not resume an interrupted transfer. Quitting the
explorer during the download discards it and leaves the partial file behind,
and the next run downloads from the beginning.

:func:`prefetch` therefore downloads to completion outside the UI, after which
later runs are served from the cache. libdebuginfod reads its cache only when
at least one URL is configured, and with ``DEBUGINFOD_URLS`` empty it returns
ENOSYS without reading it. Cache-only lookups therefore use a URL that points
at a closed port (:data:`OFFLINE_URLS`): cached files are served from disk, and
a file missing from the cache fails immediately instead of opening a network
connection.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

# A port nothing listens on. Reaching the "server" fails immediately with
# ECONNREFUSED, but the cache is still searched first. See the module docstring.
OFFLINE_URLS = "http://127.0.0.1:1/"

# libdebuginfod prunes any file it has not read for max_unused_age_s (a week by
# default) and re-probes a miss after cache_miss_s (ten minutes). Both are
# shorter than a period of offline work, so a prefetched vmlinux could be
# pruned. libdebuginfod reads both settings from these files in the cache
# directory.
_RETAIN_SECONDS = 365 * 24 * 3600
_MISS_SECONDS = 24 * 3600

# Partial downloads are named "<kind>.XXXXXX" by mkstemp. One older than this
# belongs to a run that has exited, and will not be completed.
_STALE_TEMP_SECONDS = 4 * 3600

_TEMP = re.compile(r"^(debuginfo|executable|source.*)\.[A-Za-z0-9]{6}$")


def cache_path() -> Path:
    """Where libdebuginfod keeps its cache for the current user."""
    explicit = os.environ.get("DEBUGINFOD_CACHE_PATH")
    if explicit:
        return Path(explicit)
    base = os.environ.get("XDG_CACHE_HOME")
    return (Path(base) if base else Path.home() / ".cache") / "debuginfod_client"


def ima_cert_path() -> str:
    """The IMA certificate path this machine configures, or "".

    Fedora's DEBUGINFOD_URLS carries ``ima:enforcing``, which makes
    libdebuginfod verify the signature on what it downloads, and the
    certificates come from a separate variable. Both are set by
    /etc/profile.d/debuginfod.sh and both are dropped by sudo, so forwarding
    only the URL asks for enforcement with no keys: the transfer completes,
    fails with ENOKEY, and nothing is cached. Read the same *.certpath files
    that profile script reads.
    """
    try:
        parts = [p.read_text().strip() for p in sorted(Path("/etc/debuginfod").glob("*.certpath"))]
    except OSError:
        return ""
    return ":".join(part for part in parts if part)


def offline() -> bool:
    """True if lookups are currently restricted to the cache."""
    return os.environ.get("DEBUGINFOD_URLS", "") == OFFLINE_URLS


def configure(offline_only: bool = False) -> None:
    """Set the cache retention and, if requested, restrict lookups to the cache.

    drgn reads ``DEBUGINFOD_URLS`` when it loads debug info, not at import, so
    calling this before drgn attaches also applies to drgn's fetch of the
    vmlinux DWARF.
    """
    if offline_only:
        os.environ["DEBUGINFOD_URLS"] = OFFLINE_URLS

    # Signature enforcement without certificates downloads the whole file and
    # then rejects it with ENOKEY, on every run.
    if (
        "ima:" in os.environ.get("DEBUGINFOD_URLS", "")
        and not os.environ.get("DEBUGINFOD_IMA_CERT_PATH")
    ):
        certs = ima_cert_path()
        if certs:
            os.environ["DEBUGINFOD_IMA_CERT_PATH"] = certs
        else:
            # No certificates are installed, so enforcement cannot succeed:
            # download without signature verification and report it.
            os.environ["DEBUGINFOD_URLS"] = servers()
            print("kernel debug info: no IMA certificates on this machine, "
                  "so the download is not signature-checked", file=sys.stderr)

    cache = cache_path()
    try:
        cache.mkdir(parents=True, exist_ok=True)
        (cache / "max_unused_age_s").write_text(f"{_RETAIN_SECONDS}\n")
        (cache / "cache_clean_interval_s").write_text(f"{_RETAIN_SECONDS}\n")
        # A source file missing for a build ID stays missing, and the
        # source-prefix probe requests paths that do not exist. Without this
        # setting, every run repeats the same two failed requests.
        (cache / "cache_miss_s").write_text(f"{_MISS_SECONDS}\n")
    except OSError:
        pass  # libdebuginfod reports a read-only or missing cache itself.

    # Remove zero-length debuginfo and executable entries, left by an
    # interrupted transfer or a cached 404, before anything reads the cache.
    clear_empty_entries()


def _entry_dir(build_id: str) -> Path:
    return cache_path() / build_id


def is_cached(build_id: str) -> bool:
    """True if the completed vmlinux debuginfo is already on disk.

    Zero-length files are never a hit: libdebuginfod writes one at the final
    name for a server 404 (negative cache), and an interrupted transfer can
    leave one behind too. Either way drgn would load an empty vmlinux.
    """
    try:
        path = _entry_dir(build_id) / "debuginfo"
        if path.stat().st_size == 0:
            return False
        result = subprocess.run(["readelf", "-W", "-h", "-S", "-n", str(path)], capture_output=True,
                                text=True, timeout=30)
        found = re.search(r"Build ID: ([0-9a-fA-F]+)", result.stdout)
        if (result.returncode == 0 and not result.stderr.strip() and found
                and found.group(1).lower() == build_id.lower()):
            return True
        # Preserve the bad file for diagnosis while allowing a fresh download.
        path.rename(path.with_name("debuginfo.invalid"))
        print("kernel debug info: invalid cached file or mismatched build ID; "
              "saved as debuginfo.invalid; a replacement is required", file=sys.stderr)
        return False
    except OSError:
        return False
    except subprocess.TimeoutExpired:
        return False


def clear_empty_entries() -> int:
    """Delete zero-length debuginfo/executable cache entries; return count.

    libdebuginfod answers a later query from the empty file with an immediate
    ENOENT, so an empty entry prevents every later fetch for that build ID.
    Source misses are also cached as empty files; those are correct and are
    kept. Removing a debuginfo or executable entry that was a real 404 costs
    one repeated query.
    """
    removed = 0
    for entry_dir in _directories(cache_path()):
        for name in ("debuginfo", "executable"):
            victim = entry_dir / name
            try:
                if victim.stat().st_size == 0:
                    victim.unlink()
                    removed += 1
            except OSError:
                continue
    return removed


def stale_partials(build_id: str | None = None, max_age: float = _STALE_TEMP_SECONDS) -> list[Path]:
    """Abandoned partial downloads, which libdebuginfod never resumes.

    ``max_age`` zero means every partial, fresh ones included; the default
    keeps only those from runs that have already exited.
    """
    cache = cache_path()
    roots = [_entry_dir(build_id)] if build_id else _directories(cache)
    cutoff = time.time() - max_age
    found = []
    for root in roots:
        try:
            entries = list(root.iterdir())
        except OSError:
            continue
        for item in entries:
            if not _TEMP.match(item.name):
                continue
            try:
                if item.stat().st_mtime < cutoff:
                    found.append(item)
            except OSError:
                continue
    return found


def _directories(cache: Path) -> Iterator[Path]:
    try:
        for item in cache.iterdir():
            if item.is_dir():
                yield item
    except OSError:
        return


def clear_partials(build_id: str | None = None) -> int:
    """Delete abandoned partial downloads; returns the bytes reclaimed."""
    freed = 0
    for item in stale_partials(build_id):
        try:
            freed += item.stat().st_size
            item.unlink()
        except OSError:
            continue
    return freed


def human(size: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GiB"


def servers(urls: str | None = None) -> str:
    """The server URLs in DEBUGINFOD_URLS, without the ``ima:`` directives.

    Fedora ships ``ima:enforcing <url> ima:ignore`` in /etc/debuginfod/*.urls.
    Those tokens set signature checking for the URL between them. The value is
    valid, but printed unmodified it appears malformed.
    """
    if urls is None:
        urls = os.environ.get("DEBUGINFOD_URLS", "")
    found = [token for token in urls.split() if "://" in token]
    return " ".join(found) if found else urls


def expected_size(build_id: str) -> int:
    """The transfer size the server announced, or 0 if it has not said yet.

    libdebuginfod writes the response headers to ``hdr-debuginfo`` in the
    cache entry, and Fedora's server sends ``x-debuginfod-size``. It appears
    while the body is still transferring, so it can drive a percentage.
    """
    try:
        text = (_entry_dir(build_id) / "hdr-debuginfo").read_text(errors="replace")
    except OSError:
        return 0
    match = re.search(r"^x-debuginfod-size:\s*(\d+)", text, re.MULTILINE)
    return int(match.group(1)) if match else 0


def _partial_size(build_id: str) -> int:
    """Bytes written so far to the in-flight temporary file."""
    largest = 0
    for item in stale_partials(build_id, max_age=0):
        try:
            largest = max(largest, item.stat().st_size)
        except OSError:
            continue
    return largest


class _Progress(threading.Thread):
    """Report transfer progress by watching the partial file grow.

    DEBUGINFOD_PROGRESS prints one line per callback, with no rate and no
    total. The size of the partial file is the number of bytes transferred,
    and the cache entry records the total size.
    """

    def __init__(self, build_id: str, stream) -> None:
        super().__init__(daemon=True)
        self.build_id = build_id
        self.stream = stream
        self.done = threading.Event()
        self.live = hasattr(stream, "isatty") and stream.isatty()

    def run(self) -> None:
        started = time.monotonic()
        last_line = ""
        while not self.done.wait(0.5):
            size = _partial_size(self.build_id)
            if not size:
                continue
            elapsed = time.monotonic() - started
            rate = size / elapsed if elapsed > 0 else 0
            total = expected_size(self.build_id)
            if total:
                line = field("progress", f"{human(size)} of {human(total)} "
                                         f"({100 * size / total:.0f}%) at {human(rate)}/s")
            else:
                line = field("progress", f"{human(size)} at {human(rate)}/s")
            if self.live:
                self.stream.write("\r\033[K" + line)
                self.stream.flush()
            elif line[:20] != last_line[:20]:  # only when the figure moves
                self.stream.write(line + "\n")
                self.stream.flush()
            last_line = line
        if self.live and last_line:
            self.stream.write("\r\033[K")
            self.stream.flush()

    def stop(self) -> None:
        self.done.set()
        self.join(timeout=2)


def field(label: str, value: str) -> str:
    """One indented ``label value`` line of the fetch report."""
    return f"  {label:<9}{value}"


def prefetch(build_id: str, log: Callable[[str], None] = print) -> bool:
    """Download the vmlinux debuginfo to completion, reporting progress.

    After a completed prefetch, every later run, including ``--offline`` runs,
    is served from the cache. An interrupted prefetch, like an interrupted
    fetch in the explorer, discards the partial download.
    """
    local = local_vmlinux()
    if local:
        log(field("local", str(local)))
        return True

    if is_cached(build_id):
        size = (_entry_dir(build_id) / "debuginfo").stat().st_size
        log(field("cached", f"{human(size)} in {_entry_dir(build_id)}"))
        return True

    freed = clear_partials(build_id)
    if freed:
        log(field("cleaned", f"discarded {human(freed)} of abandoned partial downloads"))

    urls = os.environ.get("DEBUGINFOD_URLS", "")
    if not urls or urls == OFFLINE_URLS:
        log(field("server", "DEBUGINFOD_URLS names no real server; nothing to fetch"))
        return False

    log(field("build-id", build_id))
    log(field("server", servers(urls)))

    started = time.monotonic()
    progress = _Progress(build_id, sys.stderr)
    progress.start()
    try:
        result = subprocess.run(
            ["debuginfod-find", "debuginfo", build_id],
            env=dict(os.environ, DEBUGINFOD_PROGRESS="0"),
            stdout=subprocess.PIPE,  # the resulting path, which the caller knows
            text=True,
        )
    except OSError as exc:
        progress.stop()
        log(field("failed", f"could not run debuginfod-find: {exc}"))
        return False
    except KeyboardInterrupt:
        progress.stop()
        log(field("stopped", "the partial file is discarded, nothing is cached"))
        return False
    finally:
        progress.stop()

    if result.returncode != 0 or not is_cached(build_id):
        log(field("failed", "the transfer did not complete; nothing was cached"))
        return False

    size = (_entry_dir(build_id) / "debuginfo").stat().st_size
    seconds = time.monotonic() - started
    log(field("done", f"{human(size)} in {seconds:.0f}s; later runs read the cache"))
    return True


def local_vmlinux() -> Path | None:
    """A locally installed vmlinux with DWARF, if one exists.

    Covers the distro debug packages (Ubuntu's dbgsym, Debian's -dbg), which
    install outside the debuginfod cache. drgn searches these same paths.
    """
    release = os.uname().release
    for candidate in (
        Path(f"/usr/lib/debug/boot/vmlinux-{release}"),
        Path(f"/usr/lib/debug/lib/modules/{release}/vmlinux"),
        Path(f"/boot/vmlinux-{release}"),
    ):
        if candidate.is_file():
            return candidate
    return None


def status(build_id: str | None) -> str:
    """One line describing what the attach will find."""
    local = local_vmlinux()
    if local:
        return f"installed locally: {local}"
    if not build_id:
        return "no kernel build-id; debuginfod cannot be used"
    if is_cached(build_id):
        try:
            size = (_entry_dir(build_id) / "debuginfo").stat().st_size
        except OSError:
            size = 0
        where = "cache only" if offline() else servers()
        return f"cached, {human(size)}, from {where}"
    if offline():
        return "not cached, and offline: run 'kexplore --prefetch' first"
    return "not cached; fetched during the attach"

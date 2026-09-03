"""Make the debuginfod cache survive between runs, and let it work offline.

The kernel's DWARF is ~700MB. libdebuginfod downloads it to a temporary name
(``debuginfo.XXXXXX``) and only renames it to ``debuginfo`` once the transfer
*completes* -- there is no resume. Quitting the explorer mid-fetch therefore
throws the whole download away and leaves the partial file behind, so the next
run starts from zero. That is why the fetch appears to happen every time.

Two things follow:

  * the download has to be done once, to completion, outside the UI
    (:func:`prefetch`), after which every later run is a local cache hit;
  * once it is cached, the network is not needed at all -- but the client only
    consults its cache when at least one URL is configured. With
    ``DEBUGINFOD_URLS`` empty it returns ENOSYS without ever looking. So
    "cache only" is spelled as a URL pointing at a closed port
    (:data:`OFFLINE_URLS`): hits are served from disk, misses fail instantly
    rather than reaching for a roaming connection.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path
from typing import Callable, Iterator

# A port nothing listens on. Reaching the "server" fails immediately with
# ECONNREFUSED, but the cache is still searched first. See the module docstring.
OFFLINE_URLS = "http://127.0.0.1:1/"

# The client prunes any file it has not read for max_unused_age_s (a week by
# default) and re-probes a miss after cache_miss_s (ten minutes). Both are far
# too short to keep a 700MB vmlinux across a stretch of working offline, and
# both are documented as being read from these files in the cache directory.
_RETAIN_SECONDS = 365 * 24 * 3600
_MISS_SECONDS = 24 * 3600

# Partial downloads are named "<kind>.XXXXXX" by mkstemp. Anything older than
# this is from a run that has already exited, so it is dead weight, not
# progress.
_STALE_TEMP_SECONDS = 4 * 3600

_TEMP = re.compile(r"^(debuginfo|executable|source.*)\.[A-Za-z0-9]{6}$")


def cache_path() -> Path:
    """Where libdebuginfod keeps its cache for the current user."""
    explicit = os.environ.get("DEBUGINFOD_CACHE_PATH")
    if explicit:
        return Path(explicit)
    base = os.environ.get("XDG_CACHE_HOME")
    return (Path(base) if base else Path.home() / ".cache") / "debuginfod_client"


def offline() -> bool:
    """True if lookups are currently restricted to the cache."""
    return os.environ.get("DEBUGINFOD_URLS", "") == OFFLINE_URLS


def configure(offline_only: bool = False) -> None:
    """Pin the cache retention, and optionally cut the network off.

    Safe to call before drgn attaches: drgn reads ``DEBUGINFOD_URLS`` when it
    loads debug info, not at import, so this still applies to its own fetch of
    the vmlinux DWARF.
    """
    if offline_only:
        os.environ["DEBUGINFOD_URLS"] = OFFLINE_URLS

    cache = cache_path()
    try:
        cache.mkdir(parents=True, exist_ok=True)
        (cache / "max_unused_age_s").write_text(f"{_RETAIN_SECONDS}\n")
        (cache / "cache_clean_interval_s").write_text(f"{_RETAIN_SECONDS}\n")
        # A missing source file for a given build-id stays missing, and the
        # source-prefix probe deliberately asks for paths that do not exist.
        # Without this every run re-asks the server for the same two misses.
        (cache / "cache_miss_s").write_text(f"{_MISS_SECONDS}\n")
    except OSError:
        pass  # A read-only or absent cache is the client's problem to report.


def _entry_dir(build_id: str) -> Path:
    return cache_path() / build_id


def is_cached(build_id: str) -> bool:
    """True if the completed vmlinux debuginfo is already on disk."""
    return (_entry_dir(build_id) / "debuginfo").is_file()


def stale_partials(build_id: str | None = None) -> list[Path]:
    """Abandoned partial downloads, which cost ~700MB each and never resume."""
    cache = cache_path()
    roots = [_entry_dir(build_id)] if build_id else _directories(cache)
    cutoff = time.time() - _STALE_TEMP_SECONDS
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


def prefetch(build_id: str, log: Callable[[str], None] = print) -> bool:
    """Download the vmlinux debuginfo to completion, reporting progress.

    This is the whole point of a separate command: run it once on a connection
    you are happy to use, and every later run -- including ``--offline`` ones --
    is served from disk. Interrupting the explorer's own fetch discards it, but
    interrupting this only costs the same download again.
    """
    if is_cached(build_id):
        size = (_entry_dir(build_id) / "debuginfo").stat().st_size
        log(f"already cached: {human(size)} in {_entry_dir(build_id)}")
        return True

    freed = clear_partials(build_id)
    if freed:
        log(f"discarded {human(freed)} of abandoned partial downloads")

    urls = os.environ.get("DEBUGINFOD_URLS", "")
    if not urls or urls == OFFLINE_URLS:
        log("DEBUGINFOD_URLS is not set to a real server; nothing to fetch from")
        return False

    log(f"fetching kernel debuginfo for build-id {build_id}")
    log(f"  from {urls}")
    log("  this is a few hundred MB and does not resume -- let it finish")

    environment = dict(os.environ, DEBUGINFOD_PROGRESS="1")
    try:
        result = subprocess.run(
            ["debuginfod-find", "debuginfo", build_id], env=environment
        )
    except OSError as exc:
        log(f"could not run debuginfod-find: {exc}")
        return False
    except KeyboardInterrupt:
        log("\ninterrupted -- the partial download was discarded, nothing is cached")
        return False

    if result.returncode != 0 or not is_cached(build_id):
        log("fetch did not complete; nothing was cached")
        return False

    size = (_entry_dir(build_id) / "debuginfo").stat().st_size
    log(f"cached {human(size)}; later runs can use --offline")
    return True


def status(build_id: str | None) -> str:
    """One line describing what the cache can serve right now."""
    if not build_id:
        return "no kernel build-id; debuginfod cannot be used"
    if is_cached(build_id):
        try:
            size = (_entry_dir(build_id) / "debuginfo").stat().st_size
        except OSError:
            size = 0
        where = "cache only" if offline() else os.environ.get("DEBUGINFOD_URLS", "")
        return f"debuginfo cached ({human(size)}), {where}"
    if offline():
        return "debuginfo not cached and offline: run 'kexplore --prefetch' first"
    return "debuginfo not cached; it will be downloaded now (a few hundred MB)"

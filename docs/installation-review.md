# Installation review

The installation path is usable for learning labs, with automatic matching
source configuration on Ubuntu and Debian. It is not yet a reproducible,
fully validated lab release across all three distributions.

## Issues corrected during review

- Source extraction now happens in a private staging directory. A partial
  tree cannot become a successful installation merely because one header
  exists. Successful trees are published separately; temporary downloads and
  failed extractions are cleaned up.
- Launchpad downloads use partial filenames until complete. Only the exact
  package revision is accepted, and dpkg-source verifies archive checksums.
- Existing Lima labs must match both distribution and profile release:
  Fedora 44, Ubuntu 26.04, or Debian 13.
- Native dependency detection rejects the old Textual API. The experimental
  Debian/Ubuntu native recipe uses the same drgn and Textual version ranges
  as the lab environment.
- APT installations wait for the dpkg lock for up to five minutes. Fatal APT
  update failures stop setup.
- Cached Fedora debug files must have readable ELF header/section metadata and
  the expected build ID. Rejected files are retained as debuginfo.invalid,
  rather than falsely reported as usable downloads.
- readelf is now part of the prerequisite checks.

## Verified behavior

Ubuntu and Debian setup successfully install or reuse debug packages and
matching sources, verify the running kernel release and type resolution, and
verify source availability before reporting Ready. Source downloads are
enabled by default, clearly announced before work begins, and can be skipped
with KEXPLORE_DOWNLOAD_SOURCE=0. Required DWARF remains required. Explicit
local source overrides take precedence over installed defaults.

The installation regression tests cover profile selection, release mismatch,
kernel/symbol version mismatch, source download opt-out, interrupted source
extraction, and invalid debug caches. ShellCheck and the targeted Python lint
checks pass.

The Fedora and Ubuntu catalog checks pass. Debian's 6.12 catalog has missing
layout support for mutex waiters, private futex hashes, and block hardware
queues. Source browsing and basic live structure navigation work on Debian;
this does not establish compatibility for every catalog entry or measurement.

## Remaining limitations

- Reproducibility is unfinished. Ubuntu/Debian images come from Lima's
  installed templates, repositories change, Python dependencies use version
  ranges, and retained artifacts are not owned by this project. Frozen images,
  package repositories, exact dependency locks, and automated fresh-install
  validation are needed for versioned lab releases.
- Ubuntu's older matching debug packages may disappear from its debug
  repositories. Ubuntu does not yet have Debian's automated kernel-pair
  recovery. Exact source recovery through Launchpad does not solve missing
  DWARF packages.
- Debian source recovery is limited to available exact repository revisions.
  It stops rather than substituting an unrelated revision.
- The native Debian/Ubuntu installer remains experimental and modifies system
  Python with --break-system-packages. The isolated /opt/kexplore environment
  is currently a Lima-lab feature. Native installation needs equivalent
  isolation before it should be recommended as an equally easy path.
- Fresh-install validation has been exercised on Apple Silicon. The complete
  distribution matrix on Intel Macs and native Linux is not established.
- The current workspace full suite is not green: catalog/UI expectation
  failures and a kernel-memory crawl fault remain outside this installation
  review. Do not infer a clean full application regression result from the
  installation tests alone.

The tool is for disposable VMs and dedicated learning machines, not production
servers. See [installation instructions](installation.md) for setup commands
and the source-download option.

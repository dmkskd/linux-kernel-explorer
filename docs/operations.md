# What belongs in the operations tab

Three tabs list three kinds of item, and two of them narrate. Tours are a
written explanation displayed over live data. Operations run something and
report what came back: an answer that no structure holds and that reading
memory alone cannot produce.

By that rule four of the six current entries belong here, and two are
narration that predates the tours tab.

## The four that belong

**clone flag isolation** (`operations/clone_experiment.py`) compiles
`tests/helpers/clone_matrix.c`, clones once per flag combination, holds the
children alive, and compares each child's `mm`, `files`, `fs`, `sighand`,
`signal` and `nsproxy` against the parent's. It also times 200 clones per
combination. This is the entry the tab is for: observation cannot attribute a
structure to a single flag, because a real caller such as `pthread_create`
passes five at once, so the only way to isolate one is to pass it alone.

**trace: ps -e** (`operations/command_trace.py`) runs a command under bpftrace
and reports the kernel interfaces it reached, keeping measured calls separate
from static source references. See [tracing-a-command.md](tracing-a-command.md).
It is the only entry that starts where a user starts, in userspace. It is a
preset of what `t` does with any command, not a separate facility.

**EEVDF next-entity selection** (`operations/algorithm.py`) recomputes
`avg_vruntime` per cfs_rq, marks each entity eligible or not, and names the
entity the scheduler would choose. It runs no program, but it computes an
answer instead of reading one, which puts it in the same category.

**copy-on-write after fork** (`operations/clone_experiment.py`) uses the same
helper with dirtied anonymous memory and compares the two address spaces with
`follow_page`. The matrix reports that fork gives the child a new `mm_struct`;
this reports what is behind it.

## The two that do not

**task wakeup to execution** and **page-fault resolution**
(`operations/walkthrough.py`) are hand-written step lists. Each step names a
function, resolved to file and line at display time, with optional live
structures attached. Nothing is executed and nothing is computed, so they are
narration, and the tours tab is where narration goes. `GuidedTour` already
answers `check()`, which these do not, so moving them also closes that gap.

## Open items

### Two entry kinds answer no check()

`Walkthrough` and `Algorithm` have no `check()`, so `--check` covers the
catalog and the tours and reports "0 failing entries" without resolving a
single operation. Every other entry kind answers it. An experiment's `check()`
should report whether gcc and the helper source are present rather than run
the experiment, the way `Measurement.check` reports that bpftrace is installed
rather than measuring.

### The scheduler analysis has nothing to analyse

On an idle machine every cfs_rq holds one entity and an empty rbtree, so the
EEVDF rows read "avg_vruntime not defined" and "nothing to choose: tree empty".
The rule is stated and the arithmetic is shown, but the selection it exists to
demonstrate never happens. The clone helper already knows how to start
processes and hold them.

### The COW counts are a sample reported as totals

`_cow_after_fork` stops after 256 pages per VMA. A run against 64 MiB of
ballast classified 552 pages, which is 2.2 MiB of it, and the rows say "pages
at the same physical page" with no mention of the cap. Either name the cap in
the rows or report a proportion.

### The page-fault steps open the wrong address space

The walkthrough describes resolving a userspace fault. Its `handle_mm_fault`
step opens `init_mm`, the kernel's own address space, and its `find_vma` step
opens pid 1's VMAs. Neither is a faulting address space. Two of its six
functions (`alloc_pages`, `set_pte_at`) are a macro and an inline with no
symbol, and an unresolvable step renders a blank source column without saying
why.

### Clone cost is measured twice

`measure > process > fork and thread creation cost` histograms `kernel_clone`
split by `CLONE_THREAD`, and its own blind spot says it records nothing unless
clones happen while it runs. The clone matrix answers the same question with
nine combinations instead of two buckets and generates the clones itself.

### Nothing links a path to its measurement

Three pairs state and compute the same thing from different tabs: the wakeup
walkthrough and `measure > sched > wakeup latency breakdown` name the same
functions, the EEVDF tour step restates the rule the EEVDF analysis computes,
and the clone matrix and clone cost both time `clone()`.

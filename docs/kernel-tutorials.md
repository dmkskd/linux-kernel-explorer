# Kernel tutorials

Work in progress: steps and tutorials change between versions.

Planning for the guided tutorials in the tutorials tab: the video each one
accompanies, what it inspects, its steps, and what is missing. The tab itself is
described in [kernel-concepts.md](kernel-concepts.md). Builders are in
`kexplore/operations/tour.py`. A script is written in plain English before any
step is coded; one scene is one `TourStep`.

## Status

Builders live in `kexplore/operations/tour.py`; every registered tutorial is in
`TOURS` there.

| Plan | Tutorial | Builder | Status |
| :--- | :--- | :--- | :--- |
| 1 | `USER_MEMORY_TYPES` | `_build_user_memory_steps` | **Done.** 10 live steps, accepted 2026-09-17. |
| 2 | `PAGE_TABLE_TRANSLATION` | `_build_page_table_steps` | **Done.** 9 live steps, built as a live walk rather than as plan 2 below. |
| 3 | `EEVDF_SCHEDULER` | `_build_eevdf_scheduler_steps` | Registered, still the scheduler tour that predates its plan below. |
| 4 | `PROCESS_ARCHITECTURE` | `_build_process_architecture_steps` | Registered, still the thread-group tour, not the `execve` path. The scene by scene script for the rewrite is in its section below. |
| 5 | `PROCESS_LIFECYCLE` | `_build_process_lifecycle_steps` | Registered, still the namespaces tour, not the ELF format. |
| 6 | `VMSCAN_RECLAIM` | none | Not started. |

What the two finished memory tutorials do and do not cover:

- Plan 1 landed steps 1 to 5, 8 and 9 (address space, text, shared library,
  heap, stack, THP, `rss_stat`). The shared memory steps 6 and 7 (tmpfs, POSIX
  `shm_open`, `memfd_create`, System V) were not wired up: `_resolve_shm` and
  `_resolve_memfd` exist in `tour.py` but no step calls them.
- Plan 2's itinerary below was not the one built. The implemented tutorial walks
  home, `task_struct`, `mm_struct.pgd`, VMAs, one VMA, its resident pages, the
  `struct page`, and the zone. Missing from the plan: the 9-bit index
  decomposition, per-level descent rows, and the hardware PTE flag decode
  (Present, Dirty, Accessed, NX). Steps 8 and 9 still end on `struct page` and
  `struct zone`, the two the VMScan proposal below wants to own.

## User memory types (`USER_MEMORY_TYPES`)

### Video against the current tutorial

- **Companion video**: *Linux Memory Management: Types of User Memory* (`6dwzZEFEgWE`)
- **What the video covers**: The video categorizes user memory into file-backed memory, anonymous memory, shared memory, and huge pages. It spends significant focus on the four distinct Linux shared memory mechanisms (tmpfs, POSIX `shm_open`, `memfd_create`, and System V IPC `shmget`/`shmat`), as well as transparent huge pages and `/proc/<pid>/smaps` accounting.
- **Current tutorial path**: Steps 1-2 navigate from the kexplore starting screen into `init`. Steps 3-8 inspect `task_struct -> mm_struct -> VMAs`, drill into the executable text segment (`r-xp`) and its backing file/inode, anonymous heap/stack, and physical `struct page`.
- **Gaps to address**:
  - Shared memory is missing. The tutorial does not inspect shared VMAs (`rw-s`), tmpfs mappings, or memfd areas.
  - Huge pages are missing. Transparent huge page flags or hugetlbfs VMAs are not demonstrated.
  - Steps 1 and 2 spend time on generic home-menu navigation rather than immediate memory classification.

### Planned steps (implemented, minus steps 6 and 7)

Companion video: *Linux Memory Management: Types of User Memory* (`6dwzZEFEgWE`)

1. **Address Space Overview (`mm_struct`)**: Inspect user address space bounds (`start_code..end_code`, `start_brk..brk`, `start_stack`).
2. **File-Backed Private Memory (`r-xp` Text Segment)**: Inspect the executable code VMA, verifying `vma->vm_file` points to the on-disk binary and `VM_SHARED` is unset.
3. **File-Backed Shared Libraries (`libc.so`)**: Inspect mapped dynamic library VMAs and how multiple processes share the same read-only physical code pages.
4. **Anonymous Memory: The Process Heap (`[heap]`)**: Inspect the dynamic heap VMA grown via `brk()`, showing `vma->vm_file == NULL` and `vma->anon_vma`.
5. **Anonymous Memory: The Execution Stack (`[stack]`)**: Inspect the thread stack VMA with the `VM_GROWSDOWN` flag and user stack pointer.
6. **Shared Memory Type 1 & 2: POSIX Shared Memory and tmpfs**: Inspect a shared memory VMA (`rw-s`) backed by `/dev/shm` or tmpfs with `VM_SHARED`.
7. **Shared Memory Type 3: Anonymous Shared (`memfd_create`)**: Inspect a descriptor-backed anonymous shared mapping sharing pages across unrelated processes.
8. **Huge Page Memory (Transparent Huge Pages - THP)**: Inspect 2MB-aligned memory regions and Transparent Huge Page indicators in `/proc/<pid>/smaps`.
9. **Memory Accounting & Working Sets**: Inspect `mm->rss_stat` (`MM_FILEPAGES`, `MM_ANONPAGES`, `MM_SHMEMPAGES`) and correlate with `/proc/<pid>/smaps` (RSS vs PSS).

## Page tables and address translation (`PAGE_TABLE_TRANSLATION`)

### Video against the current tutorial

- **Companion video**: *Linux Memory Management: Page Tables & Address Translation* (`Y2oSY_eenQ4`)
- **What the video covers**: The 4-level virtual address decomposition (PGD, PUD, PMD, PTE bit ranges), hardware MMU translation walks, hardware entry status bits (Present, Writable, User, Dirty, Accessed, No-Execute), TLB caching, and page faults.
- **Current tutorial path**: Steps 1-2 navigate menus. Steps 3-7 inspect `init_mm`, bounds in `mm_struct`, VMA permissions, page tables, and physical page frames. Steps 8-9 inspect reverse mapping (`anon_vma`) and NUMA buddy allocator zones (`struct zone`).
- **Gaps to address**:
  - The tutorial displays a list of resident pages rather than decoding the bitmask flags of actual hardware page table entries (PTE flags: Present, Dirty, Accessed, No-Execute).
  - Steps 8 and 9 diverge into page reclamation (RMAP) and NUMA zone free lists, which belong to memory depletion/reclaim topics rather than hardware address translation.

### Planned steps (tutorial implemented, this itinerary not)

Companion video: *Linux Memory Management: Page Tables & Address Translation* (`Y2oSY_eenQ4`)

1. **MMU Translation Root (CR3 / TTBR0 Register)**: Inspect `mm->pgd` and the hardware MMU base register loaded upon context switch.
2. **4-Level Virtual Address Decomposition**: Show how 48-bit virtual addresses split into 9-bit indices for PGD, PUD, PMD, and PTE plus a 12-bit page offset.
3. **Page Global Directory (PGD Level)**: Walk `pgd_t` entries indexed by virtual address bits 47:39.
4. **Intermediate Directories (PUD & PMD Levels)**: Traverse down through `pud_t` (bits 38:30) and `pmd_t` (bits 29:21).
5. **Huge Page PMD Folding**: Demonstrate how 2MB huge pages fold translation at the PMD level, skipping the leaf PTE level entirely.
6. **Leaf Page Table Entry (PTE Level)**: Reach the 4KB leaf `pte_t` (bits 20:12) and inspect its raw 64-bit value.
7. **Hardware PTE Status Flag Bits**: Decode the hardware protection bits: Present (`P`), Read/Write (`RW`), User (`US`), Accessed (`A`), Dirty (`D`), and No-Execute (`NX`).
8. **Physical Address Resolution (PFN to RAM)**: Extract Page Frame Number (PFN) from bits 12:51, compute physical memory address, and verify via `/proc/<pid>/pagemap`.
9. **Demand Paging & Page Fault Exception**: Walk through `handle_mm_fault()`, showing how unmapped addresses trigger allocation on first read or write.

## Scheduler and task selection (`EEVDF_SCHEDULER`)

### Video against the current tutorial

- **Companion video**: *Linux Scheduler and Process Wait Chains* (`SdpaIMBOdv4`)
- **What the video covers**: Diagnosing why processes wait, distinguishing between runqueue latency and blocked states, inspecting wait queue heads (`wait_queue_head_t`), tracking mutex contention chains, and tracing off-CPU blocking with tracepoints.
- **Current tutorial path**: Steps 1-5 walk per-CPU runqueues, `struct rq`, `struct cfs_rq`, and `struct sched_entity` to explain the mathematical EEVDF virtual timeline and lag calculation. Steps 6-8 inspect `rq->curr`, process sleep states, and `__schedule` context switches. Step 9 measures scheduler event rates with bpftrace.
- **Gaps to address**:
  - Topic mismatch between scheduling algorithm versus wait chains: the video focuses on wait queues, lock contention, and off-CPU blocking, whereas the tutorial focuses on CFS/EEVDF mathematical lag and deadline picking.

### Planned steps

Companion video: *Linux Scheduler and Process Wait Chains* (`SdpaIMBOdv4`)

1. **Per-CPU Runqueues (`struct rq`)**: Inspect live CPU runqueue structures and currently running task pointers (`rq->curr`).
2. **Process Execution States (`__state`)**: Inspect `TASK_RUNNING`, interruptible sleep (`TASK_INTERRUPTIBLE`), and uninterruptible sleep (`TASK_UNINTERRUPTIBLE`).
3. **Voluntary vs Involuntary Context Switches**: Inspect `task->nvcsw` (sleep, I/O wait) versus `task->nivcsw` (time slice expiration, preemption).
4. **Wait Queues (`wait_queue_head_t`)**: Inspect kernel wait queues where sleeping tasks enqueue awaiting timers, events, or disk I/O.
5. **Mutex Contention & Wait Chains**: Follow `task->blocked_on` to trace lock contention chains and identify the lock holder.
6. **The CFS/EEVDF Runnable Tree (`cfs_rq`)**: Inspect the red-black tree of tasks currently competing for CPU execution.
7. **Virtual Runtime & Deadlines**: Inspect `se->vruntime`, `avg_vruntime`, scheduling lag, and earliest eligible deadlines.
8. **Context Switch Mechanics (`context_switch`)**: Follow `__schedule()` into architecture-specific register and stack swapping in `switch_to`.
9. **Live Scheduler Telemetry (bpftrace)**: Run live tracing of `sched_switch` and `sched_wakeup` latency to measure runqueue waiting delays.

## How Linux runs a program (`PROCESS_ARCHITECTURE`)

### Video against the current tutorial

- **Current tutorial path**: Steps 1-2 navigate menus. Steps 3-9 inspect thread group sharing across active threads (`auditd`), checking `CLONE_VM` (shared `mm_struct`), maple tree VMAs, file descriptors, and shared signal handlers (`CLONE_SIGHAND`).
- **Gaps to address**:
  - The tutorial is a multi-threading and thread-group sharing exploration, whereas the companion video is about binary loading, `execve`, auxiliary vectors, and dynamic linking.

Companion video: *How Linux Runs a Program* (`9jNWc8RUFvs`).

### What the video covers

Running a command is several distinct handovers rather than one act:

- The shell does not run the program. It makes a copy of itself, and that copy
  replaces its own image with the program. A process and the program it runs
  are separate things, joined late.
- The parent waits. The prompt returns when the child exits and unblocks it.
- The kernel does not complete the loading. It maps the dynamic loader named in
  the ELF and jumps there. The loader maps libc and the other dependencies
  before any of the program's own code runs.
- The program's visible work is a single `write` at the end.

The video uses strace because each handover crosses the syscall boundary.

### What our tutorial covers

The same sequence read from the kernel side. A process that has been running
for hours still holds, in its `task_struct` and `mm_struct`, the record of which
process created it, which kernel code replaced its image, which file the image
came from, what the kernel passed to the loader, and what the loader mapped
afterwards. The tutorial picks a running process and reconstructs its start
from those fields.

### The steps

1. **Who created it.** Press `parent`. The process is a copy of its parent, and
   `task->real_parent` records which.
2. **What it was before.** Press the binfmt row on `mm_struct`. `execve` offers
   the file to each registered format handler; the one that accepted it is at
   `mm->binfmt`, and its `load_binary` resolves to `load_elf_binary`.
3. **What it is now.** Press `exe_file`, the `struct file` the image was loaded
   from, kept for the life of the process.
4. **What the parent is doing.** On the parent, press `children`. It is in
   interruptible sleep in `wait`, and the child is an entry in its list.
5. **What execve copied.** argv and the environment, between `arg_start` and
   `arg_end`. The old address space was discarded, so these were copied into
   the new one before the program could read them.
6. **What the kernel passed to the loader.** `mm->saved_auxv`: `AT_ENTRY` is
   the program's entry point, `AT_BASE` the address the loader was mapped at.
7. **A cross-check.** The `[vdso]` mapping starts at the address
   `AT_SYSINFO_EHDR` reports. Two structures written by different parts of
   `execve` agree.
8. **What the loader mapped.** The library mappings, several per library
   because each segment has its own permissions. All of this happened before
   the program's first instruction.
9. **Where the output goes.** Press `fd 1`. What the descriptor points at
   decides whether the characters reach a terminal, a pipe, or a file.

### Notes

- No task on the lab VM has a shell as its parent, so the tutorial cannot open
  on "the command you just typed". Pick any process with an `exe_file`, or add
  a helper that holds a child stopped just after `execve`.
- Needs building: an `mm_struct › exe_file` link, a binfmt row resolving
  `load_binary` to its symbol, decoded `saved_auxv` rows, argv rows via
  `drgn.helpers.linux.mm.cmdline`. All four were checked against the lab kernel
  and work.
- The current tutorial is about threads sharing state through clone flags. It
  is either replaced by this or renamed to what it is.

## Process lifecycle and isolation (`PROCESS_LIFECYCLE`)

### Video against the current tutorial

- **Companion video**: *Inside a Linux Executable File* (`7Bvx5Gd99F0`)
- **What the video covers**: Internal layout of ELF binaries, including ELF headers, program headers (`LOAD`, `DYNAMIC`, `INTERP`), section headers (`.text`, `.data`, `.rodata`, `.bss`), and symbol tables.
- **Current tutorial path**: Steps 1-2 navigate from process catalog into `init_task`. Steps 3-9 inspect credentials (`struct cred`), namespace isolation (`struct nsproxy`), network namespaces (`struct net`), file descriptor tables (`struct files_struct`), and filesystem inodes.
- **Gaps to address**:
  - The companion video is strictly about ELF executable formats, whereas the tutorial is about kernel namespaces and container isolation.

### Planned steps

Companion video: *Inside a Linux Executable File* (`7Bvx5Gd99F0`)

1. **ELF Header (`Elf64_Ehdr`)**: Magic bytes, machine architecture, entry point virtual address, and header offsets.
2. **Program Headers (`Elf64_Phdr`)**: Segments that describe runtime memory layout (`LOAD`, `DYNAMIC`, `INTERP`, `NOTE`, `GNU_STACK`).
3. **Segment Permissions & Alignment**: How `p_flags` (`PF_R`, `PF_W`, `PF_X`) dictate VMA flags and 4KB page alignment.
4. **Section Headers (`Elf64_Shdr`)**: Linker sections inside the file (`.text`, `.rodata`, `.data`, `.bss`, `.plt`, `.got`).
5. **Sections to Segments Mapping**: How multiple sections map into single contiguous memory segments.
6. **The Global Offset Table (GOT) & Procedure Linkage Table (PLT)**: Relocation tables enabling position-independent execution (PIE).
7. **Symbol Tables (`.symtab` & `.dynsym`)**: Function and variable symbol resolution tables.
8. **String Tables (`.strtab` & `.dynstr`)**: Stored null-terminated strings referenced by symbol names.
9. **Kernel Loading Interface (`load_elf_binary`)**: Step-by-step kernel code that reads and parses these exact headers into memory.

## Memory reclaim and VMScan (`VMSCAN_RECLAIM`)

### Video against the current tutorial

- **Companion video**: *Linux Memory Management - VMScan* (`tpRlczF0pqw`)
- **What the video covers**: Kernel virtual memory scanning (`mm/vmscan.c`), background reclaim with `kswapd`, foreground direct reclaim, `/proc/zoneinfo` watermarks (`min`, `low`, `high`), page fault memory consumption (chapter at 18:10 / 19:35), memory depletion testing (`tail /dev/zero`), `sar -B` paging metrics (`pgscank`, `pgscand`, `pgsteal`), and kernel tracepoint profiling with `perf`.
- **Relationship to existing codebase**:
  - `kexplore` already accesses `struct zone` (watermarks, free areas) and `struct pglist_data` (NUMA nodes) via `kexplore/catalog/links.py`.
  - In `PAGE_TABLE_TRANSLATION`, steps 8 and 9 previously wandered into reverse mapping and NUMA zone free lists. Adding a dedicated VMScan tutorial gives these structures their proper conceptual home.
  - Live tracing of `vmscan:mm_vmscan_kswapd_wake` and `vmscan:mm_vmscan_direct_reclaim_begin` can leverage `kexplore/operations/command_trace.py`.

### Planned steps (proposed next)

Companion video: *Linux Memory Management - VMScan* (`tpRlczF0pqw`)

1. **System Memory Balance (`/proc/meminfo`)**: Inspect total, free, available, cached, and anonymous memory distribution.
2. **NUMA Node Root (`struct pglist_data`)**: Navigate into the primary NUMA memory node (`contig_page_data` or `node_data[0]`).
3. **The Asynchronous Reclaim Daemon (`pgdat->kswapd`)**: Follow the kernel thread pointer to `struct task_struct *kswapd`, inspecting its state and execution statistics.
4. **Memory Zones & Watermarks (`struct zone`)**: Inspect `zone->_watermark[WMARK_MIN]`, `WMARK_LOW`, and `WMARK_HIGH` to observe hysteresis thresholds where reclaim triggers.
5. **Buddy Allocator Free Lists (`zone->free_area`)**: Examine free page counts across allocation orders (order 0 through order 10).
6. **Active vs Inactive LRU Queues (`struct lruvec`)**: Inspect how evictable pages sit in inactive and active lists (`LRU_INACTIVE_FILE`, `LRU_ACTIVE_FILE`, `LRU_INACTIVE_ANON`).
7. **Demand Paging to Watermark Depletion (19:35 Chapter)**: Connect virtual page allocations to physical free page reduction, showing how allocation pressure drops free pages toward `watermark_low`.
8. **Direct Reclaim vs Background Reclaim**: Contrast non-blocking asynchronous scanning (`kswapd`) with blocking synchronous reclamation (direct reclaim when reaching `watermark_min`).
9. **Live Scanning Telemetry (`sar -B` / tracepoints)**: Inspect real-time `pgscank/s`, `pgscand/s`, and `pgsteal/s` counters, accompanied by safe bounded memory pressure and tracepoint telemetry (`vmscan:mm_vmscan_kswapd_wake`).

### Video overview and chapter breakdown
- **Companion video**: *Linux Memory Management - VMScan | DEEP LINUX* (`tpRlczF0pqw`)
- **Video length**: ~37 minutes (2237 seconds)
- **Topic**: How the Linux kernel monitors memory depletion and reclaims physical pages via the virtual memory scanning subsystem (`mm/vmscan.c`).

#### Full video chapters
- `00:00`: Introduction to memory scanning and reclamation
- `00:43`: `/proc/meminfo` accounting (Buffers vs Cached vs Free vs Available)
- `02:22`: Paging in and paging out metrics (`pgpgin/s`, `pgpgout/s`)
- `07:47`: Page scans (`pgscand/s` direct reclaim vs `pgscank/s` kswapd scans)
- `11:20`: `/proc/zoneinfo` and zone watermark thresholds (`min`, `low`, `high`)
- `12:27`: Free pages tracking across Buddy allocator orders
- `18:10`: **Page Faults** (covering `t=1175s` / `19:35`)
- `23:30`: Page Ins
- `24:03`: **Tail Dev0** (live memory depletion using `tail /dev/zero`)
- `31:58`: **Perf tracing** of kernel `vmscan` tracepoints

#### Analysis of timestamp `t=1175s` (19:35)
At 19:35, the presenter covers the transition between virtual allocation and physical page consumption:
- Virtual allocations remain mere metadata entries in user space until touched.
- Accessing an unmapped page triggers a page fault exception (`handle_mm_fault()`), which allocates a physical frame from the Buddy allocator.
- As page faults continuously consume free pages, the free count in `struct zone` drops below the `watermark_low` threshold.
- Reaching this watermark triggers the asynchronous wake-up of `kswapd`.
- This theoretical setup directly motivates the practical demonstration at 24:03, where running `tail /dev/zero` rapidly exhausts memory, forcing `kswapd` into action, followed by synchronous direct reclaim once memory drops to `watermark_min`.

### Architectural rationale: why VMScan belongs in kexplore

1. **Resolves the structural mismatch in existing tutorials**:
   - In the initial draft of `PAGE_TABLE_TRANSLATION`, steps 8 and 9 inspected `anon_vma` (reverse mapping) and `struct zone` (NUMA free lists).
   - In a hardware address translation tour, diving into page reclamation and NUMA free lists felt disconnected from MMU page table walks (PGD -> PUD -> PMD -> PTE).
   - Creating a dedicated `VMSCAN_RECLAIM` tutorial provides the natural conceptual home for physical zones, page frame flags, and reverse mapping.

2. **Leverages existing live drgn structures**:
   - `kexplore` already links `struct page` to its allocating `struct zone`, and `struct zone` to its parent `struct pglist_data` in [`kexplore/catalog/links.py`](file:///Users/filippo/dev/linux/linux-kernel-explorer/kexplore/catalog/links.py#L289-L305).
   - `struct zone` directly exposes `_watermark[WMARK_MIN]`, `WMARK_LOW`, `WMARK_HIGH`, and `free_area` (free page counts per order).
   - `struct pglist_data` contains the direct task pointer `pgdat->kswapd` (the live `task_struct` of the kernel's background reclaim daemon), as well as the active/inactive LRU lists (`struct lruvec`).

3. **Multi-layered correlation (drgn + userspace + tracing)**:

| Video Concept | Kernel Structure in drgn | kexplore Surface | Userspace Telemetry Companion |
| :--- | :--- | :--- | :--- |
| **Zone Watermarks** | `struct zone` (`_watermark[WMARK_MIN/LOW/HIGH]`) | `mm -> zones` | `cat /proc/zoneinfo` |
| **NUMA Node & kswapd** | `struct pglist_data` (`pgdat->kswapd`) | `mm -> NUMA nodes` | `pgrep -a kswapd` |
| **Buddy Allocator Free Lists** | `zone->free_area[MAX_ORDER]` | `page -> first 512 frames` | `cat /proc/buddyinfo` |
| **LRU Lists & Eviction** | `struct lruvec` (`lists[NR_LRU_LISTS]`) | `mm -> lruvec` | `cat /proc/meminfo` |
| **Page Flags & Frame State** | `struct page` (`PG_lru`, `PG_active`, `PG_referenced`) | `page -> resident` | `/proc/kpageflags` |
| **Page Scanning Metrics** | Reclaim scan counters | `measure` subsystem | `sar -B 1` / `vmstat 1` |
| **Live Reclaim Tracing** | `vmscan` tracepoints | [`kexplore/operations/command_trace.py`](file:///Users/filippo/dev/linux/linux-kernel-explorer/kexplore/operations/command_trace.py) | `perf record -e vmscan:*` / `bpftrace` |

### Complete 9-step tutorial itinerary

1. **System Memory Balance (`/proc/meminfo`)**:
   Inspect system-wide memory counters, distinguishing unallocated free pages from evictable page cache and non-evictable anonymous allocations.
2. **NUMA Node Root (`struct pglist_data`)**:
   Navigate into the primary NUMA memory node structure (`contig_page_data` or `node_data[0]`), inspecting total present and managed pages.
3. **The Asynchronous Reclaim Daemon (`pgdat->kswapd`)**:
   Follow `pgdat->kswapd` to inspect the dedicated kernel thread responsible for background reclamation.
4. **Memory Zones & Watermarks (`struct zone`)**:
   Inspect `zone->_watermark[WMARK_MIN]`, `WMARK_LOW`, and `WMARK_HIGH`, explaining the hysteresis gap where `kswapd` wakes up (`low`) and sleeps (`high`).
5. **Buddy Allocator Free Lists (`zone->free_area`)**:
   Inspect available page counts across allocation orders (order 0 up to order 10) to observe physical fragmentation.
6. **Active vs Inactive LRU Queues (`struct lruvec`)**:
   Inspect how evictable pages queue into active and inactive lists (`LRU_INACTIVE_FILE`, `LRU_ACTIVE_FILE`, `LRU_INACTIVE_ANON`), explaining how unreferenced pages are selected for eviction.
7. **Demand Paging to Watermark Depletion (19:35 Chapter)**:
   Trace the page fault path (`handle_mm_fault`), showing how allocations consume free frames and push free page counts toward the `low` watermark.
8. **Direct Reclaim vs Background Reclaim**:
   Contrast asynchronous background scanning (`kswapd`) with synchronous blocking (`__alloc_pages_direct_reclaim`) when allocations reach the `min` watermark.
9. **Live Scanning Telemetry (`sar -B` / tracepoints)**:
   Observe real-time `pgscank/s`, `pgscand/s`, and `pgsteal/s` counters, accompanied by live tracepoint capture of `vmscan:mm_vmscan_kswapd_wake`.

### Workload safety and controlled execution
In the video at 24:03, the presenter uses `tail /dev/zero` to force memory exhaustion. In a virtual machine environment (such as Lima VM):
- An unconstrained `tail /dev/zero` rapidly exhausts all physical memory and swap, causing the Out-of-Memory (OOM) killer to terminate unpredictable processes, potentially killing SSH daemons or container runtimes.
- **kexplore design decision**:
  Rather than running unconstrained `tail /dev/zero`, kexplore should use a **bounded memory stressor** (for example, a helper with strict resource limits via `prlimit` or cgroups that temporarily consumes 60-70% of available memory). This safely drops available memory below `watermark_low`, demonstrating `kswapd` wake-up and `sar -B` page scanning without risking virtual machine crashes.

## What is this machine waiting for? (`WAITING`)

Companion video: *Profiling Linux Activity for Performance and Troubleshooting*
(`YEWp3O7Kem8`).

### What the video covers

Thread state can be sampled from /proc without instrumenting anything, because
the kernel maintains it continuously. Two of its points are worth keeping:

- The state letter is a summary. `D` is uninterruptible sleep, which is as often
  a lock on an inode as a read from a device; the wait channel or the kernel
  stack says which.
- Both `R` and `D` count toward the load average, so a machine can report a high
  load with idle CPUs.

The tools in the video (`ps` through `sort | uniq -c`, then psnapper, then a
C sampler) exist because reading those fields out of /proc text quickly is the
difficult part.

### What our tutorial covers

A wait has two ends. /proc names the near end, so the video's method is to count
names. From the structures the far end is reachable as well: the thread waiting
and the thread holding, the futex address and the other tasks parked on it, the
blocked read and the disk queue it is waiting on.

### The steps

1. **The census.** All tasks grouped by `__state`, the same counts as
   `ps -eL -o state | sort | uniq -c`, read from the field the file prints.
   R plus D is the load average.
2. **What running means.** The per-CPU runqueues hold at most one running task
   per CPU; the rest counted as R are queued.
3. **Waiting for a time.** The queued hrtimers, each with an expiry and the task
   it will wake.
4. **Waiting for another thread.** The futex waiters, grouped by the address
   they are parked on. Tasks on the same address are waiting on the same lock.
5. **The far end of a lock.** From a waiter to the holder through the mutex's
   `owner`, and the derived row saying whether it is held and by whom.
6. **Waiting for a disk, or not.** From a blocked task to its open file, inode,
   mount, disk, request queue, and the requests in flight. With nothing in
   flight, the wait is not the device.
7. **Which threads these are.** `comm`, and the argv copied at exec, which is
   where PostgreSQL and the JVM put their worker labels.
8. **What this view cannot answer.** One instant, read in detail. Sampling a
   shallow view repeatedly answers how long and how often instead; kexplore's
   measurement entries (off-CPU duration, runqueue latency) do that with
   bpftrace.

### Notes

- Needs building: a wait channel. Nothing in kexplore takes a stack trace,
  while drgn can unwind a blocked task with `prog.stack_trace(task)`, which
  gives the `wchan` and `kstack` columns without /proc.
- Already in the catalog: task state with a decoder, per-CPU runqueues, queued
  hrtimers with their `hrtimer_sleeper`, futex waiters with their addresses,
  `mutex › owner` and the `= state` derived row, and the open file to disk
  chain.
- An idle lab VM shows none of the interesting states, so this needs a helper
  that creates contention, as `stuck_socket.py` and `clone_demo.py` stage their
  conditions.

## Where did this process's time go? (`TIME_ACCOUNTING`)

No companion video.

### What our tutorial covers

A process is running on a CPU, ready to run and waiting for one, or blocked.
`top` and `ps` report the first and group the rest as not running. The kernel
counts all three separately per task and keeps the totals in the task_struct,
so the difference between a process that is slow because the CPUs are busy and
one that is slow because it is waiting for something is readable without
tracing.

Six of the seven steps stay on one task_struct, which is why this is a
reasonable first tutorial to build: no walking between structures, no helper
program.

### The steps

1. **What it is doing now.** `__state`, decoded.
2. **Time on a CPU.** `utime` and `stime` next to `se.sum_exec_runtime`: two
   accounting paths for the same quantity, and they agree.
3. **Time ready but not running.** Into `sched_info`: `run_delay` is the total
   time the task spent on a runqueue without getting a CPU, `pcount` how many
   times it was queued, `max_run_delay` the longest single wait.
4. **Why it stopped running.** `nvcsw` against `nivcsw`: it asked to wait, or
   it was preempted with work left. The two indicate different problems.
5. **Time blocked.** `stats.sum_block_runtime`, `iowait_sum` and `nr_wakeups`:
   time off the runqueue, and how often something woke it.
6. **The queue that produced the delay.** Follow the task to its runqueue and
   see how many other tasks are queued there.
7. **The counters that are off.** `task->delays` is a null pointer unless
   `kernel.task_delayacct` is set. With it on, the structure is allocated and
   carries separate totals for block I/O, page reclaim, swap-in and thrashing.
   The viewer sets the sysctl and returns to the same row.

### The disagreement in step 3

`sched_info.run_delay` and `stats.wait_sum` both count time spent waiting to
run, and on the lab VM they differ by more than a factor of ten for pid 1. The
step shows both and offers the source of each update site with `s` rather than
giving a reason for the gap.

### Measurements

Lab VM, kernel 6.19.10-300.fc44.aarch64, read through drgn. Facts about that
machine, not claims about the tool.

| Checked | Result |
| :--- | :--- |
| `sched_schedstats` | 1, so `sched_info` is populated |
| pid 1 `sched_info` | `pcount` 4207, `run_delay` 69.5 ms, `max_run_delay` 3.16 ms |
| pid 1 CPU time | `utime` 602 ms plus `stime` 583 ms against `sum_exec_runtime` 1.19 s |
| pid 1 switches | `nvcsw` 3161, `nivcsw` 1047 |
| pid 1 `stats` | `wait_sum` 3.7 ms, `wait_max` 690 us, `sum_block_runtime` 9.1 ms, `nr_wakeups` 506 |
| `kernel.task_delayacct` | 0, and `task->delays` is a null pointer, not a zeroed structure |

`sched_info` and `stats` are embedded structures, which the field view already
opens, and the `runqueue` link exists, so no catalog work is needed.

## Reference talks

Beyond the Deep Linux foundational tutorials, two industry keynote presentations provide critical insights into how Linux memory management functions in containerized and hyperscale production environments.

### Presentation 1: Linux Memory Management and Containers (Gerlof Langeveld)
- **Video link**: [Linux Memory Management and Containers - Gerlof Langeveld, AT Computing (Linux Foundation)](https://www.youtube.com/watch?v=ql1axx--8sI&t=3472s)
- **Key timestamp (`t=3472s` / 57:52)**: Transition to cgroups v2 memory controller parameters.
- **Presenter**: Gerlof Langeveld (creator and maintainer of the `atop` performance monitoring tool).
- **Core topics covered**:
  - **Memory fundamentals**: Demand paging, page fault mechanisms, page scanning, swapping algorithms, and NUMA node interleaving.
  - **Container abstraction reality**: Containers are not distinct virtual machines; they are standard Linux processes governed by namespaces (isolation) and cgroups (resource limits).
  - **Cgroups v2 memory controller**:
    - `memory.max`: Hard upper limit. When exceeded and reclaim fails, triggers the cgroup-level OOM killer without affecting external processes.
    - `memory.high`: Soft throttle threshold. When exceeded, the allocating process is throttled and forced to perform synchronous direct reclaim.
    - `memory.min` / `memory.low`: Best-effort and hard memory protection shields, preventing kernel page scanning from reclaiming pages allocated to critical containers.
    - `memory.swap.max`: Swap allocation caps per container.
  - **Mapping orchestration flags to kernel interfaces**:
    - Docker `--memory` and `--memory-reservation` directly map to `memory.max` and `memory.low`.
    - Kubernetes `resources.limits.memory` and `resources.requests.memory` map to `memory.max` and `memory.min`/`low`.
- **Application to kexplore**:
  - Bridges per-process virtual memory (`task->mm`) to container limits (`task->cgroups`).
  - Demonstrates how inspecting `/sys/fs/cgroup/...` alongside `struct task_struct` explains whether a process is under host memory pressure or container-level cgroup pressure.

### Presentation 2: Linux Memory Management at Scale (Chris Down)
- **Video link**: [Linux Memory Management at Scale: Under the Hood - Chris Down, Facebook (SREcon19)](https://www.youtube.com/watch?v=beefUhRH5lU&t=2373s)
- **Key timestamp (`t=2373s` / 39:33)**: Senpai / Memory sizing, PSI (Pressure Stall Information), and memory protection.
- **Presenter**: Chris Down (kernel developer at Meta/Facebook, author of `oomd`, cgroup v2 memory controller maintainer).
- **Core topics covered**:
  - **The myth of "free" memory**: Unused memory is wasted RAM. A healthy Linux system keeps physical RAM almost completely filled with page cache to avoid disk latency.
  - **The true purpose of swap ("In defence of swap")**: Swap does not exist merely as an emergency overflow when RAM runs out. Swap allows the kernel to evict anonymous pages that are never accessed (cold memory), freeing physical RAM frames for active disk page cache (hot memory). Without swap, the kernel can only reclaim page cache, leading to severe I/O thrashing.
  - **PSI (Pressure Stall Information)**: Why traditional CPU and memory utilization statistics fail to predict degradation. PSI (`/proc/pressure/memory`) tracks the percentage of wall-clock time that tasks are stalled waiting for memory allocations, page faults, or swap I/O.
  - **Proactive memory sizing and protection**:
    - Using `memory.low` to prevent host background reclaim (`kswapd`) from stealing page cache from latency-sensitive services.
    - Using userspace daemons (`oomd` / `senpai`) driven by PSI metrics to detect unrecoverable thrashing long before system hang.
- **Application to kexplore**:
  - Connects low-level physical page states (`PG_active`, `PG_referenced`, `PG_lru`) with high-level system health metrics.
  - Shows why inspecting `struct zone` watermarks and `lruvec` must be paired with `/proc/pressure/memory` to diagnose real-world memory saturation.

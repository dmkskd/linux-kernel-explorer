# Kernel concepts in kexplore

This document maps theoretical Linux kernel mechanisms, such as those presented
in deep-dive kernel tutorials (for example, the Deep Linux series), to concrete,
observable data structures and operational traces inside kexplore.

Instead of presenting static block diagrams, kexplore inspects a running kernel
through `/proc/kcore` with drgn, reads struct definitions from the kernel's DWARF,
and traces dynamic behavior with bpftrace.

---

## 1. Virtual memory and address spaces

### The concept
Linux processes execute in a private virtual address space managed by an `mm_struct`.
This address space is partitioned into non-overlapping Virtual Memory Areas
(`struct vm_area_struct`), each representing a contiguous virtual address range
with uniform permissions (`READ`, `WRITE`, `EXEC`, `SHARED`/`PRIVATE`).

User memory primarily divides into:
- **File-backed memory**: Backed by an underlying file on disk (for example, executable
  binaries and shared libraries). `vma->vm_file` points to the corresponding `struct file`.
- **Anonymous memory**: Has no file backing (for example, the program heap, execution
  stacks, and `mmap(MAP_ANONYMOUS)` allocations). `vma->vm_file` is NULL, and reverse
  mappings are tracked via `vma->anon_vma`.

### How kexplore shows it
- **Subsystem**: `mm` (`vmas_pid1`) or from `process`, by following `task -> mm -> VMAs`.
- **Structure fields**:
  - `vm_start` and `vm_end`: The virtual boundary addresses.
  - `vm_flags`: The access bits, decoded into permissions.
  - `vm_file`: Followable link to the backing file and its inode.
  - `anon_vma`: Followable link to the reverse-mapping tracking structure.
- **Curated links**:
  - `resident pages`: Walks the page tables for the VMA using `follow_page()`,
    displaying each virtual page currently backed by physical memory.

Example VMA rows observed for a process:
```
Virtual Address Range              Permissions          Backing
0xaaaac9a10000 - 0xaaaac9a30000    READ EXEC PRIVATE    /usr/lib/systemd/systemd
0xaaaac9a40000 - 0xaaaac9a41000    READ WRITE PRIVATE   /usr/lib/systemd/systemd
0xaaab062d7000 - 0xaaab06400000    READ WRITE PRIVATE   [heap]
```

---

## 2. Page tables and address translation

### The concept
Virtual addresses cannot be accessed by hardware directly. The Memory Management Unit
(MMU) traverses a multi-level hierarchical page table structure:
```
Virtual Address -> PGD -> P4D -> PUD -> PMD -> PTE -> Physical Page Frame (PFN)
```
Each valid Page Table Entry (PTE) points to a physical frame, represented in the
kernel by a `struct page` within the flat `vmemmap` array.

When an unmapped or unallocated virtual address is first accessed, the MMU triggers a
page fault exception. The kernel fault handler allocates a physical page, installs the
PTE, and restarts the faulting instruction.

### How kexplore shows it
- **Subsystem**: `page` (`resident pages of pid 1`, `pages by pfn`).
- **Live translation**: Each row displays the virtual address alongside its
  Page Frame Number (PFN), physical address, and decoded status flags:
  ```
  Virtual Address : 0xaaaac9a40000
  Physical PFN    : 0x24c35f (Physical Address: 0x24c35f000)
  Page Flags      : PG_uptodate | PG_dirty | PG_lru | PG_swapbacked
  ```
- **Operations walkthrough**: `page-fault resolution` (`operations` tab).
  Step-by-step trace showing each kernel function executed during fault handling:
  1. `do_page_fault`: Architecture-specific fault entry point.
  2. `find_vma`: Locates the `vm_area_struct` covering the faulting address in the maple tree.
  3. `handle_mm_fault`: Walks the page tables down to the target level, allocating missing tables.
  4. `do_anonymous_page`: Handles anonymous memory, mapping the shared zero page on reads.
  5. `alloc_pages`: Allocates a physical frame from the buddy allocator.
  6. `set_pte_at`: Writes the hardware PTE, completing the mapping.

---

## 3. Physical memory topology: NUMA nodes and zones

### The concept
Physical memory is organized into NUMA nodes (`pglist_data`), each containing multiple
memory zones (`struct zone`):
- `ZONE_DMA` / `ZONE_DMA32`: Lower physical address ranges reserved for legacy hardware.
- `ZONE_NORMAL`: Directly addressable physical memory used by the kernel and user pages.
- `ZONE_MOVABLE`: Hotpluggable memory containing only movable pages to prevent fragmentation.

Each zone maintains page allocators (buddy allocator free lists) and page watermarks
(`watermark[WMARK_MIN]`, `watermark[WMARK_LOW]`, `watermark[WMARK_HIGH]`). When free pages
drop below the low watermark, the background reclaimer (`kswapd`) is awakened.

### How kexplore shows it
- **Subsystem**: `mm` (`NUMA nodes (pglist_data)`, `zones`).
- **Structure fields**:
  - `spanned_pages`: Total range of physical pages covered by the zone.
  - `present_pages`: Physical pages actually present in the zone (excluding holes).
  - `managed_pages`: Pages managed by the buddy allocator.
  - `watermark`: Array of minimum, low, and high watermarks in page counts.

---

## 4. The scheduler: runqueues, task states, and EEVDF

### The concept
The Linux Completely Fair Scheduler (CFS), evolving into the Earliest Eligible Virtual
Deadline First (EEVDF) scheduler, selects tasks based on virtual runtime (`vruntime`)
and eligible deadlines.

- Each CPU maintains a `struct rq`, which embeds a root `cfs_rq`.
- Each schedulable entity is represented by a `struct sched_entity` (embedded in
  `task_struct` as `task->se`).
- An entity is eligible to run if its `vruntime <= avg_vruntime`. Among all eligible
  entities, EEVDF picks the one with the earliest deadline (`se->deadline`).

### How kexplore shows it
- **Subsystem**: `sched` (`runqueues (per-cpu)`, `currently running`).
- **Curated links**:
  - `rq -> curr`: The task currently executing on that CPU.
  - `task -> sched_entity`: CFS scheduling state (`vruntime`, load weights).
  - `sched_entity -> cfs_rq it sits on`: The queue managing this entity.
- **Operations algorithm**: `EEVDF next-entity selection`.
  Recomputes EEVDF selection from each CPU's live `cfs_rq` state:
  - Calculates `avg_vruntime = zero_vruntime + (sum_w_vruntime / sum_weight)`.
  - Determines lag and eligibility for every queued entity.
  - Identifies which entity the scheduler would choose next.
- **Operations walkthrough**: `task wakeup to execution`.
  Follows the execution path from sleeping to on-CPU:
  `try_to_wake_up` -> `select_task_rq_fair` -> `enqueue_task_fair` -> `ttwu_do_activate` -> `__schedule` -> `pick_next_task_fair` -> `context_switch`.
- **Measurements**:
  - `scheduler event rate`: Context switches and wakeups per second.
  - `wakeup latency breakdown`: Time spent between waking up and execution.
  - `on-CPU duration` and `off-CPU duration`: Real-time scheduling delay histograms.

---

## 5. Process creation: clone flags and copy-on-write

### The concept
The `clone()` system call creates execution contexts with fine-grained sharing:
- Traditional `fork()` creates a new process where all structures (`mm`, `files`, `fs`,
  `sighand`, `signal`) are duplicated or referenced.
- `pthread_create()` creates a thread sharing the address space (`CLONE_VM`), file tables
  (`CLONE_FILES`), filesystem state (`CLONE_FS`), and signal handlers (`CLONE_SIGHAND`).

Under `fork()`, the kernel uses Copy-on-Write (COW): the child receives a new `mm_struct`,
but page tables initially point to the parent's physical pages marked read-only.
Physical allocation occurs only when either process writes to a shared page.

### How kexplore shows it
- **Operations experiment**: `clone flag isolation` (`clone_matrix`).
  Runs a helper program executing each flag combination, holds child processes alive,
  and inspects their `task_struct` members:
  - Compares pointers (`mm`, `files`, `fs`, `sighand`, `signal`, `nsproxy`) against the parent.
  - Reads reference counts (for example, `mm_users`, `files->count`).
  - Benchmarks execution latency across 200 iterations for each flag combination.
- **Operations experiment**: `copy-on-write after fork` (`cow_after_fork`).
  Forks a child after allocating and dirtying 64 MiB of memory, then compares the
  parent and child page tables page by page:
  - Counts pages sharing the same physical PFN (unmodified).
  - Counts pages with different PFNs (copied on write).
  - Counts pages resident only in the parent.

---

## 6. Command tracing into the kernel

### The concept
Standard tools such as `ps` do not query a single kernel system call for process
lists; they read directory trees in `/proc`. Each `/proc/<pid>/` read invokes kernel
procfs handlers that format task data.

### How kexplore shows it
- **Operations algorithm**: `trace: ps -e` (`command_trace.py`).
  Runs `ps -e` under bpftrace probes placed at kernel interface boundaries:
  - Captures `do_sys_openat2` and `vfs_read` across procfs dentries.
  - Traces kernel execution stacks inside `seq_read_iter` and procfs formatting routines.
  - Identifies which `task_struct` fields are read by the formatting functions.

---

## 7. Live guided tutorials across the running kernel

The third top-level tab in kexplore (`tutorials`, accessible via `v` or the tab bar) organizes live, end-to-end explorations into guided walkthroughs. Instead of replaying static sessions, tutorials dynamically discover active processes and threads on the running machine, driving an exploration through their actual structures with live commentary:

| Category | Tutorial | Video Companion | Live Invariants & Structures Inspected |
| :--- | :--- | :--- | :--- |
| `process` | `Multi-threaded Process Architecture` | [How Linux Runs a Program](https://youtu.be/9jNWc8RUFvs) | Begins at process catalog (`subsystems › process`). Follows `processes` into thread group leader (`tgid == pid`), thread list via `signal->thread_head`, shared `mm_struct` (`CLONE_VM`, `mm_users`), maple tree VMA indexing, executable binary VMA, backing `struct file` and `struct inode`, and shared signal handlers (`struct sighand_struct`, `CLONE_SIGHAND`). |
| `process` | `Process Lifecycle: Clone & Namespaces` | [Inside a Linux Executable File](https://youtu.be/7Bvx5Gd99F0) | Begins at process catalog (`subsystems › process`). Follows `init (pid 1)` to init task swapper (`init_task`), credentials and capability sets (`struct cred`), namespace isolation proxy (`struct nsproxy`), network namespace stack (`struct net`), file descriptor table (`struct files_struct`), opened file (`struct file`), and underlying filesystem inode (`struct inode`). |
| `memory` | `User Memory Types & VMAs (Deep Linux)` | [Types of User Memory](https://youtu.be/6dwzZEFEgWE) | Begins at process catalog (`subsystems › process`). Follows `init (pid 1)` into `task_struct`, `mm_struct`, maple tree VMA list, executable text (`r-xp`, file-backed), backing ELF binary on disk (`struct file` & `struct inode`), dynamic heap and stack (`[heap]`, `[stack]`, `anon_vma`), and physical RAM page frame (`struct page`, PFN, page flags). |
| `memory` | `Page Tables & Address Translation (Deep Linux)` | [Page Tables & Address Translation](https://youtu.be/Y2oSY_eenQ4) | Begins at memory catalog (`subsystems › mm`). Follows `init_mm` into multi-level hardware translation walk: PGD register (`CR3`/`TTBR0_EL1`), address space bounds (`mm_struct`), VMA permissions, multi-level descent (PGD ➔ PUD ➔ PMD ➔ PTE), leaf PTE physical frame (`struct page`), reverse mapping (`anon_vma`), and physical memory zones (`ZONE_DMA`, `ZONE_NORMAL`). |
| `sched` | `EEVDF Scheduler & Task Selection (Deep Linux)` | [Scheduler & Process Wait Chains](https://youtu.be/SdpaIMBOdv4) | Begins at scheduler catalog (`subsystems › sched`). Follows `runqueues` into per-CPU `runqueues`, weighted CFS virtual timeline (`sum_w_vruntime`, `zero_vruntime`), sched entity lag (`lag = avg_vruntime - vruntime`), virtual deadlines, currently executing task (`curr`), process sleep states (`TASK_INTERRUPTIBLE`), and context switch register swapping. |

When launched (via `Enter` on a tutorial or `--tutorial <name>` from the CLI), the tutorial presents a dedicated overview landing page before stepping through live structures:
1. **Tutorial landing page**: An overview screen displaying the tutorial title, category, total steps badge, goal and live invariants, official YouTube companion video link, and the full sequence of steps. Use Up/Down arrows or PgUp/PgDn to scroll through the itinerary.
2. **Commentary banner**: A top panel (`#tutorial-banner`) displays the tutorial title, current step indicator (`[Step X of 8]`), single-line traversal flow roadmap, and narrative takeaway explaining the live kernel invariants.
3. **Dual highlight navigation**:
   - **Action coach mark**: Highlights the next action row with a blinking indicator (`👉 [ENTER] <target> ──▶ follow into ...`) and automatically pre-positions the cursor.
   - **Value payoff bulbs**: Highlights key structural data payoff on the current step with a bulb icon (`💡 <field> ★ <value>`).
4. **Walkthrough from Main Menu**: Step 1 always starts at the subsystem catalog, showing how an operator navigates from the catalog down into live structs.
5. **Controls**:
   - `n` or `Space`: Advance to the next tutorial step (or begin Step 1 from the landing page).
   - `p`: Return to the previous tutorial step (or return to the landing overview from Step 1).
   - `Enter`: Follow the action highlight into the next structure (or begin Step 1 from the landing page).
   - `a`: Toggle hands-free auto-play walkthrough mode.
   - `H`: Toggle commentary header variant (Pipeline + Insight vs Minimal HUD).
   - `Y`: Toggle highlight visual style (Coach mark + Bulb vs Chevron + Pill).
   - `Esc`: Exit the tutorial back to free exploration.

---

## 8. Video companion alignment and tutorial gap analysis

A review comparing what the Deep Linux video tutorials teach against what kexplore's guided tutorials currently inspect:

### 1. User memory types (`USER_MEMORY_TYPES`)
- **Companion video**: *Linux Memory Management: Types of User Memory* (`6dwzZEFEgWE`)
- **What the video covers**: The video categorizes user memory into file-backed memory, anonymous memory, shared memory, and huge pages. It spends significant focus on the four distinct Linux shared memory mechanisms (tmpfs, POSIX `shm_open`, `memfd_create`, and System V IPC `shmget`/`shmat`), as well as transparent huge pages and `/proc/<pid>/smaps` accounting.
- **Current tutorial path**: Steps 1-2 navigate from the kexplore starting screen into `init`. Steps 3-8 inspect `task_struct -> mm_struct -> VMAs`, drill into the executable text segment (`r-xp`) and its backing file/inode, anonymous heap/stack, and physical `struct page`.
- **Gaps to address**:
  - Shared memory is missing. The tutorial does not inspect shared VMAs (`rw-s`), tmpfs mappings, or memfd areas.
  - Huge pages are missing. Transparent huge page flags or hugetlbfs VMAs are not demonstrated.
  - Steps 1 and 2 spend time on generic home-menu navigation rather than immediate memory classification.

### 2. Page tables and address translation (`PAGE_TABLE_TRANSLATION`)
- **Companion video**: *Linux Memory Management: Page Tables & Address Translation* (`Y2oSY_eenQ4`)
- **What the video covers**: The 4-level virtual address decomposition (PGD, PUD, PMD, PTE bit ranges), hardware MMU translation walks, hardware entry status bits (Present, Writable, User, Dirty, Accessed, No-Execute), TLB caching, and page faults.
- **Current tutorial path**: Steps 1-2 navigate menus. Steps 3-7 inspect `init_mm`, bounds in `mm_struct`, VMA permissions, page tables, and physical page frames. Steps 8-9 inspect reverse mapping (`anon_vma`) and NUMA buddy allocator zones (`struct zone`).
- **Gaps to address**:
  - The tutorial displays a list of resident pages rather than decoding the bitmask flags of actual hardware page table entries (PTE flags: Present, Dirty, Accessed, No-Execute).
  - Steps 8 and 9 diverge into page reclamation (RMAP) and NUMA zone free lists, which belong to memory depletion/reclaim topics rather than hardware address translation.

### 3. Scheduler and task selection (`EEVDF_SCHEDULER`)
- **Companion video**: *Linux Scheduler and Process Wait Chains* (`SdpaIMBOdv4`)
- **What the video covers**: Diagnosing why processes wait, distinguishing between runqueue latency and blocked states, inspecting wait queue heads (`wait_queue_head_t`), tracking mutex contention chains, and tracing off-CPU blocking with tracepoints.
- **Current tutorial path**: Steps 1-5 walk per-CPU runqueues, `struct rq`, `struct cfs_rq`, and `struct sched_entity` to explain the mathematical EEVDF virtual timeline and lag calculation. Steps 6-8 inspect `rq->curr`, process sleep states, and `__schedule` context switches. Step 9 measures scheduler event rates with bpftrace.
- **Gaps to address**:
  - Topic mismatch between scheduling algorithm versus wait chains: the video focuses on wait queues, lock contention, and off-CPU blocking, whereas the tutorial focuses on CFS/EEVDF mathematical lag and deadline picking.

### 4. Multi-threaded process architecture (`PROCESS_ARCHITECTURE`)
- **Companion video**: *How Linux Runs a Program* (`9jNWc8RUFvs`)
- **What the video covers**: Program execution from `execve()`, ELF binary loading (`load_elf_binary`), initial user stack layout (`argc`, `argv`, `envp`, auxiliary vector `auxv`), and dynamic linking with `ld.so`.
- **Current tutorial path**: Steps 1-2 navigate menus. Steps 3-9 inspect thread group sharing across active threads (`auditd`), checking `CLONE_VM` (shared `mm_struct`), maple tree VMAs, file descriptors, and shared signal handlers (`CLONE_SIGHAND`).
- **Gaps to address**:
  - The tutorial is a multi-threading and thread-group sharing exploration, whereas the companion video is about binary loading, `execve`, auxiliary vectors, and dynamic linking.

### 5. Process lifecycle and isolation (`PROCESS_LIFECYCLE`)
- **Companion video**: *Inside a Linux Executable File* (`7Bvx5Gd99F0`)
- **What the video covers**: Internal layout of ELF binaries, including ELF headers, program headers (`LOAD`, `DYNAMIC`, `INTERP`), section headers (`.text`, `.data`, `.rodata`, `.bss`), and symbol tables.
- **Current tutorial path**: Steps 1-2 navigate from process catalog into `init_task`. Steps 3-9 inspect credentials (`struct cred`), namespace isolation (`struct nsproxy`), network namespaces (`struct net`), file descriptor tables (`struct files_struct`), and filesystem inodes.
- **Gaps to address**:
  - The companion video is strictly about ELF executable formats, whereas the tutorial is about kernel namespaces and container isolation.

### 6. Memory reclaim and VMScan (`VMSCAN_RECLAIM`)
- **Companion video**: *Linux Memory Management - VMScan* (`tpRlczF0pqw`)
- **What the video covers**: Kernel virtual memory scanning (`mm/vmscan.c`), background reclaim with `kswapd`, foreground direct reclaim, `/proc/zoneinfo` watermarks (`min`, `low`, `high`), page fault memory consumption (chapter at 18:10 / 19:35), memory depletion testing (`tail /dev/zero`), `sar -B` paging metrics (`pgscank`, `pgscand`, `pgsteal`), and kernel tracepoint profiling with `perf`.
- **Relationship to existing codebase**:
  - `kexplore` already accesses `struct zone` (watermarks, free areas) and `struct pglist_data` (NUMA nodes) via `kexplore/catalog/links.py`.
  - In `PAGE_TABLE_TRANSLATION`, steps 8 and 9 previously wandered into reverse mapping and NUMA zone free lists. Adding a dedicated VMScan tutorial gives these structures their proper conceptual home.
  - Live tracing of `vmscan:mm_vmscan_kswapd_wake` and `vmscan:mm_vmscan_direct_reclaim_begin` can leverage `kexplore/operations/command_trace.py`.

---

## 9. Proposed alignment plans for tutorials

To align kexplore's guided tutorials directly with the Deep Linux video companion curriculum, the following step-by-step itineraries can be implemented:

### Plan 1: User memory types (`USER_MEMORY_TYPES`)
Aligned with: *Linux Memory Management: Types of User Memory* (`6dwzZEFEgWE`)

1. **Address Space Overview (`mm_struct`)**: Inspect user address space bounds (`start_code..end_code`, `start_brk..brk`, `start_stack`).
2. **File-Backed Private Memory (`r-xp` Text Segment)**: Inspect the executable code VMA, verifying `vma->vm_file` points to the on-disk binary and `VM_SHARED` is unset.
3. **File-Backed Shared Libraries (`libc.so`)**: Inspect mapped dynamic library VMAs and how multiple processes share the same read-only physical code pages.
4. **Anonymous Memory: The Process Heap (`[heap]`)**: Inspect the dynamic heap VMA grown via `brk()`, showing `vma->vm_file == NULL` and `vma->anon_vma`.
5. **Anonymous Memory: The Execution Stack (`[stack]`)**: Inspect the thread stack VMA with the `VM_GROWSDOWN` flag and user stack pointer.
6. **Shared Memory Type 1 & 2: POSIX Shared Memory and tmpfs**: Inspect a shared memory VMA (`rw-s`) backed by `/dev/shm` or tmpfs with `VM_SHARED`.
7. **Shared Memory Type 3: Anonymous Shared (`memfd_create`)**: Inspect a descriptor-backed anonymous shared mapping sharing pages across unrelated processes.
8. **Huge Page Memory (Transparent Huge Pages - THP)**: Inspect 2MB-aligned memory regions and Transparent Huge Page indicators in `/proc/<pid>/smaps`.
9. **Memory Accounting & Working Sets**: Inspect `mm->rss_stat` (`MM_FILEPAGES`, `MM_ANONPAGES`, `MM_SHMEMPAGES`) and correlate with `/proc/<pid>/smaps` (RSS vs PSS).

### Plan 2: Page tables and address translation (`PAGE_TABLE_TRANSLATION`)
Aligned with: *Linux Memory Management: Page Tables & Address Translation* (`Y2oSY_eenQ4`)

1. **MMU Translation Root (CR3 / TTBR0 Register)**: Inspect `mm->pgd` and the hardware MMU base register loaded upon context switch.
2. **4-Level Virtual Address Decomposition**: Show how 48-bit virtual addresses split into 9-bit indices for PGD, PUD, PMD, and PTE plus a 12-bit page offset.
3. **Page Global Directory (PGD Level)**: Walk `pgd_t` entries indexed by virtual address bits 47:39.
4. **Intermediate Directories (PUD & PMD Levels)**: Traverse down through `pud_t` (bits 38:30) and `pmd_t` (bits 29:21).
5. **Huge Page PMD Folding**: Demonstrate how 2MB huge pages fold translation at the PMD level, skipping the leaf PTE level entirely.
6. **Leaf Page Table Entry (PTE Level)**: Reach the 4KB leaf `pte_t` (bits 20:12) and inspect its raw 64-bit value.
7. **Hardware PTE Status Flag Bits**: Decode the hardware protection bits: Present (`P`), Read/Write (`RW`), User (`US`), Accessed (`A`), Dirty (`D`), and No-Execute (`NX`).
8. **Physical Address Resolution (PFN to RAM)**: Extract Page Frame Number (PFN) from bits 12:51, compute physical memory address, and verify via `/proc/<pid>/pagemap`.
9. **Demand Paging & Page Fault Exception**: Walk through `handle_mm_fault()`, showing how unmapped addresses trigger allocation on first read or write.

### Plan 3: Scheduler and process wait chains (`EEVDF_SCHEDULER`)
Aligned with: *Linux Scheduler and Process Wait Chains* (`SdpaIMBOdv4`)

1. **Per-CPU Runqueues (`struct rq`)**: Inspect live CPU runqueue structures and currently running task pointers (`rq->curr`).
2. **Process Execution States (`__state`)**: Inspect `TASK_RUNNING`, interruptible sleep (`TASK_INTERRUPTIBLE`), and uninterruptible sleep (`TASK_UNINTERRUPTIBLE`).
3. **Voluntary vs Involuntary Context Switches**: Inspect `task->nvcsw` (sleep, I/O wait) versus `task->nivcsw` (time slice expiration, preemption).
4. **Wait Queues (`wait_queue_head_t`)**: Inspect kernel wait queues where sleeping tasks enqueue awaiting timers, events, or disk I/O.
5. **Mutex Contention & Wait Chains**: Follow `task->blocked_on` to trace lock contention chains and identify the lock holder.
6. **The CFS/EEVDF Runnable Tree (`cfs_rq`)**: Inspect the red-black tree of tasks currently competing for CPU execution.
7. **Virtual Runtime & Deadlines**: Inspect `se->vruntime`, `avg_vruntime`, scheduling lag, and earliest eligible deadlines.
8. **Context Switch Mechanics (`context_switch`)**: Follow `__schedule()` into architecture-specific register and stack swapping in `switch_to`.
9. **Live Scheduler Telemetry (bpftrace)**: Run live tracing of `sched_switch` and `sched_wakeup` latency to measure runqueue waiting delays.

### Plan 4: How Linux runs a program (`PROCESS_ARCHITECTURE`)
Aligned with: *How Linux Runs a Program* (`9jNWc8RUFvs`)

1. **Process Creation to Execution (`clone` to `execve`)**: The transition from fork/clone duplication to image replacement via `do_execveat_common()`.
2. **Binary Format Inspection & ELF Magic**: How the kernel matches magic bytes (`7f 45 4c 46`) to register the `elf_format` binary handler.
3. **Address Space Reset (`setup_new_exec`)**: Clearing old address space mappings and allocating a clean `mm_struct`.
4. **Mapping ELF Segments**: How ELF program headers (`PT_LOAD`) are translated into initial `vm_area_struct` regions for code, data, and BSS.
5. **The Dynamic Linker / Interpreter (`PT_INTERP`)**: Inspecting the loaded ELF interpreter (`/lib64/ld-linux-x86-64.so.2`) mapped into user space.
6. **Initial Stack Setup (`argc`, `argv`, `envp`)**: Inspecting user stack boundaries (`mm->arg_start..arg_end`, `env_start..env_end`).
7. **The Auxiliary Vector (`auxv`)**: Inspecting `mm->saved_auxv` keys (AT_PHDR, AT_ENTRY, AT_BASE, AT_PAGESZ) passed from kernel to dynamic linker.
8. **Dynamic Library Dependencies (`libc.so`)**: Inspecting memory-mapped shared library VMAs linked at runtime.
9. **User Space Entry (`start_thread`)**: Register state configuration setting instruction pointer (`IP`) to the program or interpreter entry point.

### Plan 5: Linux executable format (`PROCESS_LIFECYCLE`)
Aligned with: *Inside a Linux Executable File* (`7Bvx5Gd99F0`)

1. **ELF Header (`Elf64_Ehdr`)**: Magic bytes, machine architecture, entry point virtual address, and header offsets.
2. **Program Headers (`Elf64_Phdr`)**: Segments that describe runtime memory layout (`LOAD`, `DYNAMIC`, `INTERP`, `NOTE`, `GNU_STACK`).
3. **Segment Permissions & Alignment**: How `p_flags` (`PF_R`, `PF_W`, `PF_X`) dictate VMA flags and 4KB page alignment.
4. **Section Headers (`Elf64_Shdr`)**: Linker sections inside the file (`.text`, `.rodata`, `.data`, `.bss`, `.plt`, `.got`).
5. **Sections to Segments Mapping**: How multiple sections map into single contiguous memory segments.
6. **The Global Offset Table (GOT) & Procedure Linkage Table (PLT)**: Relocation tables enabling position-independent execution (PIE).
7. **Symbol Tables (`.symtab` & `.dynsym`)**: Function and variable symbol resolution tables.
8. **String Tables (`.strtab` & `.dynstr`)**: Stored null-terminated strings referenced by symbol names.
9. **Kernel Loading Interface (`load_elf_binary`)**: Step-by-step kernel code that reads and parses these exact headers into memory.

### Plan 6: Memory reclaim and VMScan (`VMSCAN_RECLAIM`)
Aligned with: *Linux Memory Management - VMScan* (`tpRlczF0pqw`)

1. **System Memory Balance (`/proc/meminfo`)**: Inspect total, free, available, cached, and anonymous memory distribution.
2. **NUMA Node Root (`struct pglist_data`)**: Navigate into the primary NUMA memory node (`contig_page_data` or `node_data[0]`).
3. **The Asynchronous Reclaim Daemon (`pgdat->kswapd`)**: Follow the kernel thread pointer to `struct task_struct *kswapd`, inspecting its state and execution statistics.
4. **Memory Zones & Watermarks (`struct zone`)**: Inspect `zone->_watermark[WMARK_MIN]`, `WMARK_LOW`, and `WMARK_HIGH` to observe hysteresis thresholds where reclaim triggers.
5. **Buddy Allocator Free Lists (`zone->free_area`)**: Examine free page counts across allocation orders (order 0 through order 10).
6. **Active vs Inactive LRU Queues (`struct lruvec`)**: Inspect how evictable pages sit in inactive and active lists (`LRU_INACTIVE_FILE`, `LRU_ACTIVE_FILE`, `LRU_INACTIVE_ANON`).
7. **Demand Paging to Watermark Depletion (19:35 Chapter)**: Connect virtual page allocations to physical free page reduction, showing how allocation pressure drops free pages toward `watermark_low`.
8. **Direct Reclaim vs Background Reclaim**: Contrast non-blocking asynchronous scanning (`kswapd`) with blocking synchronous reclamation (direct reclaim when reaching `watermark_min`).
9. **Live Scanning Telemetry (`sar -B` / tracepoints)**: Inspect real-time `pgscank/s`, `pgscand/s`, and `pgsteal/s` counters, accompanied by safe bounded memory pressure and tracepoint telemetry (`vmscan:mm_vmscan_kswapd_wake`).

---

## 10. Dedicated tutorial proposal: Memory Reclaim & VMScan (Deep Linux companion)

### 10.1 Video overview and chapter breakdown
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
At 19:35, the presenter focuses on the crucial transition point between virtual allocation and physical page consumption:
- Virtual allocations remain mere metadata entries in user space until touched.
- Accessing an unmapped page triggers a page fault exception (`handle_mm_fault()`), which allocates a physical frame from the Buddy allocator.
- As page faults continuously consume free pages, the free count in `struct zone` drops below the `watermark_low` threshold.
- Reaching this watermark triggers the asynchronous wake-up of `kswapd`.
- This theoretical setup directly motivates the practical demonstration at 24:03, where running `tail /dev/zero` rapidly exhausts memory, forcing `kswapd` into action, followed by synchronous direct reclaim once memory drops to `watermark_min`.

---

### 10.2 Architectural rationale: why VMScan belongs in kexplore

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

---

### 10.3 Complete 9-step tutorial itinerary

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

---

### 10.4 Workload safety and controlled execution
In the video at 24:03, the presenter uses `tail /dev/zero` to force memory exhaustion. In a virtual machine environment (such as Lima VM):
- An unconstrained `tail /dev/zero` rapidly exhausts all physical memory and swap, causing the Out-of-Memory (OOM) killer to terminate unpredictable processes, potentially killing SSH daemons or container runtimes.
- **kexplore design decision**:
  Rather than running unconstrained `tail /dev/zero`, kexplore should use a **bounded memory stressor** (for example, a helper with strict resource limits via `prlimit` or cgroups that temporarily consumes 60-70% of available memory). This safely drops available memory below `watermark_low`, demonstrating `kswapd` wake-up and `sar -B` page scanning without risking virtual machine crashes.

---

## 11. Industry reference presentations: Container memory & Memory at scale

Beyond the Deep Linux foundational tutorials, two industry keynote presentations provide critical insights into how Linux memory management functions in containerized and hyperscale production environments.

### 11.1 Presentation 1: Linux Memory Management and Containers (Gerlof Langeveld)
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

---

### 11.2 Presentation 2: Linux Memory Management at Scale (Chris Down)
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

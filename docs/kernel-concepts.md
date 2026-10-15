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

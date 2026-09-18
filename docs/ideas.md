# Ideas not yet built

## Userspace equivalent for every link and entry

The tool teaches where something lives in the kernel. On a production box the
tool is not installed, but the mental model still is. So each curated link and
entry should be able to show *what you would type instead*, toggled by a key.

The link already states its kernel origin; this adds the userspace column beside
it:

```
→ open files    walks task->files->fdt->fd[]        ls -l /proc/<pid>/fd
→ VMAs          walks task->mm->mm_mt (maple tree)  cat /proc/<pid>/maps
→ threads       walks task->signal->thread_head     ls /proc/<pid>/task
→ namespaces    task->nsproxy members               ls -l /proc/<pid>/ns
```

Candidates, roughly in order of how often they would be used:

| view | userspace equivalent |
|---|---|
| open files | `ls -l /proc/<pid>/fd`, `lsof -p <pid>` |
| VMAs | `cat /proc/<pid>/maps`, `smaps_rollup` for totals |
| threads | `ls /proc/<pid>/task`, `ps -L -p <pid>` |
| namespaces | `ls -l /proc/<pid>/ns` |
| cred | `grep -E 'Uid|Gid' /proc/<pid>/status` |
| mm | `/proc/<pid>/statm`, `VmRSS` in `status` |
| children | `pgrep -P <pid>` |
| sockets | `ss -tanp`, `ls -l /proc/<pid>/fd \| grep socket` |
| sched_entity | `cat /proc/<pid>/sched` (needs `sched_schedstats=1`) |
| runqueue / current CPU | `ps -o pid,psr,comm -p <pid>`, field 39 of `/proc/<pid>/stat` |
| slab caches | `slabtop`, `/proc/slabinfo` |
| mounts | `findmnt`, `/proc/self/mountinfo` |
| net devices | `ip -d link` |
| TCP sockets | `ss -tan`, `/proc/net/tcp` |
| interrupts | `/proc/interrupts`, `/proc/softirqs` |
| pages of a mapping | `/proc/<pid>/pagemap` |
| scheduler totals | `/proc/schedstat`, `/proc/stat` |

How it fits:

- It is enrichment, so it belongs next to `origin` in `catalog/links.py`, as a
  `userspace:` field on `Link` and on `Entry`.
- Some have no equivalent at all (the EEVDF tree, `cfs_rq` internals). Saying
  "no userspace equivalent" is itself worth showing: it marks the things you can
  only see with a debugger.
- Where the command needs a pid, substitute the pid of the object being viewed
  so it is copy-pasteable rather than a template.
- A few are approximations rather than the same data (`ps -o psr` reports the
  CPU, not the runqueue). Those should say so, the way measurements state their
  blind spots.


## Struct layout view: offsets, padding and cache lines

Fields are listed in declaration order, which is also layout order, but nothing
shows *where* they sit. That hides the part of a struct's design that is
deliberate.

`struct task_struct` on this kernel is 9472 bytes, 148 cache lines:

```
field       offset   size  line  note
__state         40      4     0
stack           48      8     0
flags           60      4     0
on_cpu          68      4     1
prio           124      4     1
se             192    320     3   52B padding before  <- ____cacheline_aligned
mm            2296      8    35
comm          2952     16    46
```

The 52 bytes before `se` are not waste: they force `sched_entity` onto a 64-byte
boundary so it does not share a line with the fields before it. There are 179
bytes of such padding between named members.

What to show, as a mode on the struct view rather than a separate tab:

- byte offset and size per field
- which 64-byte line each field starts in, and a rule where a line boundary
  falls between two rows
- holes, marked as padding rather than left invisible
- fields that are explicitly cacheline-aligned, since that is a design decision
  worth pointing at

Everything needed is already available: `member.bit_offset` and `sizeof` come
from drgn, and `pahole -C <tag>` (already used for declaration lookup) prints
offsets, holes and cacheline boundaries directly if a cross-check is wanted.

Why it matters here rather than being a micro-optimisation curiosity: it
explains why touching a task's scheduling state costs one or two cache lines
while touching its identity costs a different one, which is the concrete
version of "false sharing" and "hot fields" that is otherwise abstract.


## Three missing views

Every view in the tool shows a structure the same way: its fields, and the
curated links out of it. Three kinds of structure need something different.

**hierarchy** would show several levels at once, each indented under the one
above it.

Following a pointer replaces the screen with the next struct, so only one level
is ever visible. Resolving a virtual address walks five tables (`pgd`, `p4d`,
`pud`, `pmd`, `pte`), but `follow_page()` does all five inside one call and
returns only the page at the end, so `page > resident pages of pid 1` shows the
destination and none of the route. A hierarchy view would give a row per table:
the index taken there, the entry, and where the walk stopped, because a huge
page ends it early at the pmd. The mount and cgroup trees are the same problem,
where `vfs > mounts` is a flat list and the tree survives only inside the path
strings.

**state machine** would show the values a field can take, and what moves
between them.

The tool decodes `skc_state = 10` into `TCP_LISTEN`. That is where one socket
is now, and says nothing about where it can go next or what would take it
there. The view would draw the states and the transitions, label each
transition with the event that causes it, and mark how many of this kernel's
sockets are sitting in each state. The folio lifecycle and block request states
work the same way.

**invariant** would show a counter the kernel keeps beside the same number
worked out by walking the structures and counting.

The kernel keeps running totals, such as how many free pages a zone has. The
same number can be worked out the slow way, by walking the structures and
counting. Showing both is worth a view because the comparison tells you what
the counter actually includes. Measured on an idle kernel here:

```
zone Normal
  zone->vm_stat[NR_FREE_PAGES]      676864 pages   the counter
  sum(free_area[order].nr_free)     676864 pages   counted from the free lists
  per-cpu cached pages                6408 pages   in neither number
```

The first two agreeing exactly is the finding: `NR_FREE_PAGES` tracks the buddy
free lists and nothing else. The 6408 pages parked in per-CPU caches appear in
neither total, so a page can be available to the next allocation without being
counted as free by the zone. Under load the first two drift apart by a handful
of pages, because the kernel allocates between the two reads and they cannot be
sampled together; a view would have to say that rather than report it as a
fault. `mm->map_count` against a walk of the VMAs, and `sk_rmem_alloc` against
the summed `truesize` of a socket's queued skbs, are the same exercise.


## A /proc file read line by line, pivoted back to the structures

Userspace mode goes one way: a struct or a link, and the command you would type
instead. The pivot is the other way, and starts from the file a person already
reads. Show the output of a /proc file, put the cursor on one line, and the
pane below says which kernel state produced that number.

Read from the lab VM, sampling the file and the counters in one pass: the two
zones held 569332 and 814208 free pages, which at 4 kB a page is 5534160 kB,
exactly the MemFree the file printed. Sampled seconds apart instead, the two
differed by 109 MB, so a view like this has to read both at once and say that
it did.

```
/proc/meminfo
  MemTotal:        8096472 kB
  MemFree:         5534160 kB     <- cursor
  Percpu:              408 kB
  HardwareCorrupted:     0 kB

  si_meminfo() fills struct sysinfo.freeram from
  global_zone_page_state(NR_FREE_PAGES), summed over zones
  zone DMA      zone->vm_stat[NR_FREE_PAGES]   569332 pages
  zone Normal   zone->vm_stat[NR_FREE_PAGES]   814208 pages
                sum 1383540 pages = 5534160 kB, the line above
```

The mapping exists in one place and can be read: `fs/proc/meminfo.c` is a
sequence of `show_val_kb(m, "MemFree:        ", i.freeram)` calls, line 61 on
the kernel in the lab VM. What that file gives is the expression behind each
line, not the storage, so resolving a line falls into three cases worth
separating:

- the line is a counter read, such as a `vm_stat` entry. It resolves to a
  field, and the existing entry machinery can show it.
- the line is computed by a helper, such as `si_meminfo()`. It resolves to the
  several places the helper reads, which is more instructive than the number.
- the line is a sum over structures. Only a walk reproduces it, which is what
  the catalog entries already do, so the view would link to the entry rather
  than restate it.

This is the same data the catalog already holds, indexed by the userspace line
instead of by the struct. It pairs with the trace view: that one shows the path
a command takes into the kernel at runtime, this one shows where a number came
from without running anything.

Open questions:

- Whether the mapping is hand-written per file, like the link table, or derived
  per kernel from the source of each `show` function. Hand-written is honest
  and small for `/proc/meminfo`; it does not scale to `/proc/<pid>/status`.
- Which files earn it first. `/proc/meminfo`, `/proc/<pid>/status`,
  `/proc/stat` and `/proc/zoneinfo` are the ones people read under pressure.
- What a line shows when nothing resolves it. It has to say that the mapping is
  missing, not leave the pane empty, or an unmapped line looks like a line with
  no kernel state behind it.


## Subsystems with no entry point

Types this kernel has that nothing in the catalog reaches yet: cgroup, page
cache (`address_space`), reclaim (`lruvec`) and the buddy allocator. The irq,
timer, workqueue and RCU structures listed here before are now the `irq`,
`time` and `sync` branches; `bio`, `request` and `request_queue` are the
`block` branch; the System V queues, semaphores and shared memory are the
`ipc` branch. An `address_space` is reachable from a `block_device` and from a
shared memory segment's file, but has no links of its own yet.

POSIX IPC is not covered: the mq_open(3) queues have a count in
`init_ipc_ns.mq_queues_count` but reaching the queues themselves means walking
the internal mqueue mount, and POSIX shared memory is ordinary tmpfs files
under /dev/shm.

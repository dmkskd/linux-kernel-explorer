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

Notes on shape:

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

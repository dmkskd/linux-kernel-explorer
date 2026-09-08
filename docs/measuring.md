# Timing something inside the kernel

`operations/clone_experiment.py` reports how long `clone()` takes per flag
combination. Getting that column to mean anything took several passes, and each
one was a case of the harness measuring itself. What follows is what went
wrong, how each cause was identified, and what the loop looks like now.

The numbers below come from one 4-vCPU lima VM, 200 rounds per variant. They
are here as evidence for the causes, not as facts about `clone()`: another
machine will produce different ones. The ratios and the direction of each
change are the part that transfers.

## Read the distribution, not the summary

The original column reported min, median and p90, and the p90 was routinely
double the min. Three different causes were hiding under that one spread, and
none of them was visible in the summary. Each was found by dumping every sample
with its round number and looking at the sequence.

Two things are worth doing before believing a spread:

- **Plot against round number.** A number that climbs with the round is the
  harness accumulating state, never the kernel.
- **Histogram it.** A wide p90 can be a tail or a second mode, and those have
  completely different causes.

## Cause 1: the harness grew during the run

The loop allocated a 256 KB stack per round and leaked it, because a child may
still be running on its stack when `clone()` returns. So the address space
gained a mapping every round, and the variants that copy it got slower as the
run went on:

```
plain fork, median of the first 20 rounds   32 us
plain fork, median of the last 20 rounds    69 us
```

Sorting that sequence turns a trend into a spread: the min is an early round
and the p90 a late one. The `CLONE_VM` variants, which never copy the address
space, showed no such trend, which is what pointed at the mm.

## Cause 2: the fix was worse than the problem

Allocating all 200 stacks up front removes the trend, but it makes every round
pay the full price rather than the average: plain fork went from 39.5 us median
to 51.5. Constant is not the same as correct.

One arena carved into slots was better, at 26.1, because 50 MB in one mapping
is one `vm_area_struct` instead of 200. But the arena still has to be faulted
in, and that turned out to matter more than how many mappings it spans: touching
one page per 2 MB cost exactly as much as touching all 200 slots (21.0 us
median each, against 14.1 untouched). The cost is not the pages, it is that
`dup_mmap` copies the page tables once they exist at all.

What actually works is not allocating the memory. Only `CLONE_THREAD` needs a
fresh stack per round; every other variant either waits for the child, or
(vfork) resumes only once the child is finished with the stack. Those reuse a
single stack, so the address space stays as small as it started.

## Cause 3: the measurement included a wakeup to another CPU

With the address space constant, pinning the timing process to one CPU halved
the whole distribution:

```
                unpinned          pinned
plain fork      25.1 us median    12.6
CLONE_THREAD     7.3 us median     3.3
```

Raising the priority to `SCHED_FIFO` 50 changed nothing, so this is not
preemption. The child inherits the affinity mask: unpinned, `wake_up_new_task`
places it on another CPU and `clone()` pays for an IPI and for bringing an idle
vCPU back, which inside a VM means going out to the host. Pinned, the child is
queued on the same runqueue and none of that happens.

Pinning changes what is measured, which is the point: the column claims to show
the `clone()` path, not the idle-exit latency of the machine underneath.

## Cause 4: a second mode, once every 8 rounds

After all that, one column was still five times wider at p90 than at p10. The
histogram showed it was not a tail: 342 of 400 rounds under 10 us, a clean gap,
then a second mode at 20-30 us holding about 12% of rounds.

The round numbers of the slow rounds were 6, 14, 22, 30, and so on: exactly
every eighth, with 47 of 49 gaps equal to 8. Count-periodic and not
time-periodic rules out the timer tick. With a 256 KB stack, every eighth round
starts 2 MB further into the arena, so the prediction was that a 128 KB stack
would move it to every sixteenth round. It did. That is a page table page being
allocated on the first touch of each new region.

It appeared in the vfork column alone because vfork is the only variant whose
parent stays blocked while the child runs on the new stack, so the child's
fault falls inside the timed window. Everywhere else the child faults after
`clone()` has already returned. Faulting the stacks in before timing took
vfork's p90 from 24.0 us to 6.5 and left its median at 5.1.

## Cause 5: the summary itself

With the run pinned and the address space constant, the samples cluster
tightly, and the single fastest one sits well below the cluster: only 7 of 400
rounds landed within 10% of the minimum. Quoting the min made the spread look
twice as wide as it is. The column reports p10, median and p90.

## What the loop does now

`tests/helpers/clone_matrix.c`, per variant, in a freshly forked runner so no
variant inherits another's address space:

1. Pin to one CPU, taking the first in the existing affinity mask so `taskset`
   and cpusets still work.
2. Allocate one stack, or one per round for `CLONE_THREAD`, and touch each one.
3. Time 200 `clone()` calls, and nothing else, with `CLOCK_MONOTONIC`.
4. Report p10, median and p90.

Across two consecutive runs the medians reproduce to within a few percent, and
p10 to p90 spans 1.4x to 1.8x for every variant.

## What is left, and why it stays

Two columns are still wide, and both are the kernel rather than the harness:

- **vfork** suspends the parent until the child exits or execs, so its number
  includes a scheduler round trip.
- **CLONE_NEWNET** waits on RCU and the network namespace cleanup workqueue.
  It is also the slowest by an order of magnitude, and it is the reason the
  whole experiment takes a while to run.

A tail that survives pinning, a constant address space and pre-faulting is
worth reporting rather than tuning away.

## The general rule

Every cause above was found the same way: predict what the mechanism implies,
then change one thing and check the prediction. The 2 MB boundary was named by
halving the stack size and watching the period double. The cross-CPU wakeup was
separated from preemption by trying `SCHED_FIFO` and getting nothing. A
mechanism that only ever explains the number you already have is a guess.

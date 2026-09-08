/*
 * Create one child per clone flag combination, so each flag's effect can be
 * isolated.
 *
 * Observing an arbitrary process cannot show what a flag does: a pthread_create
 * passes CLONE_VM|CLONE_FILES|CLONE_FS|CLONE_SIGHAND|CLONE_THREAD together, so
 * every structure differs at once and nothing is attributable. Here each child
 * varies one thing.
 *
 *   gcc -O2 -o /tmp/clone_matrix tests/helpers/clone_matrix.c
 *   sudo /tmp/clone_matrix 300 > /tmp/clone_matrix.log
 *
 * It first times each variant, then leaves one child of each alive so the
 * resulting structures can be inspected. Children pause() forever and are
 * killed when the parent exits.
 */
#define _GNU_SOURCE
#include <sched.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>
#include <sys/wait.h>
#include <time.h>
#include <sys/syscall.h>

#define STACK_BYTES (256 * 1024)
#define MAX_CHILDREN 16

struct variant {
	const char *name;
	int flags;
	/*
	 * vfork suspends the parent until the child execs or exits, so a child
	 * held alive would block this helper forever. Such variants are timed
	 * but not held for inspection.
	 */
	int hold;
};

/*
 * CLONE_SIGHAND requires CLONE_VM, and CLONE_THREAD requires CLONE_SIGHAND, so
 * the later rows are cumulative by necessity rather than by choice.
 */
static const struct variant variants[] = {
	{ "SIGCHLD (plain fork)",      SIGCHLD, 1 },
	{ "vfork (VM|VFORK)",          CLONE_VM | CLONE_VFORK | SIGCHLD, 0 },
	{ "CLONE_VM",                  CLONE_VM | SIGCHLD, 1 },
	{ "CLONE_FILES",               CLONE_FILES | SIGCHLD, 1 },
	{ "CLONE_FS",                  CLONE_FS | SIGCHLD, 1 },
	{ "CLONE_VM|SIGHAND",          CLONE_VM | CLONE_SIGHAND | SIGCHLD, 1 },
	{ "CLONE_VM|SIGHAND|THREAD",   CLONE_VM | CLONE_SIGHAND | CLONE_THREAD, 1 },
	{ "CLONE_NEWNS",               CLONE_NEWNS | SIGCHLD, 1 },
	{ "CLONE_NEWNET",              CLONE_NEWNET | SIGCHLD, 1 },
};

static int child_fn(void *arg)
{
	(void)arg;
	for (;;)
		pause();
	return 0;
}

static int child_exit_fn(void *arg)
{
	(void)arg;
	/*
	 * _exit() is exit_group(): for the CLONE_THREAD variant that would take
	 * the whole helper down with it. Exit just this task.
	 */
	syscall(SYS_exit, 0);
	return 0;
}

static int compare_long(const void *a, const void *b)
{
	long x = *(const long *)a, y = *(const long *)b;
	return (x > y) - (x < y);
}

/*
 * Pin to one CPU for the duration of the timing. Unpinned, wake_up_new_task
 * puts the new task on a different CPU, and clone() then pays for an IPI and
 * for bringing an idle vCPU back, which inside a VM means going out to the
 * host. Measured over 400 rounds on this VM, pinning halved the whole
 * distribution: plain fork went from 25.1 us median to 12.6, and a
 * CLONE_THREAD clone from 7.3 to 3.3. What is left is the clone() path
 * itself, which is what the column claims to show.
 */
static void pin_to_one_cpu(void)
{
	cpu_set_t allowed, one;

	CPU_ZERO(&allowed);
	if (sched_getaffinity(0, sizeof(allowed), &allowed))
		return;
	for (int cpu = 0; cpu < CPU_SETSIZE; cpu++) {
		if (!CPU_ISSET(cpu, &allowed))
			continue;
		CPU_ZERO(&one);
		CPU_SET(cpu, &one);
		sched_setaffinity(0, sizeof(one), &one);
		return;
	}
}

/*
 * Time the clone() call itself, and report the distribution rather than a mean.
 * Inside a VM the host can deschedule the vCPU mid-call, which produces a long
 * tail that says nothing about the kernel, so a quantile is reported rather
 * than a mean. The low end is p10 and not the minimum: with the run pinned and
 * the address space held constant, the samples cluster tightly and the single
 * fastest one is a rare outlier below that cluster (7 of 400 rounds landed
 * within 10% of it), so quoting it makes the spread look twice as wide as it
 * is. Raising the RT priority was tried and changed nothing, so preemption is
 * not what remains.
 */
static void time_variant(const struct variant *v, int rounds, long *out)
{
	static long samples[4096];
	struct timespec a, b;
	int counted = 0;
	int slots;
	char *arena;

	if (rounds > (int)(sizeof(samples) / sizeof(samples[0])))
		rounds = sizeof(samples) / sizeof(samples[0]);

	pin_to_one_cpu();

	/*
	 * Only CLONE_THREAD needs a stack per round: its child may still be
	 * running on the stack when clone() returns. Everywhere else the child
	 * is finished with it by then, because the loop waits for it, and vfork
	 * suspends the parent until the child exits or execs. So those variants
	 * reuse a single stack.
	 *
	 * This is what keeps the address space out of the measurement. Freeing
	 * and reallocating a stack per round grew the mm by a mapping each time
	 * and the variants that copy it got steadily slower: on plain fork the
	 * median of the first 20 rounds was 32 us against 69 for the last 20.
	 * Allocating all the stacks up front instead is constant but expensive,
	 * because touching that arena anywhere makes dup_mmap copy its page
	 * tables on every round: one page touched per 2 MB cost as much as
	 * touching all 200 slots (both 21.0 us median against 14.1 untouched).
	 * One reused stack is both constant and cheap, at 11.7.
	 */
	slots = (v->flags & CLONE_THREAD) ? rounds : 1;
	while (slots > 0) {
		arena = malloc((size_t)slots * STACK_BYTES);
		if (arena)
			break;
		slots /= 2;
	}
	if (slots <= 0) {
		out[0] = out[1] = out[2] = out[3] = -1;
		return;
	}
	if (slots < rounds && slots > 1)
		rounds = slots;

	/*
	 * Fault the stacks in before timing rather than during it. The first
	 * write to a slot faults, and every 2 MB that fault also allocates a
	 * page table page. That landed in the vfork column alone, once every 8
	 * rounds with a 256 KB stack: vfork is the only variant whose parent is
	 * still blocked while the child runs on the new stack. Halving the
	 * stack size moved it to every 16th round, which is what identified the
	 * 2 MB boundary. Pre-touched, vfork's p90 fell from 24.0 us to 6.5 with
	 * the median unchanged at 5.1.
	 */
	for (int i = 0; i < slots; i++)
		arena[(size_t)(i + 1) * STACK_BYTES - 8] = 0;

	/* Stacks grow down, so each child starts at the top of its slot. */
	for (int i = 0; i < rounds; i++) {
		clock_gettime(CLOCK_MONOTONIC, &a);
		pid_t pid = clone(child_exit_fn,
				  arena + (size_t)(i % slots + 1) * STACK_BYTES,
				  v->flags, NULL);
		clock_gettime(CLOCK_MONOTONIC, &b);
		if (pid < 0)
			break;
		samples[counted++] = (b.tv_sec - a.tv_sec) * 1000000000L +
				     (b.tv_nsec - a.tv_nsec);
		if (!(v->flags & CLONE_THREAD))
			waitpid(pid, NULL, __WALL);
	}

	if (!counted) {
		out[0] = out[1] = out[2] = out[3] = -1;
		return;
	}
	qsort(samples, counted, sizeof(samples[0]), compare_long);
	out[0] = samples[counted / 10];         /* p10 */
	out[1] = samples[counted / 2];          /* median */
	out[2] = samples[(counted * 9) / 10];   /* p90 */
	out[3] = counted;
}

static pid_t children[MAX_CHILDREN];
static int nchildren;

static void cleanup(int signo)
{
	(void)signo;
	for (int i = 0; i < nchildren; i++)
		if (children[i] > 0)
			kill(children[i], SIGKILL);
	_exit(0);
}

int main(int argc, char **argv)
{
	int seconds = argc > 1 ? atoi(argv[1]) : 300;
	int ballast_mb = argc > 2 ? atoi(argv[2]) : 0;

	signal(SIGTERM, cleanup);
	signal(SIGINT, cleanup);

	printf("parent %d\n", getpid());

	/*
	 * Time each variant in a fresh child. Timing them in one process made
	 * the numbers rise with run order: every round leaks a 256 KB stack, so
	 * later variants forked a much larger parent and paid for copying its
	 * page tables. A fresh process per variant restores a common baseline.
	 */
	for (size_t i = 0; i < sizeof(variants) / sizeof(variants[0]); i++) {
		long stats[4] = { -1, -1, -1, -1 };
		int fds[2];

		if (pipe(fds) == 0) {
			pid_t runner = fork();
			if (runner == 0) {
				close(fds[0]);
				time_variant(&variants[i], 200, stats);
				ssize_t ignored = write(fds[1], stats, sizeof(stats));
				(void)ignored;
				_exit(0);
			}
			close(fds[1]);
			if (runner > 0) {
				ssize_t got = read(fds[0], stats, sizeof(stats));
				(void)got;
				waitpid(runner, NULL, 0);
			}
			close(fds[0]);
		}
		printf("COST %ld %ld %ld %ld %s\n", stats[0], stats[1], stats[2],
		       stats[3], variants[i].name);
	}
	fflush(stdout);

	/*
	 * Dirty some anonymous memory before creating the children that are held
	 * for inspection, so a forked child has pages worth counting. This is
	 * done after timing so the cost numbers stay free of it.
	 */
	if (ballast_mb > 0) {
		size_t bytes = (size_t)ballast_mb * 1024 * 1024;
		char *ballast = malloc(bytes);
		if (ballast) {
			for (size_t off = 0; off < bytes; off += 4096)
				ballast[off] = 1;
			printf("BALLAST %d MiB at %p\n", ballast_mb, (void *)ballast);
		}
	}

	for (size_t i = 0; i < sizeof(variants) / sizeof(variants[0]); i++) {
		if (!variants[i].hold)
			continue;
		char *stack = malloc(STACK_BYTES);
		if (!stack) {
			fprintf(stderr, "%s: out of memory\n", variants[i].name);
			continue;
		}
		pid_t pid = clone(child_fn, stack + STACK_BYTES,
				  variants[i].flags, NULL);
		if (pid < 0) {
			printf("FAILED %s\n", variants[i].name);
			free(stack);
			continue;
		}
		children[nchildren++] = pid;
		printf("%d %s\n", pid, variants[i].name);
	}
	/* Distinct marker: a variant name also appears in the COST lines above. */
	printf("READY %d children\n", nchildren);
	fflush(stdout);

	sleep(seconds);
	cleanup(0);
	return 0;
}

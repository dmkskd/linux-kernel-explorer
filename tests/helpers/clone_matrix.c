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
 * Time the clone() call itself, and report the distribution rather than a mean.
 * Inside a VM the host can deschedule the vCPU mid-call, which produces a long
 * tail that says nothing about the kernel; the minimum is the sample least
 * contaminated by it, so all three are reported.
 */
static void time_variant(const struct variant *v, int rounds, long *out)
{
	static long samples[4096];
	struct timespec a, b;
	int counted = 0;

	if (rounds > (int)(sizeof(samples) / sizeof(samples[0])))
		rounds = sizeof(samples) / sizeof(samples[0]);

	for (int i = 0; i < rounds; i++) {
		char *stack = malloc(STACK_BYTES);
		if (!stack)
			break;
		clock_gettime(CLOCK_MONOTONIC, &a);
		pid_t pid = clone(child_exit_fn, stack + STACK_BYTES, v->flags, NULL);
		clock_gettime(CLOCK_MONOTONIC, &b);
		if (pid < 0) {
			free(stack);
			break;
		}
		samples[counted++] = (b.tv_sec - a.tv_sec) * 1000000000L +
				     (b.tv_nsec - a.tv_nsec);
		if (!(v->flags & CLONE_THREAD))
			waitpid(pid, NULL, __WALL);
		/* The child may still be on this stack briefly; leak it. */
	}

	if (!counted) {
		out[0] = out[1] = out[2] = out[3] = -1;
		return;
	}
	qsort(samples, counted, sizeof(samples[0]), compare_long);
	long total = 0;
	for (int i = 0; i < counted; i++)
		total += samples[i];
	out[0] = samples[0];                    /* min */
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

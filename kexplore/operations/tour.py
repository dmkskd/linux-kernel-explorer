"""Live guided tours: interactive walkthroughs through live kernel structures.

While structure entries show what is in a struct and operations trace code paths,
guided tours drive an end-to-end investigation across the running kernel.
Tours dynamically inspect current running processes, threads, address spaces,
and per-CPU queues, presenting live commentary, invariants, and userspace
counterparts.

Each step provides an action path, live commentary, userspace commands, and
followable links that open the real data structures in live memory.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from drgn import Object, Program

from ..catalog.registry import CheckResult
from ..core import ctypes as ct

Resolver = Callable[[Any], "Iterator[tuple[str, Any]]"]


@dataclass(frozen=True)
class TourStep:
    """One step in a live guided tour / tutorial."""

    title: str
    action: str
    commentary: str
    userspace: str = ""
    structures: Resolver | None = None
    doc: str = ""
    highlight_field: str = ""
    action_field: str = ""
    value_fields: tuple[str, ...] = ()
    insight: str = ""
    flow_label: str = ""

    def get_action_field(self) -> str:
        return self.action_field or self.highlight_field

    def get_value_fields(self) -> list[str]:
        return list(self.value_fields)

    def get_insight(self) -> str:
        if self.insight:
            return self.insight
        first = self.commentary.split(". ")[0].strip()
        return first + ("." if not first.endswith(".") else "")

    def get_flow_label(self) -> str:
        if self.flow_label:
            return self.flow_label
        target = self.action.split("›")[-1].strip()
        target = re.sub(r"\(.*?\)", "", target).strip()
        if "=" in target:
            target = target.split("=")[0].strip()
        target = re.sub(r"^(struct\s+|mm->|task->|vma->|file->)", "", target).strip()
        if not target:
            target = self.title.split("(")[0].strip()
        return target[:14].strip()


TutorialStep = TourStep


@dataclass(frozen=True)
class GuidedTour:
    """A live guided tour / tutorial through real kernel structures with commentary."""

    key: str
    label: str
    category: str
    doc: str
    builder: Callable[[Program | None], list[TourStep]]
    video_url: str = ""
    video_title: str = ""

    def steps(self, prog: Program | None = None) -> list[TourStep]:
        """Generate tour steps, adapting to live processes if a kernel is attached."""
        try:
            return self.builder(prog)
        except Exception:  # noqa: BLE001
            # Fall back to base steps if live inspection encounters an error
            return self.builder(None)

    def check(self, prog: Program) -> CheckResult:
        """Check that live resolvers for this tour succeed on the running kernel."""
        try:
            steps = self.steps(prog)
            if not steps:
                return CheckResult(False, "no steps generated")
            for step in steps:
                if step.structures is not None:
                    next(step.structures(prog), None)
            return CheckResult(True, f"{len(steps)} step(s)")
        except Exception as exc:  # noqa: BLE001
            return CheckResult(False, str(exc))


GuidedTutorial = GuidedTour
Tutorial = GuidedTour


# ----------------------------------------------------------- Live Discovery


def _find_live_multithreaded_task(prog: Program | None):
    if prog is None:
        return None
    try:
        from drgn.helpers.linux.pid import find_task, for_each_task

        for task in for_each_task(prog):
            if task.pid == task.tgid and task.signal.nr_threads.value_() > 1:
                return task
        return find_task(prog, 1)
    except Exception:  # noqa: BLE001
        return None


def _find_live_target_task(prog: Program | None):
    if prog is None:
        return None
    try:
        from drgn.helpers.linux.pid import find_task

        return find_task(prog, 1)
    except Exception:  # noqa: BLE001
        return None


def _resolve_process_catalog(p: Program):
    from drgn.helpers.linux.pid import find_task

    t1 = find_task(p, 1)
    if t1:
        yield "init (pid 1)", t1
    multithreaded = _find_live_multithreaded_task(p)
    if multithreaded:
        yield "processes", multithreaded
    yield "all tasks", t1 or multithreaded


def _resolve_mm_catalog(p: Program):
    mm = p["init_mm"].address_of_()
    yield "init_mm", mm
    yield "vma_types", mm
    yield "maple_tree", mm


def _resolve_sched_catalog(p: Program):
    from drgn.helpers.linux.cpumask import for_each_online_cpu
    from drgn.helpers.linux.percpu import per_cpu

    rq = p["runqueues"].address_of_()
    yield "runqueues", rq
    for cpu in for_each_online_cpu(p):
        rq_cpu = per_cpu(p["runqueues"], cpu).address_of_()
        yield f"fair_sched (cpu{cpu})", rq_cpu.cfs.address_of_()
        break


def _resolve_home(p: Program):
    if p is not None:
        try:
            yield "kexplore", p["init_uts_ns"].address_of_()
            return
        except Exception:  # noqa: BLE001
            pass
    yield "kexplore", None


# ---------------------------------------------------- Process Tour Builders


def _build_process_architecture_steps(prog: Program | None) -> list[TourStep]:
    task = _find_live_multithreaded_task(prog)
    comm = task.comm.string_().decode() if task else "target"
    pid = task.pid.value_() if task else 1
    tgid = task.tgid.value_() if task else 1

    nr_threads = 1
    if task and task.signal:
        try:
            nr_threads = task.signal.nr_threads.value_()
        except Exception:  # noqa: BLE001
            nr_threads = 1

    mm_users = 1
    if task and task.mm:
        try:
            mm_users = task.mm.mm_users.counter.value_()
        except Exception:  # noqa: BLE001
            mm_users = 1

    def _resolve_leader(p: Program):
        t = _find_live_multithreaded_task(p)
        if t:
            c = t.comm.string_().decode()
            yield f"{t.pid.value_()} {c} [leader]", t

    def _resolve_threads(p: Program):
        from drgn.helpers.linux.pid import for_each_task_in_group

        t = _find_live_multithreaded_task(p)
        if t:
            for thread in for_each_task_in_group(t, include_self=True):
                marker = " [leader]" if thread.pid == thread.tgid else ""
                c = thread.comm.string_().decode()
                yield f"{thread.pid.value_()}{marker} {c}", thread

    def _resolve_mm(p: Program):
        t = _find_live_multithreaded_task(p)
        if t and t.mm:
            yield f"{t.comm.string_().decode()} mm_struct", t.mm
        else:
            yield "init_mm", p["init_mm"].address_of_()

    def _resolve_vmas(p: Program):
        from drgn.helpers.linux.mm import for_each_vma, vma_name

        t = _find_live_multithreaded_task(p)
        mm = t.mm if t and t.mm else p["init_mm"].address_of_()
        for vma in for_each_vma(mm):
            name = vma_name(vma)
            label = name.decode("utf-8", "replace") if name else "anon"
            yield f"{vma.vm_start.value_():#x} {label}", vma

    def _resolve_text(p: Program):
        from drgn.helpers.linux.mm import for_each_vma, vma_name

        t = _find_live_multithreaded_task(p)
        mm = t.mm if t and t.mm else p["init_mm"].address_of_()
        for vma in for_each_vma(mm):
            name = vma_name(vma)
            label = name.decode("utf-8", "replace") if name else ""
            if (vma.vm_flags.value_() & 0x4) and label:  # VM_EXEC
                yield f"{vma.vm_start.value_():#x} text ({label})", vma
                return

    def _resolve_file(p: Program):
        from drgn.helpers.linux.mm import for_each_vma

        t = _find_live_multithreaded_task(p)
        mm = t.mm if t and t.mm else p["init_mm"].address_of_()
        for vma in for_each_vma(mm):
            if (vma.vm_flags.value_() & 0x4) and vma.vm_file:
                yield f"{comm} exe file", vma.vm_file
                return

    def _resolve_sighand(p: Program):
        t = _find_live_multithreaded_task(p)
        if t and t.sighand:
            yield f"{comm} sighand_struct", t.sighand
        elif t and t.signal:
            yield f"{comm} signal_struct", t.signal

    return [
        TourStep(
            title="Starting screen: kexplore entry points",
            action="kexplore › task relationships",
            commentary=(
                "kexplore is attached to the running kernel. Every structure in this "
                "walkthrough is reached by dereferencing a pointer in the one before it."
            ),
            userspace=f"cat /proc/{pid}/status | grep -E '(Pid|Tgid|Threads)'",
            structures=_resolve_home,
            highlight_field="task relationships",
            action_field="task relationships",
            value_fields=("kernel release", "struct docs and source"),
            insight="The opening screen names the kernel and what the tool can resolve against it.",
            flow_label="kexplore",
        ),
        TourStep(
            title="Main menu: process catalog",
            action="subsystems › process catalog",
            commentary=(
                "The process catalog lists the thread group leaders present on this system. "
                "A leader is a task whose tgid equals its pid; the other threads of the group "
                "are reached from it."
            ),
            userspace=f"cat /proc/{pid}/status | grep -E '(Pid|Tgid|Threads)'",
            structures=_resolve_process_catalog,
            highlight_field="processes",
            action_field="processes",
            value_fields=("init", "all tasks"),
            insight="In process catalog, follow 'processes' into thread group leader.",
            flow_label="process catalog",
        ),
        TourStep(
            title=f"Thread group leader ({comm}, PID {pid})",
            action=f"process › {pid} {comm}",
            commentary=(
                f"Currently running process '{comm}' (PID {pid}) has {nr_threads} threads. "
                f"Its task_struct has tgid==pid ({tgid}=={pid}), designating it as the thread group leader."
            ),
            userspace=f"cat /proc/{pid}/status | grep -E '(Pid|Tgid|Threads)'",
            structures=_resolve_leader,
            highlight_field="threads",
            action_field="threads",
            value_fields=("comm", "pid", "tgid"),
            insight=f"Leader '{comm}' (PID {pid}, {nr_threads} threads): tgid equals pid.",
            flow_label="task_struct",
        ),
        TourStep(
            title=f"Thread list traversal ({nr_threads} threads)",
            action=f"threads › {pid} {comm}",
            commentary=(
                "Threads in Linux are distinct task_struct descriptors sharing a tgid. "
                "The 'threads' relationship traverses task->signal->thread_head."
            ),
            userspace=f"ls /proc/{pid}/task; ps -T -p {pid}",
            structures=_resolve_threads,
            highlight_field="mm",
            action_field="mm",
            value_fields=("leader", "pid"),
            insight="Threads are separate task_struct descriptors sharing one tgid.",
            flow_label="threads",
        ),
        TourStep(
            title=f"Shared address space (mm_users={mm_users})",
            action="mm (address space) › mm_struct",
            commentary=(
                f"All threads in this group point to the exact same mm_struct instance. "
                f"clone() was invoked with CLONE_VM; mm_users refcount currently equals {mm_users}."
            ),
            userspace=f"cat /proc/{pid}/maps (identical across all thread tasks in /proc/{pid}/task/)",
            structures=_resolve_mm,
            highlight_field="VMAs",
            action_field="VMAs",
            value_fields=("mm_users", "start_code", "end_code"),
            insight=f"One mm_struct shared by the group (CLONE_VM, mm_users={mm_users}).",
            flow_label="mm_struct",
        ),
        TourStep(
            title="Virtual memory areas (maple tree)",
            action="VMAs (vm_area_struct)",
            commentary=(
                "The maple tree (mm->mm_mt) indexes all memory mappings: executable text, "
                "shared libraries, thread stacks, and heap."
            ),
            userspace=f"pmap -x {pid}",
            structures=_resolve_vmas,
            highlight_field=comm,
            action_field=comm,
            value_fields=("vm_start", "vm_end"),
            insight="Maple tree indexes all mapped memory regions for thread group.",
            flow_label="VMAs",
        ),
        TourStep(
            title="Executable binary segment (vm_area_struct, r-xp)",
            action="VMA › vm_area_struct (r-xp)",
            commentary=(
                f"The executable text mapping for '{comm}'. Memory protection is enforced via "
                "vm_flags (VM_READ|VM_EXEC = r-xp)."
            ),
            userspace=f"grep 'r-xp' /proc/{pid}/maps",
            structures=_resolve_text,
            highlight_field="vm_file",
            action_field="vm_file",
            value_fields=("vm_flags", "vm_start", "vm_end"),
            insight="Executable code, mapped VM_READ|VM_EXEC.",
            flow_label="text (r-xp)",
        ),
        TourStep(
            title="Backing ELF binary on disk (struct file & inode)",
            action="vma->vm_file › struct file",
            commentary=(
                "The executable VMA links through vm_file to struct file and struct inode. "
                "Linux uses demand paging: code pages are loaded from disk only when touched."
            ),
            userspace=f"ls -l /proc/{pid}/exe",
            structures=_resolve_file,
            highlight_field="f_inode",
            action_field="f_inode",
            value_fields=("f_path", "f_flags"),
            insight="The struct file the mapping was created from.",
            flow_label="struct file",
        ),
        TourStep(
            title="Shared signal handlers & resource limits (struct sighand_struct)",
            action="task_struct › sighand (CLONE_SIGHAND)",
            commentary=(
                "Threads share signal handlers via CLONE_SIGHAND: modifying a signal handler "
                "with sigaction() in one thread immediately affects all threads in the group."
            ),
            userspace=f"cat /proc/{pid}/status | grep -E 'Sig(Blk|Ign|Cgt)'",
            structures=_resolve_sighand,
            highlight_field="action",
            action_field="",
            value_fields=("count", "action"),
            insight="CLONE_SIGHAND shares signal handlers and resource limits across all threads.",
            flow_label="sighand",
        ),
    ]


def _build_process_lifecycle_steps(prog: Program | None) -> list[TourStep]:
    def _resolve_init_task(p: Program):
        yield "init_task (swapper/0)", p["init_task"].address_of_()

    def _resolve_init_cred(p: Program):
        from drgn.helpers.linux.pid import find_task

        t = find_task(p, 1)
        yield "init cred", t.cred

    def _resolve_init_nsproxy(p: Program):
        from drgn.helpers.linux.pid import find_task

        t = find_task(p, 1)
        yield "init nsproxy", t.nsproxy

    def _resolve_init_net(p: Program):
        from drgn.helpers.linux.pid import find_task

        t = find_task(p, 1)
        yield "init net_ns (struct net)", t.nsproxy.net_ns

    def _resolve_init_files(p: Program):
        from drgn.helpers.linux.pid import find_task

        t = find_task(p, 1)
        yield "init files_struct", t.files

    def _resolve_init_fd0(p: Program):
        from drgn.helpers.linux.pid import find_task

        t = find_task(p, 1)
        fdt = t.files.fdt
        fd0 = fdt.fd[0]
        if fd0:
            yield "fd 0 (struct file)", fd0
        else:
            yield "init files", t.files

    def _resolve_init_inode0(p: Program):
        from drgn.helpers.linux.pid import find_task

        t = find_task(p, 1)
        fdt = t.files.fdt
        fd0 = fdt.fd[0]
        if fd0 and fd0.f_inode:
            yield f"fd 0 inode {fd0.f_inode.i_ino.value_()}", fd0.f_inode
        else:
            yield "init files", t.files

    return [
        TourStep(
            title="Starting screen: kexplore entry points",
            action="kexplore › task relationships",
            commentary=(
                "kexplore is attached to the running kernel. Every structure in this "
                "walkthrough is reached by dereferencing a pointer in the one before it."
            ),
            userspace="cat /proc/1/status",
            structures=_resolve_home,
            highlight_field="task relationships",
            action_field="task relationships",
            value_fields=("kernel release", "struct docs and source"),
            insight="The opening screen names the kernel and what the tool can resolve against it.",
            flow_label="kexplore",
        ),
        TourStep(
            title="Main menu: process catalog",
            action="subsystems › process catalog",
            commentary=(
                "init is the first userspace process the kernel starts and the ancestor of "
                "every other one. Its task_struct holds the credentials, namespaces and open "
                "files that fork() either copies or shares."
            ),
            userspace="cat /proc/1/status",
            structures=_resolve_process_catalog,
            highlight_field="init",
            action_field="init",
            value_fields=("processes", "all tasks"),
            insight="In process catalog, follow 'init (pid 1)' to begin lifecycle trace.",
            flow_label="process catalog",
        ),
        TourStep(
            title="Initial task (init_task / swapper 0)",
            action="sched › init_task",
            commentary=(
                "Statically compiled idle task. All other processes and kernel threads "
                "descend from init_task through kernel_clone."
            ),
            userspace="pstree -p; ps -ef",
            structures=_resolve_init_task,
            highlight_field="cred",
            action_field="cred",
            value_fields=("comm", "pid"),
            insight="The statically allocated idle task every other task descends from.",
            flow_label="init_task",
        ),
        TourStep(
            title="Process credentials (struct cred)",
            action="task_struct › cred",
            commentary=(
                "Holds effective UID/GID, capability sets (effective, permitted, inheritable), "
                "and LSM security context. Clone copies or shares cred."
            ),
            userspace="id; capsh --print; /proc/1/status",
            structures=_resolve_init_cred,
            highlight_field="nsproxy",
            action_field="nsproxy",
            value_fields=("uid", "gid", "euid"),
            insight="Credentials: UID, GID and the capability sets.",
            flow_label="cred",
        ),
        TourStep(
            title="Namespace proxy (struct nsproxy)",
            action="task_struct › nsproxy",
            commentary=(
                "Groups pointers to UTS, IPC, mount, PID, network, and cgroup namespaces. "
                "Container engines pass CLONE_NEW* flags to instantiate private namespaces."
            ),
            userspace="ls -l /proc/1/ns; lsns",
            structures=_resolve_init_nsproxy,
            highlight_field="net_ns",
            action_field="net_ns",
            value_fields=("pid_ns_for_children", "net_ns"),
            insight="nsproxy groups the namespaces a task belongs to.",
            flow_label="nsproxy",
        ),
        TourStep(
            title="Network namespace boundary (struct net)",
            action="nsproxy › net_ns (struct net)",
            commentary=(
                "Each network namespace maintains its own loopback device, network interfaces, "
                "routing tables, firewall rules, and socket hash tables."
            ),
            userspace="ip netns list; ip link show",
            structures=_resolve_init_net,
            highlight_field="dev_base_head",
            action_field="dev_base_head",
            value_fields=("dev_base_head", "loopback_dev"),
            insight="A network namespace holds its own devices, routes and socket tables.",
            flow_label="net_ns",
        ),
        TourStep(
            title="Open file table (struct files_struct)",
            action="task_struct › files",
            commentary=(
                "Maintains the fdtable array of struct file pointers. Threads share files_struct "
                "via CLONE_FILES; fork duplicates via copy_files."
            ),
            userspace="ls -l /proc/1/fd; lsof -p 1",
            structures=_resolve_init_files,
            highlight_field="fdt",
            action_field="fdt",
            value_fields=("fdt", "max_fds"),
            insight="Open file descriptor table (fdtable) mapping integer fds to struct file.",
            flow_label="files_struct",
        ),
        TourStep(
            title="Open file object & dentry path (struct file)",
            action="fdt->fd[0] › struct file",
            commentary=(
                "Represents an opened file instance, tracking file position (f_pos), access modes "
                "(f_flags), and dentry path."
            ),
            userspace="ls -l /proc/1/fd/0",
            structures=_resolve_init_fd0,
            highlight_field="f_inode",
            action_field="f_inode",
            value_fields=("f_pos", "f_flags"),
            insight="One open file instance, with its position and access mode.",
            flow_label="struct file",
        ),
        TourStep(
            title="Underlying filesystem inode (struct inode)",
            action="file->f_inode › struct inode",
            commentary=(
                "The filesystem inode: records file permissions (i_mode), inode number (i_ino), "
                "file size (i_size), and storage device. Completes file table traversal."
            ),
            userspace="stat /proc/1/fd/0",
            structures=_resolve_init_inode0,
            highlight_field="i_ino",
            action_field="",
            value_fields=("i_ino", "i_mode", "i_size"),
            insight="Filesystem inode recording file size, mode, and storage device. Traversal complete.",
            flow_label="struct inode",
        ),
    ]


# ----------------------------------------------------- Memory Tour Builders


def _build_user_memory_steps(prog: Program | None) -> list[TourStep]:
    task = _find_live_target_task(prog)
    comm = task.comm.string_().decode() if task else "systemd"
    pid = task.pid.value_() if task else 1

    # The VMA list names its rows by start address, so a step that asks for
    # "the text VMA" matches nothing and shows no [ENTER] marker. Resolve the
    # executable mapping's address here and point the step at that row.
    text_vma_addr = ""
    text_vma_file = ""
    if prog is not None and task is not None:
        try:
            from drgn.helpers.linux.mm import for_each_vma, vma_name

            if task.mm:
                for vma in for_each_vma(task.mm):
                    if vma.vm_flags.value_() & 0x4:  # VM_EXEC
                        text_vma_addr = f"{vma.vm_start.value_():#x}"
                        name = vma_name(vma)
                        text_vma_file = name.decode("utf-8", "replace") if name else ""
                        break
        except Exception:  # noqa: BLE001
            text_vma_addr = ""

    def _resolve_mm(p: Program):
        from drgn.helpers.linux.pid import find_task

        t = find_task(p, pid) if pid else None
        if t is None:
            t = _find_live_target_task(p)
        mm = t.mm if t and t.mm else p["init_mm"].address_of_()
        yield f"{comm} mm_struct", mm

    def _resolve_text(p: Program):
        from drgn.helpers.linux.mm import for_each_vma, vma_name
        from drgn.helpers.linux.pid import find_task

        t = find_task(p, pid) if pid else None
        if t is None:
            t = _find_live_target_task(p)
        mm = t.mm if t and t.mm else p["init_mm"].address_of_()
        for vma in for_each_vma(mm):
            vn = (vma_name(vma) or b"").decode("utf-8", "replace")
            if (vma.vm_flags.value_() & 0x4) and vn:  # VM_EXEC
                yield f"{vma.vm_start.value_():#x} text ({vn})", vma
                return
        for vma in for_each_vma(mm):
            if vma.vm_flags.value_() & 0x4:
                yield f"{vma.vm_start.value_():#x} text (exec)", vma
                return

    def _resolve_libc(p: Program):
        from drgn.helpers.linux.mm import for_each_vma, vma_name
        from drgn.helpers.linux.pid import find_task, for_each_task

        t = find_task(p, pid) if pid else None
        if t is None:
            t = _find_live_target_task(p)
        mm = t.mm if t and t.mm else p["init_mm"].address_of_()
        candidates = []
        for vma in for_each_vma(mm):
            vn = (vma_name(vma) or b"").decode("utf-8", "replace")
            if ".so" in vn:
                candidates.append((vn, vma))
        # "libc" is a substring of libcap-ng.so and others, so match the path
        # component rather than testing for it anywhere in the name.
        for vn, vma in candidates:
            if "/libc.so" in vn:
                yield f"{vma.vm_start.value_():#x} shared lib ({vn})", vma
                return
        if candidates:
            vn, vma = candidates[0]
            yield f"{vma.vm_start.value_():#x} shared lib ({vn})", vma
            return
        for task_other in for_each_task(p):
            if not task_other.mm or not task_other.mm.value_():
                continue
            for vma in for_each_vma(task_other.mm):
                vn = (vma_name(vma) or b"").decode("utf-8", "replace")
                if ".so" in vn:
                    yield f"{vma.vm_start.value_():#x} shared lib ({vn})", vma
                    return
        for vma in for_each_vma(mm):
            if vma.vm_file:
                yield f"{vma.vm_start.value_():#x} file-backed VMA", vma
                return

    def _resolve_heap(p: Program):
        from drgn.helpers.linux.mm import for_each_vma, vma_name
        from drgn.helpers.linux.pid import find_task, for_each_task

        t = find_task(p, pid) if pid else None
        if t is None:
            t = _find_live_target_task(p)
        mm = t.mm if t and t.mm else p["init_mm"].address_of_()
        for vma in for_each_vma(mm):
            vn = vma_name(vma)
            if vn and b"[heap]" in vn:
                yield f"{comm} [heap] (vm_area_struct)", vma
                return
        for task_other in for_each_task(p):
            if not task_other.mm or not task_other.mm.value_():
                continue
            for vma in for_each_vma(task_other.mm):
                vn = vma_name(vma)
                if vn and b"[heap]" in vn:
                    yield f"{task_other.comm.string_().decode()} [heap] (vm_area_struct)", vma
                    return
        for vma in for_each_vma(mm):
            if (vma.vm_flags.value_() & 0x2) and not vma.vm_file:
                yield f"{comm} heap/anon (vm_area_struct)", vma
                return

    def _resolve_stack(p: Program):
        from drgn.helpers.linux.mm import for_each_vma, vma_name
        from drgn.helpers.linux.pid import find_task

        t = find_task(p, pid) if pid else None
        if t is None:
            t = _find_live_target_task(p)
        mm = t.mm if t and t.mm else p["init_mm"].address_of_()
        for vma in for_each_vma(mm):
            vn = vma_name(vma)
            if (vn and b"[stack]" in vn) or (vma.vm_flags.value_() & 0x100):
                yield f"{comm} [stack] (vm_area_struct)", vma
                return
        vmas = list(for_each_vma(mm))
        if vmas:
            yield f"{comm} stack region (vm_area_struct)", vmas[-1]

    def _resolve_shm(p: Program):
        from drgn.helpers.linux.mm import for_each_vma, vma_name
        from drgn.helpers.linux.pid import find_task, for_each_task

        for task_other in for_each_task(p):
            if not task_other.mm or not task_other.mm.value_():
                continue
            for vma in for_each_vma(task_other.mm):
                if vma.vm_flags.value_() & 0x8:  # VM_SHARED
                    vn = (vma_name(vma) or b"").decode("utf-8", "replace")
                    yield f"{task_other.comm.string_().decode()} shm ({vn or 'shared'})", vma
                    return
        t = find_task(p, pid) if pid else None
        mm = t.mm if t and t.mm else p["init_mm"].address_of_()
        for vma in for_each_vma(mm):
            yield f"{comm} VMA", vma
            return

    def _resolve_memfd(p: Program):
        from drgn.helpers.linux.mm import for_each_vma, vma_name
        from drgn.helpers.linux.pid import for_each_task

        for task_other in for_each_task(p):
            if not task_other.mm or not task_other.mm.value_():
                continue
            for vma in for_each_vma(task_other.mm):
                vn = (vma_name(vma) or b"").decode("utf-8", "replace")
                if "memfd" in vn or "tmpfs" in vn:
                    yield f"{task_other.comm.string_().decode()} memfd/tmpfs ({vn})", vma
                    return
        for task_other in for_each_task(p):
            if not task_other.mm or not task_other.mm.value_():
                continue
            for vma in for_each_vma(task_other.mm):
                if vma.vm_flags.value_() & 0x8:
                    yield f"{task_other.comm.string_().decode()} shared VMA", vma
                    return

    def _resolve_thp(p: Program):
        from drgn.helpers.linux.mm import for_each_vma
        from drgn.helpers.linux.pid import find_task, for_each_task

        pmd_size = 2 * 1024 * 1024
        for task_other in for_each_task(p):
            if not task_other.mm or not task_other.mm.value_():
                continue
            for vma in for_each_vma(task_other.mm):
                size = vma.vm_end.value_() - vma.vm_start.value_()
                if size >= pmd_size and (vma.vm_start.value_() % pmd_size == 0):
                    c = task_other.comm.string_().decode()
                    yield f"{c} 2MB-aligned THP region ({size // 1024} kB)", vma
                    return
        t = find_task(p, pid) if pid else None
        mm = t.mm if t and t.mm else p["init_mm"].address_of_()
        best = None
        best_size = 0
        for vma in for_each_vma(mm):
            size = vma.vm_end.value_() - vma.vm_start.value_()
            if size > best_size:
                best = vma
                best_size = size
        if best:
            yield f"{comm} large VMA ({best_size // 1024} kB)", best

    def _resolve_rss(p: Program):
        from drgn.helpers.linux.pid import find_task

        t = find_task(p, pid) if pid else None
        if t is None:
            t = _find_live_target_task(p)
        mm = t.mm if t and t.mm else p["init_mm"].address_of_()
        yield f"{comm} mm_struct (rss_stat)", mm

    def _resolve_task(p: Program):
        from drgn.helpers.linux.pid import find_task
        t = find_task(p, pid) if pid else None
        if t is None:
            t = _find_live_target_task(p)
        yield f"{comm} (PID {pid}) task_struct", t

    def _resolve_vmas_list(p: Program):
        from drgn.helpers.linux.mm import for_each_vma, vma_name
        from drgn.helpers.linux.pid import find_task

        from ..catalog.links import vma_perms

        t = find_task(p, pid) if pid else None
        if t is None:
            t = _find_live_target_task(p)
        mm = t.mm if t and t.mm else p["init_mm"].address_of_()
        for vma in for_each_vma(mm):
            name = vma_name(vma)
            label = name.decode("utf-8", "replace") if name else "anon"
            # The permissions are what separate the three mappings one binary
            # contributes, and the step asks for the executable one by name.
            yield f"{vma.vm_start.value_():#x}  {vma_perms(vma)}  {label}", vma

    return [
        TourStep(
            title="Starting screen: kexplore home & process navigation",
            action="kexplore › a process and its address space",
            commentary=(
                f"The walkthrough begins at the task_struct for '{comm}' and follows its "
                f"mm pointer."
            ),
            userspace="uname -r; nproc; uptime",
            structures=_resolve_home,
            highlight_field="a process and its address space",
            action_field="a process and its address space",
            value_fields=("kernel release", "architecture", "total RAM"),
            insight=(
                "The opening screen lists entry points. 'a process and its address space' opens "
                f"init (pid {pid}); every later step is a pointer followed from there."
            ),
            flow_label="kexplore home",
        ),
        TourStep(
            title=f"Process descriptor: struct task_struct ({comm}, PID {pid})",
            action="process › init (pid 1) › follow mm",
            commentary=(
                "A process is represented by a struct task_struct. The mm field points to "
                "its address space."
            ),
            userspace=f"cat /proc/{pid}/status | grep -E '(Pid|Tgid|Threads)'",
            structures=_resolve_task,
            highlight_field="mm",
            action_field="mm",
            value_fields=("pid", "tgid", "comm"),
            insight="The task_struct for one process; mm points to its memory.",
            flow_label="task_struct",
        ),
        TourStep(
            title=f"Address space boundaries: struct mm_struct ({comm})",
            action="task_struct › mm (address space) › follow VMAs",
            commentary=(
                "A struct mm_struct is the process's address space, composed of the "
                "virtual memory areas (VMAs) it has mapped. "
                "start_code, brk and start_stack record where the executable, the heap "
                "and the stack begin; mm_struct indexes the VMAs in a maple tree keyed "
                "by address."
            ),
            userspace=f"cat /proc/{pid}/maps | head -n 10",
            structures=_resolve_mm,
            highlight_field="VMAs",
            action_field="VMAs",
            value_fields=("start_code", "end_code", "start_brk", "brk", "start_stack"),
            insight="The boundary fields bound the executable, the heap and the stack.",
            flow_label="mm_struct",
        ),
        TourStep(
            title=f"Process memory map: maple tree VMAs (VMAs of pid {pid})",
            action="mm_struct › VMAs › select executable text VMA",
            commentary=(
                f"Each mapping is a struct vm_area_struct, indexed by address, and is "
                f"either file-backed or anonymous. "
                f"{text_vma_file or 'The executable'} appears three times because an ELF "
                f"binary is loaded as three mappings, one per set of permissions, "
                f"distinguished only by vm_flags. The next five steps open one mapping each: "
                f"executable code, shared library, heap, stack, and a 2MB-aligned region."
            ),
            userspace=f"cat /proc/{pid}/maps | head -n 15",
            structures=_resolve_vmas_list,
            highlight_field=text_vma_addr or "text",
            action_field=text_vma_addr or "text",
            value_fields=(),
            insight=(
                "The maple tree indexes every VMA by address; each row is one struct vm_area_struct. "
                "Open the executable mapping."
            ),
            flow_label="VMAs list",
        ),
        TourStep(
            title="File-backed private memory: executable text segment (r-xp)",
            action="VMA › vm_area_struct (r-xp binary text)",
            commentary=(
                f"Executable code. vm_file points to the struct file for "
                f"{text_vma_file or 'the executable'}. vm_flags sets VM_READ and VM_EXEC "
                f"but not VM_WRITE, so vm_start to vm_end can be read and executed, never "
                f"written."
            ),
            userspace=f"grep 'r-xp' /proc/{pid}/maps",
            structures=_resolve_text,
            highlight_field="vm_file",
            action_field="vm_file",
            value_fields=("vm_flags", "vm_start", "vm_end"),
            insight="A private file-backed mapping carrying the executable code.",
            flow_label="text (r-xp)",
        ),
        TourStep(
            title="File-backed shared library: dynamically linked code",
            action="VMA › shared library VMA (read-only code)",
            commentary=(
                "Shared library, mapped r-xp like the executable. Other processes map "
                "the same file at different addresses, and vm_file leads to the single "
                "inode behind all of them."
            ),
            userspace=f"grep -E 'libc.*\\.so' /proc/{pid}/maps || grep '\\.so' /proc/{pid}/maps | head -n 5",
            structures=_resolve_libc,
            highlight_field="vm_file",
            action_field="vm_file",
            value_fields=("vm_start", "vm_end", "vm_flags"),
            insight="Shared libraries map identical physical code pages across all running processes.",
            flow_label="shared lib",
        ),
        TourStep(
            title="Anonymous memory: dynamic heap ([heap])",
            action="VMA › [heap] (brk anonymous dynamic memory)",
            commentary=(
                "Heap: vm_file is NULL, which is what anonymous means. brk() extends "
                "the heap. An anonymous page points at anon_vma rather than at a VMA, "
                "because forking and splitting can leave several VMAs mapping that page."
            ),
            userspace=f"grep '\\[heap\\]' /proc/{pid}/maps",
            structures=_resolve_heap,
            highlight_field="anon_vma",
            action_field="anon_vma",
            value_fields=("vm_start", "vm_end", "anon_vma"),
            insight="Dynamic heap has no file on disk (vm_file == NULL). Backed by RAM via anon_vma.",
            flow_label="heap (anon)",
        ),
        TourStep(
            title="Anonymous memory: user execution stack ([stack])",
            action="VMA › [stack] (VM_GROWSDOWN)",
            commentary=(
                "Stack: anonymous, marked VM_GROWSDOWN. A fault "
                "just below vm_start extends the mapping downwards, as far as the process's "
                "stack limit allows."
            ),
            userspace=f"grep '\\[stack\\]' /proc/{pid}/maps",
            structures=_resolve_stack,
            highlight_field="vm_flags",
            action_field="vm_flags",
            value_fields=("vm_start", "vm_end", "vm_flags"),
            insight="Stack grows downward (VM_GROWSDOWN) on demand as execution depth increases.",
            flow_label="stack (anon)",
        ),
        TourStep(
            title="Transparent huge pages: a 2MB-aligned anonymous region",
            action="VMA › 2MB-aligned anonymous region",
            commentary=(
                "2MB-aligned anonymous region: large enough for "
                "transparent huge pages. Where one is used, a single PMD entry maps the "
                "whole 2MB, in place of the 512 page table entries a 4KB mapping would "
                "need."
            ),
            userspace=f"grep AnonHugePages /proc/{pid}/smaps_rollup",
            structures=_resolve_thp,
            highlight_field="vm_flags",
            action_field="vm_flags",
            value_fields=("vm_start", "vm_end", "vm_flags"),
            insight="A 2MB PMD entry can replace the 512 page table entries a 4KB mapping needs.",
            flow_label="huge pages",
        ),
        TourStep(
            title="Memory accounting: resident page counts (rss_stat)",
            action="mm->rss_stat › per-type resident counters",
            commentary=(
                "rss_stat holds one percpu_counter per counter kind (NR_MM_COUNTERS): "
                "file, anonymous, swap entries and shmem. VmRSS adds the file, "
                "anonymous and shmem counters, each taken as its batched count plus "
                "the pending delta on every CPU."
            ),
            userspace=f"cat /proc/{pid}/status | grep -E '(VmRSS|RssAnon|RssFile|RssShmem)'",
            structures=_resolve_rss,
            highlight_field="rss_stat",
            action_field="",
            value_fields=("hiwater_rss", "hiwater_vm", "total_vm"),
            insight="Resident memory broken down into File, Anon, and Shmem. Traversal complete.",
            flow_label="rss_stat",
        ),
    ]


def _build_page_table_steps(prog: Program | None) -> list[TourStep]:
    def _resolve_pgd(p: Program):
        from drgn.helpers.linux.pid import find_task

        t = find_task(p, 1)
        mm = t.mm or p["init_mm"].address_of_()
        yield "pgd (page global directory)", mm.pgd

    def _resolve_mm(p: Program):
        from drgn.helpers.linux.pid import find_task

        t = find_task(p, 1)
        mm = t.mm or p["init_mm"].address_of_()
        yield "init (pid 1) mm", mm

    def _resolve_vma(p: Program):
        from drgn.helpers.linux.pid import find_task
        from drgn.helpers.linux.mm import for_each_vma, vma_name

        t = find_task(p, 1)
        mm = t.mm or p["init_mm"].address_of_()
        for vma in for_each_vma(mm):
            if (vma.vm_flags.value_() & 0x4):
                vn = vma_name(vma)
                lbl = vn.decode("utf-8", "replace") if vn else "text"
                yield f"{vma.vm_start.value_():#x} {lbl}", vma
                return

    def _resolve_first_pages(p: Program):
        from drgn.helpers.linux.pid import find_task
        from drgn.helpers.linux.mm import for_each_vma, follow_page, page_to_pfn

        t = find_task(p, 1)
        mm = t.mm or p["init_mm"].address_of_()
        count = 0
        for vma in for_each_vma(mm):
            for addr in range(vma.vm_start.value_(), min(vma.vm_end.value_(), vma.vm_start.value_() + 32 * 4096), 4096):
                page = ct.safe(lambda a=addr: follow_page(mm, a), None)
                if page is not None and page.value_():
                    pfn = page_to_pfn(page).value_()
                    yield f"Page at {addr:#x} (PFN {pfn:#x})", page
                    count += 1
                    if count >= 8:
                        return

    def _resolve_single_page(p: Program):
        from drgn.helpers.linux.pid import find_task
        from drgn.helpers.linux.mm import for_each_vma, follow_page, page_to_pfn

        t = find_task(p, 1)
        mm = t.mm or p["init_mm"].address_of_()
        for vma in for_each_vma(mm):
            for addr in range(vma.vm_start.value_(), min(vma.vm_end.value_(), vma.vm_start.value_() + 32 * 4096), 4096):
                page = ct.safe(lambda a=addr: follow_page(mm, a), None)
                if page is not None and page.value_():
                    pfn = page_to_pfn(page).value_()
                    yield f"Physical page (PFN {pfn:#x})", page
                    return

    def _resolve_anon_vma(p: Program):
        from drgn.helpers.linux.pid import find_task
        from drgn.helpers.linux.mm import for_each_vma

        t = find_task(p, 1)
        mm = t.mm or p["init_mm"].address_of_()
        for vma in for_each_vma(mm):
            if vma.anon_vma:
                yield "anon_vma (reverse mapping)", vma.anon_vma
                return
        yield "init_mm", mm

    def _resolve_zones(p: Program):
        from drgn.helpers.linux.mmzone import for_each_online_pgdat

        for pgdat in for_each_online_pgdat(p):
            node = pgdat.node_id.value_()
            for index in range(pgdat.nr_zones.value_()):
                zone = pgdat.node_zones[index]
                name = zone.name.string_().decode("utf-8", "replace")
                yield f"node{node} {name}", zone

    return [
        TourStep(
            title="Starting screen: kexplore entry points",
            action="kexplore › kernel configuration and topology",
            commentary=(
                "kexplore is attached to the running kernel. Every structure in this "
                "walkthrough is reached by dereferencing a pointer in the one before it."
            ),
            userspace="cat /proc/meminfo | head -n 15",
            structures=_resolve_home,
            highlight_field="kernel configuration and topology",
            action_field="kernel configuration and topology",
            value_fields=("kernel release", "struct docs and source"),
            insight="The opening screen names the kernel and what the tool can resolve against it.",
            flow_label="kexplore",
        ),
        TourStep(
            title="Main menu: memory subsystem catalog",
            action="subsystems › mm catalog",
            commentary=(
                "init_mm is the kernel's own address space, active whenever no userspace "
                "process is. Its page tables are the ones a CPU uses while running kernel code "
                "with no user mapping installed."
            ),
            userspace="cat /proc/meminfo | head -n 15",
            structures=_resolve_mm_catalog,
            highlight_field="init_mm",
            action_field="init_mm",
            value_fields=("vma_types", "maple_tree"),
            insight="In memory catalog, follow 'init_mm' to open translation root.",
            flow_label="mm catalog",
        ),
        TourStep(
            title="Translation root (PGD register: CR3 / TTBR0)",
            action="mm_struct › pgd",
            commentary=(
                "Page Global Directory base address loaded into CPU hardware translation register "
                "(CR3 on x86_64, TTBR0_EL1 on ARM64) during context switch."
            ),
            userspace="/proc/kcore; /proc/1/pagemap",
            structures=_resolve_pgd,
            highlight_field="pgd",
            action_field="pgd",
            value_fields=("pgd",),
            insight="Page Global Directory translation root loaded into CPU MMU register.",
            flow_label="PGD",
        ),
        TourStep(
            title="Virtual address space limits (struct mm_struct)",
            action="task->mm › mm_struct",
            commentary=(
                "Virtual addresses must fall within bounded ranges defined in mm_struct. "
                "The MMU translates virtual addresses within start_code..end_code and heap ranges."
            ),
            userspace="cat /proc/1/maps | head -n 5",
            structures=_resolve_mm,
            highlight_field="VMAs",
            action_field="VMAs",
            value_fields=("start_code", "end_code", "pgd"),
            insight="Virtual range translated across intermediate page directories (PUD/PMD).",
            flow_label="mm_struct",
        ),
        TourStep(
            title="Virtual mapping & permission bits (struct vm_area_struct)",
            action="mm->mm_mt › vm_area_struct",
            commentary=(
                "VMA bounds define valid translation ranges; vm_flags determine hardware PTE protection "
                "bits (VM_READ, VM_WRITE, VM_EXEC)."
            ),
            userspace="grep 'r-xp' /proc/1/maps",
            structures=_resolve_vma,
            highlight_field="resident pages",
            action_field="resident pages",
            value_fields=("vm_start", "vm_end", "vm_flags"),
            insight="VMA boundaries define valid translation ranges and PTE hardware permissions.",
            flow_label="VMAs",
        ),
        TourStep(
            title="Multi-level page table descent (PGD ➔ PUD ➔ PMD ➔ PTE)",
            action="walk_page_range › resident pages",
            commentary=(
                "Hardware MMU descends 4 levels: PGD (bits 47:39) -> PUD (bits 38:30) -> PMD (bits 29:21) -> PTE (bits 20:12). "
                "Huge pages terminate early at PMD (2MB)."
            ),
            userspace="/proc/1/pagemap (PFN in bits 0-54)",
            structures=_resolve_first_pages,
            highlight_field="flags",
            action_field="flags",
            value_fields=("pfn", "flags"),
            insight="MMU walks 4 levels down to leaf PTE. Huge pages terminate early at PMD.",
            flow_label="PUD ➔ PTE",
        ),
        TourStep(
            title="Leaf PTE & physical RAM frame (struct page)",
            action="pte_t › pfn_to_page › struct page",
            commentary=(
                "Leaf PTE contains physical frame number (PFN) and hardware bits. "
                "pfn_to_page indexes the kernel vmemmap array of struct page."
            ),
            userspace="/proc/1/pagemap",
            structures=_resolve_single_page,
            highlight_field="mapping",
            action_field="mapping",
            value_fields=("pfn", "flags"),
            insight="Leaf PTE resolves into physical memory page frame (struct page).",
            flow_label="struct page",
        ),
        TourStep(
            title="Reverse mapping (RMAP: page ➔ VMAs)",
            action="struct page › anon_vma / mapping",
            commentary=(
                "Reverse mapping (RMAP) allows kernel to find all page tables referencing this physical page "
                "when unmapping, reclaiming (kswapd), or write-protecting for Copy-on-Write."
            ),
            userspace="cat /proc/meminfo | grep -E '(AnonPages|Mapped)'",
            structures=_resolve_anon_vma,
            highlight_field="root",
            action_field="root",
            value_fields=("root", "degree"),
            insight="Reverse mapping allows kernel to find all page tables referencing this physical page.",
            flow_label="anon_vma (RMAP)",
        ),
        TourStep(
            title="Physical memory zones & buddy allocator (struct zone)",
            action="mm › zones (pglist_data)",
            commentary=(
                "Physical memory frames are grouped into NUMA nodes and zones (ZONE_DMA, ZONE_NORMAL). "
                "Each zone maintains free lists for the buddy allocator and watermarks (min, low, high)."
            ),
            userspace="/proc/zoneinfo; /proc/buddyinfo",
            structures=_resolve_zones,
            highlight_field="",
            action_field="",
            value_fields=("name", "node_zones", "managed_pages"),
            insight="Physical frames partitioned into NUMA nodes and hardware addressing zones.",
            flow_label="NUMA zones",
        ),
    ]


# --------------------------------------------------- Scheduler Tour Builders


def _build_eevdf_scheduler_steps(prog: Program | None) -> list[TourStep]:
    def _rq_of_cpu0(p: Program):
        from drgn.helpers.linux.percpu import per_cpu

        yield "CPU 0 runqueue (struct rq)", per_cpu(p["runqueues"], 0).address_of_()

    def _cfs_rq_of_cpu0(p: Program):
        from drgn.helpers.linux.percpu import per_cpu

        yield "CPU 0 fair queue (struct cfs_rq)", per_cpu(p["runqueues"], 0).cfs.address_of_()

    def _resolve_se(p: Program):
        from drgn.helpers.linux.percpu import per_cpu
        from drgn.helpers.linux.pid import find_task

        rq = per_cpu(p["runqueues"], 0)
        if rq.cfs.curr:
            yield "CPU 0 active sched_entity", rq.cfs.curr
        else:
            t = find_task(p, 1)
            yield "init sched_entity (se)", t.se.address_of_()

    def _a_running_task(p: Program):
        from drgn.helpers.linux.pid import find_task
        from drgn.helpers.linux.sched import cpu_curr

        t = cpu_curr(p, 0) or find_task(p, 1)
        c = t.comm.string_().decode() if t and t.comm else "task"
        pid_val = t.pid.value_() if t and t.pid else 0
        yield f"CPU 0 running task: {c} (PID {pid_val})", t

    def _a_sleeping_task(p: Program):
        from drgn.helpers.linux.pid import find_task

        t = find_task(p, 1)
        c = t.comm.string_().decode() if t and t.comm else "init"
        yield f"PID 1 ({c}): __state", t

    def _context_switch(p: Program):
        from drgn.helpers.linux.percpu import per_cpu

        yield "CPU 0 runqueue (context switch)", per_cpu(p["runqueues"], 0).address_of_()

    return [
        TourStep(
            title="Starting screen: kexplore entry points",
            action="kexplore › what is running right now",
            commentary=(
                "kexplore is attached to the running kernel. Every structure in this "
                "walkthrough is reached by dereferencing a pointer in the one before it."
            ),
            userspace="cat /proc/sched_debug | head -n 25",
            structures=_resolve_home,
            highlight_field="what is running right now",
            action_field="what is running right now",
            value_fields=("kernel release", "struct docs and source"),
            insight="The opening screen names the kernel and what the tool can resolve against it.",
            flow_label="kexplore",
        ),
        TourStep(
            title="Main menu: scheduler subsystem catalog",
            action="subsystems › sched catalog",
            commentary=(
                "Each CPU owns a struct rq holding the tasks runnable on it. The next task is "
                "chosen from that structure alone, which is what makes scheduling a per-CPU "
                "decision rather than a global one."
            ),
            userspace="cat /proc/sched_debug | head -n 25",
            structures=_resolve_sched_catalog,
            highlight_field="runqueues",
            action_field="runqueues",
            value_fields=("fair_sched",),
            insight="In scheduler catalog, follow 'runqueues' into per-CPU queues.",
            flow_label="sched catalog",
        ),
        TourStep(
            title="Per-CPU runqueue (struct rq)",
            action="sched › struct rq (runqueue)",
            commentary=(
                "Each CPU has a dedicated struct rq holding fair (cfs_rq), realtime (rt_rq), "
                "and deadline (dl_rq) scheduling queues."
            ),
            userspace="uptime; mpstat -P ALL 1",
            structures=_rq_of_cpu0,
            highlight_field="cfs",
            action_field="cfs",
            value_fields=("nr_running", "curr"),
            insight="One struct rq per CPU, holding the fair, realtime and deadline queues.",
            flow_label="struct rq",
        ),
        TourStep(
            title="CFS fair runqueue (struct cfs_rq)",
            action="rq › cfs (weighted virtual timeline)",
            commentary=(
                "EEVDF maintains a weighted virtual timeline. zero_vruntime and sum_w_vruntime "
                "provide the reference average virtual time (avg_vruntime) for lag calculation."
            ),
            userspace="/proc/sched_debug; sysctl kernel.sched_base_slice_ns",
            structures=_cfs_rq_of_cpu0,
            highlight_field="curr",
            action_field="curr",
            value_fields=("sum_w_vruntime", "zero_vruntime", "nr_queued"),
            insight="cfs_rq keeps the weighted virtual timeline EEVDF orders tasks on.",
            flow_label="struct cfs_rq",
        ),
        TourStep(
            title="Sched entity: vruntime & deadline (struct sched_entity)",
            action="cfs_rq › curr (sched_entity)",
            commentary=(
                "EEVDF computes lag = avg_vruntime - vruntime. A task is eligible when lag >= 0. "
                "Entities receive CPU proportional to their weight (load.weight, derived from nice)."
            ),
            userspace="cat /proc/1/sched (vruntime, slice)",
            structures=_resolve_se,
            highlight_field="deadline",
            action_field="deadline",
            value_fields=("vruntime", "deadline", "slice"),
            insight="EEVDF task entity: lag = avg_vruntime - vruntime. If lag >= 0, entity is eligible.",
            flow_label="sched_entity",
        ),
        TourStep(
            title="Virtual deadline & earliest deadline selection",
            action="se › deadline = vruntime + slice / weight",
            commentary=(
                "Virtual deadline: deadline = vruntime + slice / weight. pick_next_task_fair "
                "selects the eligible entity with the earliest deadline."
            ),
            userspace="cat /proc/sys/kernel/sched_base_slice_ns",
            structures=_resolve_se,
            highlight_field="vruntime",
            action_field="vruntime",
            value_fields=("deadline", "vruntime"),
            insight="EEVDF picks the eligible entity with earliest deadline. Follow to active task.",
            flow_label="deadline",
        ),
        TourStep(
            title="Active task scheduled on CPU (struct task_struct)",
            action="rq › curr (currently running task)",
            commentary=(
                "Currently executing task on CPU. When slice expires or higher priority "
                "becomes eligible, __schedule() is invoked. Follow to inspect process states."
            ),
            userspace="chrt -p 1; ps -eo pid,comm,psr,stat,pri",
            structures=_a_running_task,
            highlight_field="policy",
            action_field="policy",
            value_fields=("comm", "pid", "policy", "prio"),
            insight="Currently executing task scheduled on CPU. Monitored by timer interrupts.",
            flow_label="active task",
        ),
        TourStep(
            title="Process sleep states & wait queues (TASK_INTERRUPTIBLE)",
            action="task_struct › __state & wait queues",
            commentary=(
                "Blocked processes sleep with __state set to TASK_INTERRUPTIBLE (1) or "
                "TASK_UNINTERRUPTIBLE (2) and wait on kernel wait queues until awakened by try_to_wake_up."
            ),
            userspace="ps -eo pid,comm,state,wchan | head -n 20",
            structures=_a_sleeping_task,
            highlight_field="__state",
            action_field="__state",
            value_fields=("__state", "exit_state"),
            insight="Blocked processes transition to TASK_INTERRUPTIBLE and sleep on wait queues.",
            flow_label="wait queues",
        ),
        TourStep(
            title="Context switch mechanics (__schedule ➔ switch_to)",
            action="__schedule › switch_mm_irqs_off & switch_to",
            commentary=(
                "When __schedule() picks a new task, context_switch() swaps address spaces "
                "(updating CR3/TTBR0) and restores CPU register state with switch_to."
            ),
            userspace="perf stat -e context-switches,cpu-migrations -a -- sleep 1",
            structures=_context_switch,
            highlight_field="curr",
            action_field="curr",
            value_fields=("nr_running", "curr"),
            insight="Context switch swaps virtual address space and CPU register state. Traversal complete.",
            flow_label="context switch",
        ),
    ]


# ------------------------------------------------------------- Tour Registry


PROCESS_ARCHITECTURE = GuidedTour(
    key="process_architecture",
    label="Multi-threaded Process Architecture",
    category="process",
    doc=(
        "Live guided tour of thread groups, shared address spaces (CLONE_VM), "
        "maple tree VMA indexing, and signal handlers on active processes."
    ),
    builder=_build_process_architecture_steps,
    video_url="https://youtu.be/9jNWc8RUFvs",
    video_title="How Linux Runs a Program (Deep Linux)",
)

PROCESS_LIFECYCLE = GuidedTour(
    key="process_lifecycle",
    label="Process Lifecycle: Clone & Namespaces",
    category="process",
    doc=(
        "Live tour through kernel process creation: task_struct initialization, "
        "security credentials (cred), namespace isolation (nsproxy), and files."
    ),
    builder=_build_process_lifecycle_steps,
    video_url="https://youtu.be/7Bvx5Gd99F0",
    video_title="Inside a Linux Executable File (Deep Linux)",
)

USER_MEMORY_TYPES = GuidedTour(
    key="user_memory_types",
    label="User Memory Types & VMAs (Deep Linux)",
    category="memory",
    doc=(
        "Live tour through the user memory types: executable text (r-xp), data, "
        "dynamic heap ([heap]), user stack ([stack]), and physical RAM backing."
    ),
    builder=_build_user_memory_steps,
    video_url="https://youtu.be/6dwzZEFEgWE",
    video_title="Linux Memory Management: Types of User Memory (Deep Linux)",
)

PAGE_TABLE_TRANSLATION = GuidedTour(
    key="page_table_translation",
    label="Page Tables & Address Translation (Deep Linux)",
    category="memory",
    doc=(
        "Live multi-level page table walk: translation root (PGD), intermediate "
        "levels (PUD, PMD), leaf PTE, struct page, reverse mapping, and zones."
    ),
    builder=_build_page_table_steps,
    video_url="https://youtu.be/Y2oSY_eenQ4",
    video_title="Linux Memory Management: Page Tables & Address Translation (Deep Linux)",
)

EEVDF_SCHEDULER = GuidedTour(
    key="eevdf_scheduler",
    label="EEVDF Scheduler & Task Selection (Deep Linux)",
    category="sched",
    doc=(
        "Live tour of EEVDF scheduling mechanics: per-CPU runqueues, CFS "
        "virtual timeline, sched_entity lag, deadlines, wait queues, and context switching."
    ),
    builder=_build_eevdf_scheduler_steps,
    video_url="https://youtu.be/SdpaIMBOdv4",
    video_title="Linux Scheduler and Process Wait Chains (Deep Linux)",
)

TOURS: list[GuidedTour] = [
    PROCESS_ARCHITECTURE,
    PROCESS_LIFECYCLE,
    USER_MEMORY_TYPES,
    PAGE_TABLE_TRANSLATION,
    EEVDF_SCHEDULER,
]
TUTORIALS = TOURS


def tours() -> list[GuidedTour]:
    """All curated live guided exploration tours."""
    return list(TOURS)


def tutorials() -> list[GuidedTutorial]:
    """All curated live guided exploration tutorials."""
    return list(TOURS)

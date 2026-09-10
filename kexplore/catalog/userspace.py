"""Userspace equivalents for the subsystem entry points.

The link table in ``links.py`` covers edges out of a struct. This covers the
entries themselves: the lists you start from. Same purpose -- carry the mental
model to a box that has none of this installed.

An empty string means there is genuinely no userspace equivalent, which is
worth stating rather than leaving blank: it marks what only a debugger reaches.
"""

from __future__ import annotations

import re

# Keyed by (subsystem key, entry key).
ENTRY_COMMANDS: dict[tuple[str, str], str] = {
    # process
    ("process", "processes"): "ps -e",
    ("process", "tasks"): "ps -eL",
    ("process", "kthreads"): "ps -o pid,comm --ppid 2, or ps -ef | grep '\\['",
    ("process", "zombies"): "ps -eo pid,stat,comm | awk '$2 ~ /Z/'",
    ("process", "init"): "ps -p 1",
    # sched
    ("sched", "runqueues"): "cat /proc/schedstat",
    ("sched", "running"): "ps -eo pid,psr,comm --sort=psr",
    ("sched", "init_task"): "",
    # mm
    ("mm", "init_mm"): "",
    ("mm", "pgdat"): "numactl -H, or ls /sys/devices/system/node",
    ("mm", "zones"): "cat /proc/zoneinfo",
    ("mm", "vmas_pid1"): "cat /proc/1/maps",
    ("mm", "vmap"): "sudo cat /proc/vmallocinfo",
    # page
    ("page", "resident"): "cat /proc/1/smaps",
    ("page", "vmemmap"): "od -An -tu8 /proc/kpageflags  # one 64-bit word per page",
    ("page", "low"): "",
    # vfs
    ("vfs", "mounts"): "findmnt, or cat /proc/self/mountinfo",
    ("vfs", "superblocks"): "findmnt -o SOURCE,FSTYPE,TARGET",
    ("vfs", "all_files"): "lsof",
    ("vfs", "unique_files"): "lsof -n | awk '{print $9}' | sort -u",
    ("vfs", "files_pid1"): "ls -l /proc/1/fd",
    # socket
    ("socket", "process_sockets"): "ss -tanp",
    ("socket", "process_socks"): "ss -tanp",
    ("socket", "tcp_listen"): "ss -tln",
    ("socket", "tcp_estab"): "ss -tn state established",
    ("socket", "udp"): "ss -uan",
    ("socket", "unix"): "ss -xan",
    # net
    ("net", "init_net"): "readlink /proc/self/ns/net",
    ("net", "namespaces"): "ip netns list",
    ("net", "netdevs"): "ip -d link",
    ("net", "txqueues"): "ls /sys/class/net/*/queues, or tc -s qdisc",
    # skb
    ("skb", "nonempty"): "ss -tanmH | grep -o 'skmem:([^)]*)'",
    ("skb", "receive"): "ss -tanH | awk '{print $2}'  # Recv-Q",
    ("skb", "write"): "ss -tanH | awk '{print $3}'  # Send-Q",
    ("skb", "backlog"): "cat /proc/net/softnet_stat",
    ("skb", "qdisc"): "tc -s qdisc show",
    # slab
    ("slab", "caches"): "slabtop, or cat /proc/slabinfo",
    # device
    ("device", "devices"): "ls /sys/devices, or lsblk, or lspci, or ip link",
    ("device", "bound"): "ls -l /sys/bus/*/drivers/*/",
    ("device", "buses"): "ls /sys/bus",
    ("device", "classes"): "ls /sys/class",
    ("device", "pci"): "lspci -v",
    ("device", "disks"): "lsblk -d",
    ("device", "partitions"): "lsblk",
    # system
    ("system", "overview"): "uname -a; uptime; nproc; free -h",
    ("system", "scheduler"): "sysctl -a --pattern '^kernel.sched'; cat /sys/kernel/debug/sched/features",
    ("system", "memory"): "cat /proc/meminfo; cat /proc/buddyinfo",
}


# Individual fields that userspace exposes. Most do not: the kernel keeps far
# more state than it publishes, and that asymmetry is worth seeing. Only fields
# with a genuine equivalent are listed.
FIELD_COMMANDS: dict[tuple[str, str], str] = {
    # task_struct
    ("task_struct", "comm"): "cat /proc/<pid>/comm",
    ("task_struct", "__state"): "ps -o stat= -p <pid>",
    ("task_struct", "prio"): "awk '{sub(/.*\\) /,\"\"); print $16}' /proc/<pid>/stat  # prio minus 100",
    ("task_struct", "static_prio"): "ps -o ni= -p <pid>  # nice",
    ("task_struct", "policy"): "chrt -p <pid>",
    ("task_struct", "rt_priority"): "chrt -p <pid>",
    ("task_struct", "flags"): "awk '{sub(/.*\\) /,\"\"); print $7}' /proc/<pid>/stat  # the PF_* word",
    ("task_struct", "tgid"): "grep Tgid /proc/<pid>/status",
    ("task_struct", "on_cpu"): "ps -o psr= -p <pid>",
    ("task_struct", "cpus_mask"): "grep Cpus_allowed_list /proc/<pid>/status",
    ("task_struct", "nr_cpus_allowed"): "grep Cpus_allowed_list /proc/<pid>/status",
    ("task_struct", "utime"): "awk '{sub(/.*\\) /,\"\"); print $12}' /proc/<pid>/stat",
    ("task_struct", "stime"): "awk '{sub(/.*\\) /,\"\"); print $13}' /proc/<pid>/stat",
    ("task_struct", "gtime"): "awk '{sub(/.*\\) /,\"\"); print $41}' /proc/<pid>/stat",
    ("task_struct", "start_time"): "ps -o lstart= -p <pid>",
    ("task_struct", "min_flt"): "awk '{sub(/.*\\) /,\"\"); print $8}' /proc/<pid>/stat",
    ("task_struct", "maj_flt"): "awk '{sub(/.*\\) /,\"\"); print $10}' /proc/<pid>/stat",
    ("task_struct", "nvcsw"): "grep voluntary_ctxt_switches /proc/<pid>/status",
    ("task_struct", "nivcsw"): "grep nonvoluntary_ctxt_switches /proc/<pid>/status",
    ("task_struct", "pending"): "grep SigPnd /proc/<pid>/status",
    ("task_struct", "blocked"): "grep SigBlk /proc/<pid>/status",
    ("task_struct", "cred"): "grep -E 'Uid|Gid' /proc/<pid>/status",
    ("task_struct", "cgroups"): "cat /proc/<pid>/cgroup",
    ("task_struct", "loginuid"): "cat /proc/<pid>/loginuid",
    ("task_struct", "sessionid"): "cat /proc/<pid>/sessionid",
    ("task_struct", "exit_code"): "no userspace equivalent: only the parent sees it, from wait()",
    # mm_struct: /proc/<pid>/status publishes one Vm line per counter.
    ("mm_struct", "total_vm"): "grep VmSize /proc/<pid>/status",
    ("mm_struct", "hiwater_vm"): "grep VmPeak /proc/<pid>/status",
    ("mm_struct", "hiwater_rss"): "grep VmHWM /proc/<pid>/status",
    ("mm_struct", "locked_vm"): "grep VmLck /proc/<pid>/status",
    ("mm_struct", "pinned_vm"): "grep VmPin /proc/<pid>/status",
    ("mm_struct", "data_vm"): "grep VmData /proc/<pid>/status",
    ("mm_struct", "stack_vm"): "grep VmStk /proc/<pid>/status",
    ("mm_struct", "pgtables_bytes"): "grep VmPTE /proc/<pid>/status",
    ("mm_struct", "start_code"): "grep VmExe /proc/<pid>/status",
    ("mm_struct", "end_code"): "grep VmExe /proc/<pid>/status",
    ("mm_struct", "map_count"): "wc -l < /proc/<pid>/maps",
    ("mm_struct", "start_brk"): "grep '\\[heap\\]' /proc/<pid>/maps  # start of the range",
    ("mm_struct", "brk"): "grep '\\[heap\\]' /proc/<pid>/maps  # end of the range",
    ("mm_struct", "start_stack"): "grep '\\[stack\\]' /proc/<pid>/maps",
    ("mm_struct", "exe_file"): "readlink /proc/<pid>/exe",
    ("mm_struct", "arg_start"): "cat /proc/<pid>/cmdline",
    ("mm_struct", "env_start"): "cat /proc/<pid>/environ",
    # vm_area_struct: one line of /proc/<pid>/maps, column by column.
    ("vm_area_struct", "vm_start"): "awk '{print $1}' /proc/<pid>/maps",
    ("vm_area_struct", "vm_flags"): "awk '{print $2}' /proc/<pid>/maps",
    ("vm_area_struct", "vm_pgoff"): "awk '{print $3}' /proc/<pid>/maps",
    ("vm_area_struct", "vm_file"): "awk '{print $6}' /proc/<pid>/maps",
    # sock: one field per entry in the skmem tuple of ss -tanm.
    ("sock", "sk_rcvbuf"): "ss -tanmH | grep -o 'skmem:([^)]*)' | cut -d, -f2",
    ("sock", "sk_sndbuf"): "ss -tanmH | grep -o 'skmem:([^)]*)' | cut -d, -f4",
    ("sock", "sk_forward_alloc"): "ss -tanmH | grep -o 'skmem:([^)]*)' | cut -d, -f5",
    ("sock", "sk_wmem_queued"): "ss -tanmH | grep -o 'skmem:([^)]*)' | cut -d, -f6",
    ("sock", "sk_omem_alloc"): "ss -tanmH | grep -o 'skmem:([^)]*)' | cut -d, -f7",
    ("sock", "sk_backlog"): "ss -tanmH | grep -o 'skmem:([^)]*)' | cut -d, -f8",
    ("sock", "sk_drops"): "ss -tanmH | grep -o 'skmem:([^)]*)' | cut -d, -f9 | tr -d ')'",
    ("sock", "sk_receive_queue"): "ss -tanH | awk '{print $2}'  # Recv-Q",
    ("sock", "sk_write_queue"): "ss -tanH | awk '{print $3}'  # Send-Q",
    ("sock", "sk_ack_backlog"): "ss -tlnH | awk '{print $2}'",
    ("sock", "sk_max_ack_backlog"): "ss -tlnH | awk '{print $3}'",
    ("sock", "sk_ino"): "ss -taneH | grep -o 'ino:[0-9]*'",
    ("sock", "sk_uid"): "ss -taneH | grep -o 'uid:[0-9]*'",
    ("sock", "sk_err"): "ss -tani",
    ("sock_common", "skc_state"): "ss -tanH | awk '{print $1}'",
    ("sock_common", "skc_num"): "ss -tanH | awk '{print $4}'  # address:port",
    # file
    ("file", "f_pos"): "cat /proc/<pid>/fdinfo/<n>",
    ("file", "f_flags"): "cat /proc/<pid>/fdinfo/<n>",
    ("file", "f_inode"): "stat -L -c %i /proc/<pid>/fd/<n>",
    # net_device: sysfs publishes one file per field.
    ("net_device", "name"): "ip link",
    ("net_device", "ifindex"): "cat /sys/class/net/<name>/ifindex",
    ("net_device", "mtu"): "cat /sys/class/net/<name>/mtu",
    ("net_device", "flags"): "cat /sys/class/net/<name>/flags",
    ("net_device", "operstate"): "cat /sys/class/net/<name>/operstate",
    ("net_device", "type"): "cat /sys/class/net/<name>/type",
    ("net_device", "carrier_up_count"): "cat /sys/class/net/<name>/carrier_up_count",
    ("net_device", "carrier_down_count"): "cat /sys/class/net/<name>/carrier_down_count",
    ("net_device", "dev_addr"): "cat /sys/class/net/<name>/address",
    ("net_device", "perm_addr"): "cat /sys/class/net/<name>/address",
    ("net_device", "tx_queue_len"): "cat /sys/class/net/<name>/tx_queue_len",
    ("net_device", "num_tx_queues"): "ls /sys/class/net/<name>/queues",
    ("net_device", "real_num_tx_queues"): "ls /sys/class/net/<name>/queues",
    ("net_device", "stats"): "ip -s link show <name>",
    ("net_device", "qdisc"): "tc qdisc show dev <name>",
    # slab and runqueue
    ("kmem_cache", "name"): "awk 'NR>2{print $1}' /proc/slabinfo",
    ("kmem_cache", "object_size"): "awk 'NR>2{print $1, $4}' /proc/slabinfo",
    ("rq", "nr_running"): "vmstat -n 1 | awk 'NR>2{print $1}'  # all CPUs together",
    ("rq", "nr_iowait"): "vmstat -n 1 | awk 'NR>2{print $2}'  # all CPUs together",
    ("rq", "nr_switches"): "grep ^ctxt /proc/stat  # all CPUs, cumulative",
}


# One place decides what a placeholder can be filled from. Every command in
# this file, in ``links.py`` and in the kernel-side trace goes through it, so a
# struct
# either supplies a value for every command it shows or for none of them.
#
# Each rule reads the answer out of the kernel rather than out of the path the
# user took to get here: an mm_struct names its owner, and a struct file, which
# records no holder at all, is found by the task holding it. What no rule can
# answer keeps its placeholder, because a command with <pid> still in it is
# correct and one with a made-up pid is not.


def _holder_of(obj):
    """The task holding this file, and at which descriptor.

    struct file has no back-pointer to a task: several tasks can hold the same
    file, and a file in flight over a unix socket is held by none. The index
    that does know is the fd table, so this searches them. Measured at about
    2000 descriptors across the machine, which is a hundredth of a second.
    """
    from drgn.helpers.linux.fs import for_each_file
    from drgn.helpers.linux.pid import for_each_task

    address = obj.value_()
    for task in for_each_task(obj.prog_):
        try:
            for fd, held in for_each_file(task):
                if held.value_() == address:
                    return task.pid.value_(), fd
        except Exception:  # noqa: BLE001, S112 - a task exiting mid-walk is normal
            continue
    return None, None


def placeholders(obj, tag: str) -> dict[str, str]:
    """What this struct can fill into a command shown against it."""
    found: dict[str, str] = {}
    try:
        if tag == "task_struct":
            found["<pid>"] = str(obj.pid.value_())
        elif tag == "mm_struct":
            if obj.owner:
                found["<pid>"] = str(obj.owner.pid.value_())
        elif tag == "vm_area_struct":
            if obj.vm_mm.owner:
                found["<pid>"] = str(obj.vm_mm.owner.pid.value_())
        elif tag == "signal_struct":
            if obj.curr_target:
                found["<pid>"] = str(obj.curr_target.pid.value_())
        elif tag == "net_device":
            found["<name>"] = obj.name.string_().decode()
        elif tag == "file":
            pid, fd = _holder_of(obj)
            if pid is not None:
                found["<pid>"], found["<n>"] = str(pid), str(fd)
        elif tag == "socket" and obj.file:
            pid, fd = _holder_of(obj.file)
            if pid is not None:
                found["<pid>"], found["<n>"] = str(pid), str(fd)
        elif tag == "sock" and obj.sk_socket and obj.sk_socket.file:
            pid, fd = _holder_of(obj.sk_socket.file)
            if pid is not None:
                found["<pid>"], found["<n>"] = str(pid), str(fd)
    except Exception:  # noqa: BLE001 - an unreadable owner leaves the placeholder
        return found
    return found


# What to say when a placeholder cannot be filled. An unfilled one is not a
# command waiting to be completed: if no task owns this object, no /proc
# directory publishes it, and there is nothing to run anywhere.
UNFILLED = {
    "<pid>": "no owning task, so no /proc path",
    "<n>": "no owning task, so no /proc path",
    "<name>": "no interface name",
}

_PLACEHOLDER = re.compile(r"<\w+>")


def fill(command: str, found: dict[str, str]) -> str:
    """Substitute what the struct knows, and say why where it knows nothing."""
    for placeholder, value in found.items():
        command = command.replace(placeholder, value)
    left = _PLACEHOLDER.search(command)
    if left:
        return UNFILLED.get(left.group(), f"{left.group()} is not known here")
    return command


def runnable(command: str) -> str:
    """The part of a displayed command a shell can actually take.

    A cell may offer alternatives separated by ", or " and may carry a trailing
    "# …" note. Both are for the reader: the alternatives are equivalents, not
    one pipeline, and the note is prose. Only the first alternative, without the
    note, is a command.
    """
    return command.split(", or ")[0].split("  #")[0].strip()


def field_command(tag: str, field: str, found: dict[str, str] | None = None) -> str:
    """The userspace equivalent for one struct field, if there is one."""
    return fill(FIELD_COMMANDS.get((tag, field), ""), found or {})


def entry_command(subsystem_key: str, entry_key: str) -> str:
    """The userspace command for an entry, or a statement that there is none."""
    command = ENTRY_COMMANDS.get((subsystem_key, entry_key))
    if command:
        return command
    if command == "":
        return "no userspace equivalent"
    return ""

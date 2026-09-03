"""Userspace equivalents for the subsystem entry points.

The link table in ``links.py`` covers edges out of a struct. This covers the
entries themselves: the lists you start from. Same purpose -- carry the mental
model to a box that has none of this installed.

An empty string means there is genuinely no userspace equivalent, which is
worth stating rather than leaving blank: it marks what only a debugger reaches.
"""

from __future__ import annotations

# Keyed by (subsystem key, entry key).
ENTRY_COMMANDS: dict[tuple[str, str], str] = {
    # process
    ("process", "processes"): "ps -e",
    ("process", "tasks"): "ps -eL",
    ("process", "kthreads"): "ps -eo pid,comm --ppid 2, or ps -e | grep '\\['",
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
    ("page", "vmemmap"): "/proc/kpageflags and /proc/kpagecount (needs a reader)",
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
    ("net", "init_net"): "ip netns identify",
    ("net", "namespaces"): "ip netns list",
    ("net", "netdevs"): "ip -d link",
    ("net", "txqueues"): "ls /sys/class/net/*/queues, or tc -s qdisc",
    # skb
    ("skb", "nonempty"): "ss -tanm shows per-socket queue bytes",
    ("skb", "receive"): "ss -tanm (Recv-Q column)",
    ("skb", "write"): "ss -tanm (Send-Q column)",
    ("skb", "backlog"): "cat /proc/net/softnet_stat",
    ("skb", "qdisc"): "tc -s qdisc show",
    # slab
    ("slab", "caches"): "slabtop, or cat /proc/slabinfo",
    # device
    ("device", "devices"): "ls /sys/devices, or lsblk / lspci / ip link per bus",
    ("device", "bound"): "ls -l /sys/bus/*/drivers/*/",
    ("device", "buses"): "ls /sys/bus",
    ("device", "classes"): "ls /sys/class",
    ("device", "pci"): "lspci -v",
    ("device", "disks"): "lsblk -d",
    ("device", "partitions"): "lsblk",
    # system
    ("system", "overview"): "uname -a; uptime; nproc; free -h",
    ("system", "scheduler"): "sysctl kernel.sched_; cat /sys/kernel/debug/sched/features",
    ("system", "memory"): "cat /proc/meminfo; cat /proc/buddyinfo",
}


# Individual fields that userspace exposes. Most do not: the kernel keeps far
# more state than it publishes, and that asymmetry is worth seeing. Only fields
# with a genuine equivalent are listed.
FIELD_COMMANDS: dict[tuple[str, str], str] = {
    ("task_struct", "comm"): "cat /proc/<pid>/comm",
    ("task_struct", "__state"): "ps -o stat= -p <pid>",
    ("task_struct", "prio"): "ps -o pri= -p <pid>",
    ("task_struct", "static_prio"): "ps -o ni= -p <pid> (nice)",
    ("task_struct", "flags"): "grep -i flags /proc/<pid>/status is not the same",
    ("task_struct", "utime"): "ps -o times= -p <pid>, or field 14 of /proc/<pid>/stat",
    ("task_struct", "stime"): "field 15 of /proc/<pid>/stat",
    ("task_struct", "start_time"): "ps -o lstart= -p <pid>",
    ("task_struct", "nvcsw"): "grep voluntary_ctxt_switches /proc/<pid>/status",
    ("task_struct", "nivcsw"): "grep nonvoluntary_ctxt_switches /proc/<pid>/status",
    ("task_struct", "exit_code"): "the exit status the parent reads from wait()",
    ("mm_struct", "total_vm"): "grep VmSize /proc/<pid>/status",
    ("mm_struct", "map_count"): "wc -l < /proc/<pid>/maps",
    ("mm_struct", "start_brk"): "the [heap] line of /proc/<pid>/maps",
    ("mm_struct", "arg_start"): "cat /proc/<pid>/cmdline",
    ("mm_struct", "env_start"): "cat /proc/<pid>/environ",
    ("vm_area_struct", "vm_start"): "the first column of /proc/<pid>/maps",
    ("vm_area_struct", "vm_flags"): "the permission column of /proc/<pid>/maps",
    ("sock", "sk_rcvbuf"): "ss -tanm, or sysctl net.core.rmem_default",
    ("sock", "sk_sndbuf"): "ss -tanm, or sysctl net.core.wmem_default",
    ("sock", "sk_receive_queue"): "the Recv-Q column of ss -tan",
    ("sock", "sk_write_queue"): "the Send-Q column of ss -tan",
    ("sock", "sk_drops"): "ss -tani shows drops per socket",
    ("sock", "sk_err"): "ss -tani",
    ("sock_common", "skc_state"): "the State column of ss -tan",
    ("sock_common", "skc_num"): "the local port in ss -tan",
    ("file", "f_pos"): "cat /proc/<pid>/fdinfo/<n>",
    ("file", "f_flags"): "cat /proc/<pid>/fdinfo/<n>",
    ("net_device", "name"): "ip link",
    ("net_device", "mtu"): "ip link show <name>",
    ("net_device", "flags"): "ip link show <name>",
    ("kmem_cache", "name"): "cat /proc/slabinfo",
    ("kmem_cache", "object_size"): "the objsize column of /proc/slabinfo",
    ("rq", "nr_running"): "the run-queue column of vmstat 1",
    ("rq", "nr_switches"): "the cs column of vmstat 1",
}


def field_command(tag: str, field: str, pid: int | None = None) -> str:
    """The userspace equivalent for one struct field, if there is one."""
    command = FIELD_COMMANDS.get((tag, field), "")
    if command and pid is not None:
        command = command.replace("<pid>", str(pid))
    return command


def entry_command(subsystem_key: str, entry_key: str) -> str:
    """The userspace command for an entry, or a statement that there is none."""
    command = ENTRY_COMMANDS.get((subsystem_key, entry_key))
    if command:
        return command
    if command == "":
        return "no userspace equivalent"
    return ""

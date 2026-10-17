"""Workloads: programs the explorer starts so that kernel state has something to show.

An experiment, written to be removable. Its code is in this module,
``view/workloads.py`` and ``tests/test_workloads.py``; the rest of the explorer
refers to it in marked lines in ``view/frames.py`` and ``tui/app.py``.

Each workload is one mode of ``tests/helpers/traffic.py``, run as a child of
the explorer. A child shares the explorer's network namespace, and the sockets
it opens are in the tables the socket entries read. In a container that
namespace is not init_net (see ``view/workloads.py``). A child is stopped with
SIGTERM when the user stops it, when the explorer exits, and, through
PR_SET_PDEATHSIG, when the explorer is killed.
"""

from __future__ import annotations

import atexit
import ctypes
import signal
import subprocess
import sys
import threading
from collections import deque
from dataclasses import dataclass
from pathlib import Path

TRAFFIC = Path(__file__).resolve().parents[2] / "tests" / "helpers" / "traffic.py"
# An upper bound on how long a child runs if nothing stops it.
SECONDS = 3600
# Lines of a child's output kept for its pane.
OUTPUT_LINES = 20
PR_SET_PDEATHSIG = 1


@dataclass(frozen=True)
class Workload:
    key: str
    # The sidebar branches containing it, outermost first.
    group: tuple[str, ...]
    label: str
    doc: str
    # The catalog entries that show the effect: (subsystem key, entry key,
    # what to look for there).
    look: tuple[tuple[str, str, str], ...]


WORKLOADS = (
    Workload(
        "established", ("net", "TCP"), "idle connections",
        "A socket listening on 127.0.0.1:40100 and 4 ESTABLISHED connections to "
        "it, idle after the handshake: bytes_sent and bytes_received are 0.",
        (("socket", "tcp_listen", "the TCP_LISTEN socket on 127.0.0.1:40100"),
         ("socket", "tcp_estab", "8 sockets: the client and the accepted socket "
                                 "of each connection")),
    ),
    Workload(
        "stream", ("net", "TCP"), "paced transfer",
        "A connection to 127.0.0.1:40200. The client writes 64 KB every 10 ms "
        "and the server reads continuously.",
        (("socket", "tcp_estab", "client socket (→ 127.0.0.1:40200), as tcp_sock: "
                                 "bytes_acked increases on each refresh (r)"),),
    ),
    Workload(
        "stall", ("net", "TCP"), "zero receive window",
        "A connection to 127.0.0.1:40300 whose server process sleeps after "
        "accept(). Data accumulates on the server socket's sk_receive_queue "
        "until it advertises a zero window; the client's data beyond that "
        "window is queued on sk_write_queue.",
        (("skb", "receive", "server socket: skbs from stream offset 0"),
         ("skb", "write", "client socket: unsent skbs, from the offset where "
                          "the server's receive queue ends"),
         ("socket", "tcp_estab", "client socket (→ 127.0.0.1:40300), as tcp_sock: "
                                 "snd_wnd 0")),
    ),
    Workload(
        "churn", ("net", "TCP"), "TIME_WAIT sockets",
        "5 connections a second to 127.0.0.1:40400, each closed by the client "
        "first, which puts the client end in TIME_WAIT for 60 s.",
        (("socket", "tcp_estab", "TIME_WAIT entries (→ 127.0.0.1:40400): "
                                 "struct inet_timewait_sock, reachable only through the ehash"),),
    ),
    Workload(
        "backlog", ("net", "TCP"), "full accept queue",
        "A socket listening on 127.0.0.1:40500 with backlog 2, whose process "
        "sleeps after listen(), and 6 clients calling connect().",
        (("socket", "tcp_listen", "the listening socket: sk_ack_backlog 3, "
                                  "sk_max_ack_backlog 2"),
         ("socket", "tcp_estab", "client sockets (→ 127.0.0.1:40500): 3 ESTABLISHED, "
                                 "3 SYN_SENT whose SYNs the listener drops")),
    ),
    Workload(
        "nagle", ("net", "TCP"), "Nagle on and off",
        "Two connections. The client writes each request with two 8-byte send() "
        "calls, then waits for the reply: :40600 with Nagle enabled, :40601 with "
        "TCP_NODELAY. With Nagle enabled, the second segment is sent when the "
        "server's delayed ACK for the first arrives.",
        (("socket", "tcp_estab", "client sockets (→ 127.0.0.1:40600, :40601), as "
                                 "tcp_sock: nonagle 0 and 1, srtt_us"),),
    ),
    Workload(
        "udp", ("net", "UDP"), "unread datagrams",
        "A 1 KB datagram to 127.0.0.1:40700 every 1 ms to a socket whose process "
        "sleeps after bind(). Once sk_rmem_alloc reaches sk_rcvbuf, further "
        "datagrams are dropped. On a kernel whose struct sock has "
        "sk_drop_counters, UDP counts these drops there and sk_drops reads 0.",
        (("socket", "udp", "port 40700, as udp_sock: drop_counters.drops0 "
                           "increases on each refresh (r)"),
         ("skb", "receive", "port 40700: queued datagrams, payload kexplore-udp N")),
    ),
    Workload(
        "delay", ("net", "qdisc"), "netem delay on lo",
        "Installs a netem root qdisc on lo with 1 s latency and opens a TCP_NODELAY "
        "connection to 127.0.0.1:40800 whose client writes 1 KB every 100 ms. "
        "Every packet sent through lo is delayed while it runs, including queries "
        "to a local DNS resolver. Stopping it deletes the qdisc, and lo reverts "
        "to noqueue.",
        (("skb", "qdisc", "packets in netem's tfifo: rows lo tx0 netem #n"),
         ("skb", "retransmit", "client socket: unacknowledged skbs, payload "
                               "kexplore-delay"),
         ("socket", "tcp_estab", "client socket (→ 127.0.0.1:40800), as tcp_sock: "
                                 "srtt_us about 16000000 (2 s RTT, stored times 8)")),
    ),
    Workload(
        "unix", ("net", "AF_UNIX"), "unread messages",
        "A socketpair() of each type, SOCK_STREAM and SOCK_DGRAM, with 3 messages "
        "queued on the receiving socket of each.",
        (("socket", "unix", "the 4 sockets of the two pairs"),
         ("skb", "nonempty", "the receiving socket of each pair: "
                             "sk_receive_queue qlen 3")),
    ),
)


class _Child:
    def __init__(self, workload: Workload) -> None:
        self.output: deque[str] = deque(maxlen=OUTPUT_LINES)
        # Set by stop(). A child that exits on request reads as stopped
        # whatever its exit status: traffic.py exits 0 on SIGTERM.
        self.stopped = False
        self.process = subprocess.Popen(
            [sys.executable, "-u", str(TRAFFIC), workload.key, "--seconds", str(SECONDS)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            # A new session and process group, outside the explorer's terminal
            # job control: stopping it is up to stop() and PR_SET_PDEATHSIG.
            start_new_session=True,
            preexec_fn=_die_with_parent,
        )
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        for line in self.process.stdout:
            self.output.append(line.rstrip("\n"))


def _die_with_parent() -> None:
    """In the child: receive SIGTERM when the explorer exits, however it exits."""
    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl(PR_SET_PDEATHSIG, signal.SIGTERM)


_children: dict[str, _Child] = {}


def running(workload: Workload) -> bool:
    child = _children.get(workload.key)
    return child is not None and child.process.poll() is None


def start(workload: Workload) -> None:
    if not running(workload):
        _children[workload.key] = _Child(workload)


def stop(workload: Workload) -> None:
    child = _children.get(workload.key)
    if child is None or child.process.poll() is not None:
        return
    child.stopped = True
    child.process.terminate()
    try:
        child.process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        child.process.kill()
        child.process.wait()


def toggle(workload: Workload) -> None:
    if running(workload):
        stop(workload)
    else:
        start(workload)


def title(workload: Workload) -> str:
    """The label with its innermost group, for a breadcrumb: "TCP: idle connections"."""
    return f"{workload.group[-1]}: {workload.label}" if workload.group else workload.label


def status(workload: Workload) -> str:
    child = _children.get(workload.key)
    if child is None:
        return "stopped"
    code = child.process.poll()
    if code is None:
        return f"running, pid {child.process.pid}"
    if child.stopped:
        return "stopped"
    return f"exited with status {code}"


def output(workload: Workload) -> list[str]:
    child = _children.get(workload.key)
    return list(child.output) if child is not None else []


@atexit.register
def stop_all() -> None:
    for workload in WORKLOADS:
        stop(workload)

"""Generate loopback traffic of several kinds for the socket and skb entries to show.

Each mode sets up one condition and maintains it until --seconds elapses:

  established  a socket listening on :40100 and 4 ESTABLISHED connections
               to it, idle after the handshake
  stream       a connection to :40200; the client writes 64 KB every 10 ms
               and the server reads continuously
  stall        a connection to :40300 whose server process sleeps after
               accept(): data accumulates on the server socket's
               sk_receive_queue until it advertises a zero window; the
               client's data beyond that window is queued on sk_write_queue
  churn        connections to :40400, closed by the client first, which puts
               each client end in TIME_WAIT for 60 s
  backlog      a socket listening on :40500 with backlog 2, whose process
               sleeps after listen(), and 6 clients calling connect(): 3
               complete the handshake; while the accept queue is full the
               listener drops the SYNs of the other 3 (ListenOverflows in
               /proc/net/netstat), which retransmit them from SYN_SENT
  nagle        two connections whose client writes each request with two
               8-byte send() calls: :40600 with Nagle enabled, :40601 with
               TCP_NODELAY; compare nonagle and srtt_us in their tcp_sock
  udp          a 1 KB datagram to :40700 every 1 ms to a socket whose process
               sleeps after bind(); once sk_rmem_alloc reaches sk_rcvbuf,
               further datagrams are dropped
  unix         a socketpair() of each type, SOCK_STREAM and SOCK_DGRAM, with
               3 messages queued on the receiving socket of each
  delay        a netem root qdisc on lo with 1 s latency, and a TCP_NODELAY
               connection to :40800 whose client writes 1 KB every 100 ms:
               packets wait in netem's tfifo, and each sent skb is on
               tcp_rtx_queue until its ACK arrives, one 2 s RTT later. Needs
               CAP_NET_ADMIN. Every packet sent through lo is delayed while
               it runs, including queries to a local DNS resolver. It deletes the qdisc on exit
               and on SIGTERM; after a SIGKILL, `tc qdisc del dev lo root`
               deletes it where iproute2's tc is installed. `all` excludes it.

Usage:

    python3 tests/helpers/traffic.py MODE [MODE...] [--seconds N]
    python3 tests/helpers/traffic.py all
    sudo python3 tests/helpers/traffic.py delay

Run it detached, or it exits with the shell that started it:

    setsid nohup python3 tests/helpers/traffic.py nagle --seconds 1800 \
        >/tmp/traffic.log 2>&1 </dev/null &

Every socket is on 127.0.0.1; the ports are printed at start.
"""

from __future__ import annotations

import argparse
import os
import signal
import socket
import struct
import sys
import threading
import time

HOST = "127.0.0.1"
PORTS = {
    "established": 40100,
    "stream": 40200,
    "stall": 40300,
    "churn": 40400,
    "backlog": 40500,
    "nagle": 40600,  # Nagle on; TCP_NODELAY is on 40601
    "udp": 40700,
    "delay": 40800,
}

# rtnetlink, for the delay mode. From include/uapi/linux/netlink.h,
# rtnetlink.h and pkt_sched.h; these are stable ABI.
RTM_NEWQDISC, RTM_DELQDISC = 36, 37
NLMSG_ERROR = 2
NLM_F_REQUEST, NLM_F_ACK, NLM_F_EXCL, NLM_F_CREATE = 0x1, 0x4, 0x200, 0x400
TC_H_ROOT = 0xFFFFFFFF
TCA_KIND, TCA_OPTIONS = 1, 2
# netem reads tc_netem_qopt.latency in psched ticks: PSCHED_TICKS2NS(x) is
# x << PSCHED_SHIFT, and PSCHED_SHIFT is 6.
PSCHED_SHIFT = 6
DELAY_SECONDS = 1.0
NETEM_LIMIT = 1000  # packets, the default tc uses
# Callables run on exit, in reverse order, including on SIGTERM.
cleanups: list = []


def listener(port: int, backlog: int = 16) -> socket.socket:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((HOST, port))
    server.listen(backlog)
    return server


def connected_pair(port: int) -> tuple[socket.socket, socket.socket, socket.socket]:
    """Listener, client end, and accepted server end of one connection."""
    server = listener(port)
    client = socket.create_connection((HOST, port))
    accepted, _ = server.accept()
    return server, client, accepted


def payload(mode: str, size: int, start: int = 0) -> bytes:
    """Bytes ``start`` to ``start + size`` of a stream of 32-byte text lines.

    Each line names the mode and its own offset in the stream. An skb's
    payload then identifies its workload and its position in the stream.
    """
    first = start - start % 32
    lines = b"".join(b"kexplore-%-6s %015d\n" % (mode.encode(), offset)
                     for offset in range(first, start + size, 32))
    return lines[start - first:start - first + size]


def background(target, *args) -> None:
    threading.Thread(target=target, args=args, daemon=True).start()


def established(keep: list) -> str:
    server = listener(PORTS["established"])
    keep.append(server)
    for _ in range(4):
        keep.append(socket.create_connection((HOST, PORTS["established"])))
        keep.append(server.accept()[0])
    return f"4 idle connections to :{PORTS['established']}"


def stream(keep: list) -> str:
    sockets = connected_pair(PORTS["stream"])
    keep.extend(sockets)
    _, client, accepted = sockets
    def send():
        offset = 0
        while True:
            client.sendall(payload("stream", 65536, offset))
            offset += 65536
            time.sleep(0.01)

    def drain():
        while accepted.recv(65536):
            pass

    background(send)
    background(drain)
    return f"64 KB every 10 ms to :{PORTS['stream']}"


def stall(keep: list) -> str:
    sockets = connected_pair(PORTS["stall"])
    keep.extend(sockets)
    _, client, _ = sockets
    client.setblocking(False)
    sent = 0
    try:
        while True:
            sent += client.send(payload("stall", 65536, sent))
    except BlockingIOError:
        pass
    return f"{sent} bytes queued to :{PORTS['stall']}; the server sleeps"


def churn(keep: list, rate: float) -> str:
    server = listener(PORTS["churn"], backlog=128)
    keep.append(server)

    def serve():
        while True:
            conn, _ = server.accept()
            while conn.recv(4096):
                pass
            conn.close()

    def connect():
        while True:
            with socket.create_connection((HOST, PORTS["churn"])) as client:
                client.sendall(b"hello")
            time.sleep(1 / rate)

    background(serve)
    background(connect)
    return f"{rate:g} connections/s to :{PORTS['churn']}, each leaving a TIME_WAIT socket"


def backlog(keep: list) -> str:
    server = listener(PORTS["backlog"], backlog=2)
    keep.append(server)
    for _ in range(6):
        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        client.setblocking(False)
        client.connect_ex((HOST, PORTS["backlog"]))
        keep.append(client)
    return f"6 clients connecting to :{PORTS['backlog']} (backlog 2); the listener sleeps"


def nagle(keep: list) -> str:
    """Request/response, each request written as two 8-byte send() calls.

    The server replies once it has both halves and delays its ACK to
    piggyback it on the reply. With Nagle enabled, tcp_sendmsg() queues the
    second send() until that ACK arrives, and each request takes one
    delayed-ACK timeout; with TCP_NODELAY both segments are sent immediately.
    """
    counts = {}

    def client_loop(client: socket.socket, port: int):
        while True:
            client.send(b"header--")
            client.send(b"body----")
            client.recv(8)
            counts[port] += 1
            time.sleep(0.01)

    def server_loop(conn: socket.socket):
        while True:
            request = b""
            while len(request) < 16:
                request += conn.recv(16 - len(request))
            conn.send(b"response")

    def report():
        while True:
            time.sleep(5)
            print(f"nagle: requests so far, Nagle on {counts[PORTS['nagle']]}, "
                  f"TCP_NODELAY {counts[PORTS['nagle'] + 1]}", flush=True)

    for port, nodelay in ((PORTS["nagle"], 0), (PORTS["nagle"] + 1, 1)):
        sockets = connected_pair(port)
        keep.extend(sockets)
        _, client, accepted = sockets
        client.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, nodelay)
        counts[port] = 0
        background(client_loop, client, port)
        background(server_loop, accepted)
    background(report)
    return (
        f"request/response in two writes: Nagle on to :{PORTS['nagle']}, "
        f"TCP_NODELAY to :{PORTS['nagle'] + 1}"
    )


def udp(keep: list) -> str:
    receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    receiver.bind((HOST, PORTS["udp"]))
    sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    keep.extend([receiver, sender])

    def send():
        number = 0
        while True:
            datagram = (b"kexplore-udp %06d " % number).ljust(1024, b".")
            sender.sendto(datagram, (HOST, PORTS["udp"]))
            number += 1
            time.sleep(0.001)

    background(send)
    return f"1 KB datagrams to :{PORTS['udp']} every 1 ms; the receiver sleeps"


def unix(keep: list) -> str:
    for kind in (socket.SOCK_STREAM, socket.SOCK_DGRAM):
        left, right = socket.socketpair(socket.AF_UNIX, kind)
        keep.extend([left, right])
        for index in range(3):
            left.send(b"kexplore-unix-%d" % index)
    return "AF_UNIX stream and datagram pairs, 3 unread messages each"


def _rtattr(kind: int, data: bytes) -> bytes:
    length = 4 + len(data)
    return struct.pack("=HH", length, kind) + data + b"\0" * (-length % 4)


def _qdisc_request(message: int, flags: int, device: str, attributes: bytes = b"") -> None:
    """Send one RTM_*QDISC request for the root qdisc of ``device`` and check the ack."""
    tcmsg = struct.pack("=BBHiIII", socket.AF_UNSPEC, 0, 0,
                        socket.if_nametoindex(device), 0, TC_H_ROOT, 0)
    body = tcmsg + attributes
    header = struct.pack("=IHHII", 16 + len(body), message, flags | NLM_F_REQUEST | NLM_F_ACK, 1, 0)
    with socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, socket.NETLINK_ROUTE) as link:
        link.send(header + body)
        reply = link.recv(65536)
    if struct.unpack_from("=H", reply, 4)[0] == NLMSG_ERROR:
        error = struct.unpack_from("=i", reply, 16)[0]
        if error:
            raise OSError(-error, os.strerror(-error))


def add_netem(device: str, seconds: float) -> None:
    """``tc qdisc add dev DEVICE root netem delay SECONDS``.

    With NLM_F_EXCL the request fails with EEXIST when the device has a
    configured root qdisc. The kernel replaces only a built-in default, such
    as lo's noqueue (handle 0).
    """
    qopt = struct.pack("=6I", int(seconds * 1e9) >> PSCHED_SHIFT, NETEM_LIMIT, 0, 0, 0, 0)
    _qdisc_request(RTM_NEWQDISC, NLM_F_CREATE | NLM_F_EXCL, device,
                   _rtattr(TCA_KIND, b"netem\0") + _rtattr(TCA_OPTIONS, qopt))


def delete_root_qdisc(device: str) -> None:
    """``tc qdisc del dev DEVICE root``: the device returns to its default."""
    _qdisc_request(RTM_DELQDISC, 0, device)


def delay(keep: list) -> str:
    try:
        add_netem("lo", DELAY_SECONDS)
    except PermissionError:
        sys.exit("delay: adding a qdisc needs root (CAP_NET_ADMIN)")
    except FileExistsError:
        sys.exit("delay: lo has a configured root qdisc (EEXIST); delete it to use this mode")
    cleanups.append(lambda: delete_root_qdisc("lo"))

    sockets = connected_pair(PORTS["delay"])
    keep.extend(sockets)
    _, client, accepted = sockets
    # TCP_NODELAY sends each write as its own segment, one skb each. With
    # Nagle enabled, the writes of a 2 s round trip merge into one skb.
    client.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    def send():
        offset = 0
        while True:
            client.sendall(payload("delay", 1024, offset))
            offset += 1024
            time.sleep(0.1)

    def drain():
        while accepted.recv(65536):
            pass

    background(send)
    background(drain)
    return (f"netem on lo delays every packet {DELAY_SECONDS:g} s; "
            f"1 KB every 100 ms to :{PORTS['delay']}")


MODES = ["established", "stream", "stall", "churn", "backlog", "nagle", "udp", "unix"]
# Changes the machine: runs only when named.
EXPLICIT = ["delay"]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("modes", nargs="+", choices=MODES + EXPLICIT + ["all"], metavar="MODE")
    parser.add_argument("--seconds", type=int, default=300)
    parser.add_argument("--churn-rate", type=float, default=5, help="connections per second")
    args = parser.parse_args()

    modes = MODES if "all" in args.modes else args.modes
    # The explorer stops a workload with SIGTERM. The handler raises
    # SystemExit, and the finally below runs the cleanups.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    keep: list = []
    try:
        for mode in modes:
            if mode == "churn":
                print(f"{mode}: {churn(keep, args.churn_rate)}", flush=True)
            else:
                print(f"{mode}: {globals()[mode](keep)}", flush=True)
        print(f"holding for {args.seconds}s", flush=True)
        time.sleep(args.seconds)
    finally:
        for cleanup in reversed(cleanups):
            cleanup()


if __name__ == "__main__":
    main()

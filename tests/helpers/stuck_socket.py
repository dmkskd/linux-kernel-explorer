"""Park unread packets in a socket receive queue, so there are skbs to look at.

An idle system has essentially no queued skbs -- they're consumed as fast as
they arrive. This binds a UDP socket, sends to it, and never calls recv(), so
the skbs stay in sk_receive_queue for as long as this runs.

Run it detached, or it dies with the shell that started it:

    setsid nohup python3 tests/helpers/stuck_socket.py 1800 >/tmp/stuck.log 2>&1 </dev/null &

then browse skb › socket receive queues.
"""

from __future__ import annotations

import socket
import sys
import time

PORT = 39999
SECONDS = int(sys.argv[1]) if len(sys.argv) > 1 else 300

listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
listener.bind(("127.0.0.1", PORT))

sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
for index in range(3):
    sender.sendto(b"kexplore-test-packet-%d" % index, ("127.0.0.1", PORT))

print(f"3 packets queued on 127.0.0.1:{PORT}, holding for {SECONDS}s (never recv)")
time.sleep(SECONDS)

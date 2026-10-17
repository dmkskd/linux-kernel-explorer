"""Start a workload from the workloads tab, see its effect, and stop it.

Every workload's entry rows are checked to resolve to a catalog entry. Opening
the nagle workload must not start it; its start row does, and its two client
sockets are then read from the kernel: one has nonagle 0, the other 1. The
same row, now stop, must end the child.

The delay workload is started and its held skbs are looked for in the qdisc
and retransmit entries; stopping it must give lo back its noqueue qdisc.
"""

from __future__ import annotations

import asyncio
import socket
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import drgn
from drgn import cast
from drgn.helpers.linux.net import sk_nulls_for_each
from textual.widgets import DataTable, Tabs, Tree

from harness import settle, tree_nodes
from drgn.helpers.linux.net import netdev_get_by_name

from kexplore.catalog import skb
from kexplore.catalog.links import _skb_payload
from kexplore.operations import workloads
from kexplore.tui.app import Explorer
from kexplore.view import workloads as view

ok = True


def check(condition: bool, message: str) -> None:
    global ok
    ok &= bool(condition)
    print(("  ok   " if condition else "  FAIL ") + message)


def nonagle_by_port(prog) -> dict[int, int]:
    """nonagle of each established client socket whose remote port is a nagle port."""
    hashinfo = prog["init_net"].ipv4.tcp_death_row.hashinfo
    found = {}
    for index in range(hashinfo.ehash_mask.value_() + 1):
        for sk in sk_nulls_for_each(hashinfo.ehash[index].chain):
            common = sk.__sk_common
            port = socket.ntohs(common.skc_dport.value_())
            if port in (40600, 40601) and common.skc_state.value_() == 1:
                found[port] = cast("struct tcp_sock *", sk).nonagle.value_()
    return found


def lo_qdisc(prog) -> str:
    lo = netdev_get_by_name(prog["init_net"].address_of_(), "lo")
    return lo._tx[0].qdisc.ops.id.string_().decode()


async def check_delay(prog) -> None:
    before = lo_qdisc(prog)
    if before != "noqueue":
        print(f"  FAIL lo's root qdisc is {before}. This check adds a netem root qdisc "
              "to lo, and RTM_NEWQDISC fails with EEXIST while one is configured. "
              "Stop the delay workload, or delete that qdisc, and run this test again.")
        global ok
        ok = False
        return
    delay = next(w for w in workloads.WORKLOADS if w.key == "delay")
    workloads.start(delay)
    # The handshake alone takes one 2 s round trip, then a round trip of data.
    await asyncio.sleep(5)
    netem = [label for label, _ in skb.qdisc_queues(prog) if " netem #" in label]
    held = [(label, s) for label, s in skb.retransmit_queues(prog)
            if f"[{workloads._children['delay'].process.pid}]" in label]
    check(lo_qdisc(prog) == "netem" and bool(netem), f"qdisc entry lists {len(netem)} skbs netem holds")
    first = _skb_payload(held[0][1]) if held else "(none)"
    check(len(held) > 1 and "kexplore-delay" in first,
          f"retransmit entry lists {len(held)} of its skbs, first: {first}")
    workloads.stop(delay)
    check(lo_qdisc(prog) == "noqueue", f"stopping it restores lo: {lo_qdisc(prog)}")


def port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        return probe.connect_ex(("127.0.0.1", port)) == 0


async def press_action(app, pilot, name: str) -> None:
    """Enter on the pane's start/stop row, which must be named ``name`` and marked ``▸``."""
    rows = app.stack[-1].rows
    index = next((i for i, r in enumerate(rows) if view.action_of(r) is not None), None)
    action = rows[index] if index is not None else None
    check(action is not None and action.name == name and action.marker == "▸",
          f"action row is {action.marker if action else ''} {action.name if action else '(none)'}")
    table = app.query_one("#fields", DataTable)
    table.focus()
    table.move_cursor(row=index or 0)
    app.action_follow()
    await settle(app, pilot)


async def main() -> int:
    for workload in workloads.WORKLOADS:
        rows = view.frame(None, workload).make_rows()
        missing = [r.name for r in rows if r.kind == "error" and r.name.startswith("no entry")]
        links = [r for r in rows if r.item is not None and view.action_of(r) is None]
        check(not missing and len(links) == len(workload.look)
              and all(r.marker == "→" for r in links),
              f"{workload.key}: {len(links)} entry rows marked → {missing or ''}")

    busy = [port for port in (40600, 40601) if port_in_use(port)]
    if busy:
        print(f"  FAIL ports {busy} are in use: stop the nagle workload running "
              "elsewhere (another explorer, or traffic.py) and run this again")
        return 1

    prog = drgn.program_from_kernel()
    app = Explorer(prog)
    async with app.run_test(size=(160, 45)) as pilot:
        app.query_one("#views", Tabs).active = "view-workloads"
        await settle(app, pilot)
        tree = app.query_one("#nav", Tree)
        node = next((n for n in tree_nodes(tree) if getattr(n.data, "key", "") == "nagle"), None)
        check(node is not None, "the workloads tab lists nagle")
        if node is None:
            return 1

        app.open_node(node)
        await settle(app, pilot)
        check(not workloads.running(node.data), "opening it does not start it")
        await press_action(app, pilot, "start")
        check(workloads.running(node.data), f"start row starts it: {workloads.status(node.data)}")
        check(str(node.label).startswith(view.RUNNING_MARK), f"sidebar marks it: {node.label}")

        # The child prints its first line once both connections are up.
        deadline = time.monotonic() + 10
        while not workloads.output(node.data) and time.monotonic() < deadline:
            await asyncio.sleep(0.2)
        app.action_refresh()
        await settle(app, pilot)
        output = [r for r in app.stack[-1].rows if r.name == "output"]
        check(bool(output), f"pane shows its output: {output[0].type_name if output else '(none)'}")

        found = nonagle_by_port(prog)
        check(found == {40600: 0, 40601: 1}, f"kernel nonagle by remote port: {found}")

        process = workloads._children["nagle"].process
        await press_action(app, pilot, "stop")
        check(process.poll() is not None and not workloads.running(node.data),
              f"stopped: {workloads.status(node.data)}")
        check(not str(node.label).startswith(view.RUNNING_MARK), f"sidebar unmarks it: {node.label}")

    await check_delay(prog)

    print("\nPASS" if ok else "\nFAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

"""Shared helpers for the tests that drive the TUI.

The explorer builds slow views (a walkthrough's source lookups, a measurement,
the landing page's capability probes) in a worker thread, so one message-loop
cycle after a keypress the screen shows a placeholder. ``pilot.pause()`` yields
to the message loop, not to the worker thread. A test that checks the contents
of such a view must wait for the build, or it asserts against the placeholder
and fails as if the app had a bug.
"""

from __future__ import annotations


async def settle(app, pilot, cycles: int = 2) -> None:
    """Wait until every background build has landed and been rendered.

    Twice around, because finishing one worker can start another: rendering a
    frame asks for the struct's documentation, which is itself a worker.
    """
    for _ in range(cycles):
        await pilot.pause()
        await app.workers.wait_for_complete()
    await pilot.pause()


def tree_nodes(tree) -> list:
    """Every node under the sidebar root, depth first.

    Subsystems nest (page under mm, socket under net), so an entry is not always
    two levels down.
    """
    found = []
    pending = list(reversed(tree.root.children))
    while pending:
        node = pending.pop()
        found.append(node)
        pending.extend(reversed(node.children))
    return found

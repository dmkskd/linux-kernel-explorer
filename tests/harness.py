"""Shared bits for the tests that drive the TUI.

The explorer builds anything slow -- a walkthrough's source lookups, a
measurement, the landing page's capability probes -- in a worker thread, so
what is on screen one message-loop cycle after a keypress is a placeholder.
``pilot.pause()`` does not wait for that: it yields to the message loop, not to
a thread. Tests that check the *contents* of such a view have to wait for the
build, or they assert against the placeholder and fail in a way that looks like
a bug in the app.
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

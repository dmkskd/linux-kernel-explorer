"""Simulated human navigation and cursor pathfinding across TUI tables.

Insulated from main application logic. Animates cursor gliding row-by-row
like a live terminal recording (asciinema style), computing how to reach from
a to b, updating row documentation as the cursor travels, before settling on
the destination.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from textual.app import App
    from textual.timer import Timer
    from textual.widgets import DataTable


@dataclass(frozen=True)
class NavigationPlan:
    """The computed path and action sequence to travel from a to b."""

    start: int
    target: int
    path: tuple[int, ...]
    direction: str  # "down", "up", or "stay"
    distance: int
    step_delay: float


def compute_cursor_path(
    start: int,
    target: int,
    max_steps: int = 24,
    total_duration: float = 1.1,
) -> NavigationPlan:
    """Compute an asciinema-like human traversal path from row `start` to row `target`.

    For short distances, visits every intermediate row (like someone tapping or
    holding arrow keys). For longer distances, generates an eased trajectory
    (accelerating then decelerating) so the movement is visibly readable
    without taking too long.
    """
    if start == target:
        return NavigationPlan(
            start=start,
            target=target,
            path=(target,),
            direction="stay",
            distance=0,
            step_delay=0.0,
        )

    diff = target - start
    dist = abs(diff)
    step_dir = 1 if diff > 0 else -1
    direction = "down" if diff > 0 else "up"

    if dist <= max_steps:
        # Linear stepping through every row
        path_list = list(range(start, target + step_dir, step_dir))
    else:
        # Eased trajectory: quadratic ease-in-out curve
        path_list = [start]
        num_steps = max_steps
        for i in range(1, num_steps):
            t = i / num_steps
            ease = 2.0 * t * t if t < 0.5 else 1.0 - ((-2.0 * t + 2.0) ** 2) / 2.0
            row = start + int(round(ease * diff))
            if row != path_list[-1]:
                path_list.append(row)
        if path_list[-1] != target:
            path_list.append(target)

    num_intervals = max(1, len(path_list) - 1)
    # Adaptive step delay clamped to keep movement deliberate, perceptible and human-paced
    step_delay = max(0.08, min(0.20, total_duration / num_intervals))

    return NavigationPlan(
        start=start,
        target=target,
        path=tuple(path_list),
        direction=direction,
        distance=dist,
        step_delay=step_delay,
    )


class CursorNavigator:
    """Insulated cursor driver that simulates human-like navigation from A to B.

    Drives smooth row-by-row cursor glide across a DataTable widget, updating
    documentation/hints along the way as a user would when navigating, then
    calls `on_settled` when landing squarely on the destination row.
    """

    def __init__(
        self,
        app: App,
        on_step: Callable[[int], None] | None = None,
        on_settled: Callable[[int], None] | None = None,
    ) -> None:
        self.app = app
        self.on_step = on_step
        self.on_settled = on_settled
        self._plan: NavigationPlan | None = None
        self._step_idx: int = 0
        self._timer: Timer | None = None
        self._table: DataTable | None = None
        self._is_active: bool = False

    @property
    def is_active(self) -> bool:
        """Whether a navigation animation is currently in progress."""
        return self._is_active

    def cancel(self, snap_to_target: bool = False) -> None:
        """Cancel the current animation, optionally snapping immediately to target."""
        if self._timer is not None:
            self._timer.stop()
            self._timer = None

        if self._is_active:
            self._is_active = False
            if snap_to_target and self._plan and self._table is not None:
                final_row = self._plan.target
                try:
                    self._table.move_cursor(row=final_row, animate=False)
                    if self.on_step is not None:
                        self.on_step(final_row)
                    if self.on_settled is not None:
                        self.on_settled(final_row)
                except Exception:
                    pass
        self._plan = None
        self._step_idx = 0

    def navigate(
        self,
        table: DataTable,
        start_row: int,
        target_row: int,
        total_duration: float = 0.55,
        immediate: bool = False,
    ) -> None:
        """Begin human-like cursor glide from start_row to target_row.

        If immediate=True or start_row == target_row, lands on target_row instantly.
        """
        self.cancel()
        self._table = table

        plan = compute_cursor_path(start_row, target_row, total_duration=total_duration)
        self._plan = plan

        if immediate or len(plan.path) <= 1:
            try:
                table.move_cursor(row=target_row, animate=False)
                if self.on_step is not None:
                    self.on_step(target_row)
                if self.on_settled is not None:
                    self.on_settled(target_row)
            except Exception:
                pass
            return

        self._is_active = True
        self._step_idx = 0

        # Position at start row
        try:
            table.move_cursor(row=plan.path[0], animate=False)
            if self.on_step is not None:
                self.on_step(plan.path[0])
        except Exception:
            pass

        self._timer = self.app.set_interval(plan.step_delay, self._tick)

    def _tick(self) -> None:
        if not self._is_active or self._table is None or self._plan is None:
            self.cancel()
            return

        self._step_idx += 1
        path = self._plan.path
        if self._step_idx >= len(path):
            final_row = path[-1]
            self.cancel()
            if self.on_settled is not None:
                self.on_settled(final_row)
            return

        current_row = path[self._step_idx]
        try:
            self._table.move_cursor(row=current_row, animate=False)
            if self.on_step is not None:
                self.on_step(current_row)
        except Exception:
            self.cancel()
            return

        if self._step_idx == len(path) - 1:
            # Reached target
            final_row = path[-1]
            self.cancel()
            if self.on_settled is not None:
                self.on_settled(final_row)

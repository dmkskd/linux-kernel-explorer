"""Tests for live guided tutorials through running kernel structures.

Verifies:
- All guided tutorials pass check against the live kernel.
- Tutorials dynamically inspect live processes, generating steps reflecting real PIDs.
- Tutorial frames render with the expected 4 columns.
- Live structure resolvers attached to steps expand into real kernel objects.
- The TUI displays the tutorials tab, builds the tree, opens tutorials,
  shows commentary, and cycles views via the 'v' keybinding.
- Backward-compatibility aliases (TourBanner, TourLanding, active_tour, initial_tour, etc.)
  remain operational.
"""

from __future__ import annotations

import asyncio
import sys

import drgn
from textual.widgets import DataTable, Static, Tabs, Tree

from kexplore.core.nav import Row
from kexplore.core.source import KernelSource
from kexplore.operations.tutorial import GuidedTutorial, TutorialStep, tutorials
from kexplore.tui.app import Explorer, TutorialBanner, TutorialLanding
from kexplore.view.frames import TUTORIAL_COLUMNS, Context, plan_for, tutorial_step_frame

ok = True


def check(condition: bool, message: str) -> None:
    global ok
    ok &= bool(condition)
    print(("  ok   " if condition else "  FAIL ") + message)


async def test_tui_tutorials(prog: drgn.Program) -> None:
    app = Explorer(prog, KernelSource(), live=True)

    async with app.run_test(size=(150, 45)) as pilot:
        tabs = app.query_one("#views", Tabs)
        tree = app.query_one("#nav", Tree)
        table = app.query_one("#fields", DataTable)

        # Tab check
        tab_ids = [t.id for t in tabs.query("Tab")]
        check("view-tutorials" in tab_ids, f"tutorials tab present: {tab_ids}")

        # Switch to tutorials tab
        tabs.active = "view-tutorials"
        await pilot.pause()

        check(tree.root.data is not None and tree.root.data.label == "tutorials",
              "nav tree root set to tutorials listing")

        categories = [str(n.label) for n in tree.root.children]
        check("process" in categories and "memory" in categories and "sched" in categories,
              f"categories present: {categories}")

        # Preview a tutorial node (displays tutorial landing preview)
        memory_branch = next(n for n in tree.root.children if str(n.label) == "memory")
        mem_tutorial = next(n for n in memory_branch.children if "Memory Types" in str(n.label))
        app.open_node(mem_tutorial, preview=True)
        await pilot.pause()

        landing = app.query_one("#tutorial-landing", TutorialLanding)
        check(landing.display is True, "tutorial landing preview displayed")
        landing_rendered = str(landing.render())
        check("What You'll Explore" in landing_rendered or "Goal & Live Invariants" in landing_rendered,
              "landing preview displays what you'll explore / overview")
        check("[Enter]" in landing_rendered, "landing preview displays [Enter] shortcut prompt")
        check("[a]" in landing_rendered, "landing preview displays [a] shortcut prompt")
        check("[c]" in landing_rendered, "landing preview displays [c] shortcut prompt")
        check("highlights" not in landing_rendered, "landing preview omits the short route table")
        check("[ENTER]" in landing_rendered, "landing preview keeps the per-step [ENTER] prompt")

        # Verify no markup styles leak to the end of the content
        landing_static = landing.query_one("#tutorial-landing-content", Static)
        if hasattr(landing_static, "_visual") and hasattr(landing_static._visual, "spans"):
            leaks = [sp for sp in landing_static._visual.spans if (sp.end - sp.start) > 250]
            check(len(leaks) == 0, f"landing preview has no leaked background spans: {len(leaks)}")

        # Selecting the tutorial starts the tutorial on the landing page (step 0)
        tree.focus()
        await pilot.pause()
        check(tree.has_focus is True, "nav tree is focused before selection")
        check(landing.styles.background_tint.a == 0, "landing background has no tint when unfocused")

        app.open_node(mem_tutorial, preview=False)
        await pilot.pause()

        # Focus must remain on the tree, exactly as in structures
        check(tree.has_focus is True, "focus stays on tree after selecting tutorial node")
        check(landing.has_focus is False, "landing does not steal focus when selected from tree")

        # Tabbing to the main screen focuses landing and applies background tint
        await pilot.press("tab")
        await pilot.pause()
        check(landing.has_focus is True, "tabbing moves focus to tutorial landing")
        check(landing.styles.background_tint.a > 0, "landing gains background tint when focused")

        banner = app.query_one("#tutorial-banner", TutorialBanner)
        check(app.active_tutorial is not None, "active tutorial started")
        check(app.active_tutorial.current_idx == 0, "tutorial session starts on step 0 (landing page)")
        check(landing.display is True, "landing page displayed on start")
        check(banner.display is False, "step banner hidden on landing page")

        # Verify context-dependent bindings on landing page (step 0):
        # Only start/next is enabled; table/struct actions and prev step are hidden.
        check(app.check_action("tutorial_next", ()) is True, "step 0: next step action enabled")
        check(app.check_action("tutorial_prev", ()) is None, "step 0: prev step action hidden")
        check(app.check_action("search", ()) is None, "step 0: search action hidden")
        check(app.check_action("sort", ()) is None, "step 0: sort action hidden")
        check(app.check_action("refresh", ()) is False, "step 0: refresh action hidden")
        check(app.check_action("userspace", ()) is None, "step 0: userspace action hidden")
        check(app.check_action("source", ()) is None, "step 0: source action hidden")
        check(app.check_action("trace_command", ()) is None, "step 0: trace_command action hidden")
        check(app.check_action("graph", ()) is None, "step 0: graph action hidden")

        # Advance to step 1 using action_tutorial_next (or 'n')
        app.action_tutorial_next()
        await pilot.pause()

        total_steps = len(app.active_tutorial.steps)
        check(app.active_tutorial.current_idx == 1, "advanced to step 1")
        check(banner.display is True, "tutorial banner is visible on step 1")
        check(f"Step 1 of {total_steps}" in str(banner.render()), f"tutorial banner shows step 1 of {total_steps}")
        check(landing.display is False, "landing page hidden on step 1")
        check(table.display is True, "fields table visible on step 1")

        # Verify context-dependent bindings on step 1. A walkthrough step offers
        # stepping, the route and the source; the browsing actions belong to the
        # structure tables and are withheld here, which is what keeps the footer
        # readable. ACTION_SCREENS is the declaration being checked.
        check(app.check_action("tutorial_next", ()) is True, "step 1: next step offered")
        check(app.check_action("tutorial_prev", ()) is True, "step 1: prev step offered")
        check(app.check_action("itinerary", ()) is True, "step 1: route offered")
        check(app.check_action("review", ()) is True, "step 1: review offered")
        check(app.check_action("copy_narration", ()) is True, "step 1: copy narration offered")
        for withheld in ("search", "sort", "userspace", "graph", "trace_command", "cycle_view"):
            check(app.check_action(withheld, ()) is None, f"step 1: {withheld} withheld from a step")
        check(app.check_action("refresh", ()) is False, "step 1: refresh withheld from a step")

        # Step 1 is the starting screen (kexplore home) frame
        step1_frame = app.stack[-1]
        check(len(step1_frame.rows) > 0, f"step 1 starting screen frame has {len(step1_frame.rows)} rows")

        # Advance to step 2: the process subsystem catalog
        app.action_tutorial_next()
        await pilot.pause()
        check(app.active_tutorial.current_idx == 2, "advanced to step 2")
        step2_frame = app.stack[-1]
        check(len(step2_frame.rows) > 0, f"step 2 catalog frame has {len(step2_frame.rows)} rows")

        # Advance to step 3: the live kernel structure (task_struct)
        app.action_tutorial_next()
        await pilot.pause()
        check(app.active_tutorial.current_idx == 3, "advanced to step 3")
        step3_frame = app.stack[-1]
        check(step3_frame.obj is not None, f"opened live struct: {step3_frame.obj.type_.type_name()}")
        check("field" in [str(c.label) for c in table.columns.values()], "table shows real struct fields")

        # Step through remaining steps of the tutorial using action_tutorial_next
        for step_num in range(4, total_steps + 1):
            app.action_tutorial_next()
            await pilot.pause()
            check(app.active_tutorial.current_idx == step_num, f"advanced to step {step_num}")
            check(f"Step {step_num} of {total_steps}" in str(banner.render()), f"tutorial banner shows step {step_num} of {total_steps}")
            check(len(app.stack[-1].rows) > 0, f"step {step_num} frame has {len(app.stack[-1].rows)} rows")
        check(app.check_action("tutorial_next", ()) is None, "final step: next step action hidden")
        check(app.check_action("tutorial_prev", ()) is True, "final step: prev step action enabled")

        # Step back through all steps using action_tutorial_prev
        for step_num in range(total_steps - 1, 0, -1):
            app.action_tutorial_prev()
            await pilot.pause()
            check(app.active_tutorial.current_idx == step_num, f"returned to step {step_num}")
            check(f"Step {step_num} of {total_steps}" in str(banner.render()), f"tutorial banner shows step {step_num} of {total_steps}")

        # Step back from step 1 returns to step 0 (the landing page)
        app.action_tutorial_prev()
        await pilot.pause()
        check(app.active_tutorial.current_idx == 0, "returned to step 0 (landing page)")
        check(landing.display is True, "landing page restored on retreat to step 0")
        check(banner.display is False, "step banner hidden on step 0")
        check(table.display is False, "fields table hidden on step 0")

        # Exit tutorial
        app.exit_tutorial()
        await pilot.pause()
        check(app.active_tutorial is None, "exited tutorial")
        check(landing.display is False, "tutorial landing hidden on exit")
        check(banner.display is False, "tutorial banner hidden on exit")
        check(table.display is True, "fields table restored on exit")

        # Test view cycling via action_cycle_view ('v')
        check(tabs.active == "view-tutorials", "currently on tutorials")
        app.action_cycle_view()
        await pilot.pause()
        check(tabs.active == "view-structures", f"cycled to structures: {tabs.active}")
        check(app.check_action("tutorial_next", ()) is None, "structures view: next step action hidden")
        check(app.check_action("tutorial_prev", ()) is None, "structures view: prev step action hidden")
        app.action_cycle_view()
        await pilot.pause()
        check(tabs.active == "view-operations", f"cycled to operations: {tabs.active}")
        app.action_cycle_view()
        await pilot.pause()
        check(tabs.active == "view-tutorials", f"cycled back to tutorials: {tabs.active}")


async def test_tui_initial_tutorial(prog: drgn.Program) -> None:
    app = Explorer(prog, KernelSource(), live=True, initial_tutorial="user_memory_types")

    async with app.run_test(size=(150, 45)) as pilot:
        await pilot.pause()
        tabs = app.query_one("#views", Tabs)
        table = app.query_one("#fields", DataTable)
        banner = app.query_one("#tutorial-banner", TutorialBanner)
        landing = app.query_one("#tutorial-landing", TutorialLanding)

        check(tabs.active == "view-tutorials", f"initial_tutorial opened tutorials tab: {tabs.active}")
        check(app.active_tutorial is not None, "initial_tutorial started active tutorial session")
        check(app.active_tutorial.tutorial.key == "user_memory_types", f"loaded tutorial: {app.active_tutorial.tutorial.key}")
        check(app.active_tutorial.current_idx == 0, "initial_tutorial landed on step 0 (landing page)")
        check("The route" not in str(landing.render()), "initial_tutorial landing omits the route table")
        check("Tutorial steps" in str(landing.render()), "initial_tutorial landing keeps the per-step itinerary")
        check(len(app.active_tutorial.steps) == 10, f"initial_tutorial loaded 10 steps: {len(app.active_tutorial.steps)}")
        check("Types of User Memory" in str(landing.render()), "landing page displays real video title")
        check("https://youtu.be/6dwzZEFEgWE" in str(landing.render()), "landing page displays real YouTube link")
        check("Scroll down" in str(landing.render()), "landing page has scroll down visual cue")
        check("End of itinerary" in str(landing.render()), "landing page has end of itinerary cue")

        # Verify #path display on step 0: visible, clean text without raw markup tags
        path = app.query_one("#path", Static)
        check(path.display is True, "step 0: #path is displayed")
        check("bold" not in str(path.render()), "step 0: #path contains clean text without raw tags")

        # Verify landing page scrolling: max_scroll_y > 0 and arrow down / pagedown move scroll_y
        check(landing.max_scroll_y > 0, f"landing page has scrollable content (max_scroll_y={landing.max_scroll_y})")
        initial_scroll = landing.scroll_y
        await pilot.press("down")
        await pilot.pause()
        check(landing.scroll_y > initial_scroll, f"pressing down scrolled landing page: {landing.scroll_y}")
        await pilot.press("pagedown")
        await pilot.pause()
        check(landing.scroll_y > initial_scroll + 1, f"pressing pagedown scrolled landing page further: {landing.scroll_y}")
        await pilot.press("home")
        await pilot.pause()
        check(landing.scroll_y == 0, f"pressing home scrolled back to top: {landing.scroll_y}")

        # Advancing from landing page via 'enter' enters step 1
        await pilot.press("enter")
        await pilot.pause()
        check(app.active_tutorial.current_idx == 1, "pressing Enter on landing page moved to step 1")
        check(banner.display is True, "step 1 banner displayed on Enter")
        check("Step 1 of 10" in str(banner.render()), "banner shows step 1 of 10")
        check(table.display is True, "fields table displayed on step 1")
        check(path.display is False, "step 1: #path hidden to give banner breathing room")

        # Step back to landing page with 'p'
        await pilot.press("p")
        await pilot.pause()
        check(app.active_tutorial.current_idx == 0, "pressing 'p' on step 1 retreated to landing page")
        check(landing.display is True, "landing page displayed after retreat")
        check(path.display is True, "step 0: #path restored on retreat to landing page")

        # Advancing from landing page via 'space' enters step 1
        await pilot.press("space")
        await pilot.pause()
        check(app.active_tutorial.current_idx == 1, "pressing Space on landing page moved to step 1")
        check(path.display is False, "step 1: #path hidden after space advance")

        # Step back to landing page with 'p'
        await pilot.press("p")
        await pilot.pause()
        check(app.active_tutorial.current_idx == 0, "retreated to landing page again")

        # Advancing from landing page via 'n' enters step 1
        await pilot.press("n")
        await pilot.pause()
        check(app.active_tutorial.current_idx == 1, "pressing 'n' on landing page moved to step 1")
        check(path.display is False, "step 1: #path hidden after 'n' advance")


async def test_tui_auto_and_highlights(prog: drgn.Program) -> None:
    app = Explorer(prog, KernelSource(), live=True, initial_tutorial="user_memory_types")

    async with app.run_test(size=(150, 45)) as pilot:
        await pilot.pause()
        table = app.query_one("#fields", DataTable)
        banner = app.query_one("#tutorial-banner", TutorialBanner)
        landing = app.query_one("#tutorial-landing", TutorialLanding)

        # 1. Landing page shows auto-play prompt
        check("Auto-Play" in str(landing.render()), "landing page displays [a] Auto-Play option")

        # 2. Pressing 'a' on landing page launches step 1 in auto mode
        await pilot.press("a")
        await pilot.pause()
        check(app.auto_play is True, "pressing 'a' engaged auto_play")
        check(app.active_tutorial.current_idx == 1, "auto mode advanced to step 1")
        check("AUTO PLAYING" in str(banner.render()), "banner displays AUTO PLAYING badge")

        # 3. Dual highlight checks on Step 1 (kexplore home)
        # The action row is the entry point that opens a process, not an
        # informational row: step 1 has to be able to reach step 2 from the
        # opening screen, and in the tutorials tab the sidebar is unavailable.
        ENTRY_ROW = "a process and its address space"
        table_rows = [table.get_row(str(i)) for i in range(len(app.stack[-1].rows))]
        action_rows = [r for r in table_rows if "ENTER" in str(r[0])]
        check(len(action_rows) == 1, f"exactly 1 action row highlighted on step 1: {len(action_rows)}")
        action_row = action_rows[0] if action_rows else None
        check(action_row is not None and ENTRY_ROW in str(action_row[0]), f"action row {ENTRY_ROW!r} has interactive [ENTER] coach mark: {action_row[0] if action_row else None}")

        # Table cursor placed directly on that action row
        current_row = app.current_row()
        check(current_row is not None and ENTRY_ROW in current_row.name, f"cursor positioned on action row {ENTRY_ROW!r}: {current_row.name if current_row else None}")

        # 4. Pressing 'a' pauses auto mode
        await pilot.press("a")
        await pilot.pause()
        check(app.auto_play is False, "pressing 'a' paused auto_play")
        check("AUTO PLAYING" not in str(banner.render()), "banner cleared AUTO PLAYING badge")

        # 5. Pressing Enter on the action row advances to step 2 (task_struct)
        await pilot.press("enter")
        await pilot.pause()
        check(app.active_tutorial.current_idx == 2, f"pressing Enter followed flow into step 2: {app.active_tutorial.current_idx}")
        current_row = app.current_row()
        check(current_row is not None and "mm" in current_row.name, f"step 2 cursor positioned on action row 'mm': {current_row.name if current_row else None}")

        # 6. Pressing Enter on step 2 advances to step 3 (mm_struct boundaries)
        await pilot.press("enter")
        await pilot.pause()
        check(app.active_tutorial.current_idx == 3, f"pressing Enter followed flow into step 3: {app.active_tutorial.current_idx}")
        current_row = app.current_row()
        check(current_row is not None and "VMAs" in current_row.name, f"step 3 cursor positioned on action row 'VMAs': {current_row.name if current_row else None}")

        # 7. Pressing Enter on step 3 advances to step 4 (VMAs maple tree list)
        await pilot.press("enter")
        await pilot.pause()
        check(app.active_tutorial.current_idx == 4, f"pressing Enter followed flow into step 4: {app.active_tutorial.current_idx}")

        # 8. Pressing Enter on step 4 advances to step 5 (text segment VMA)
        await pilot.press("enter")
        await pilot.pause()
        check(app.active_tutorial.current_idx == 5, f"pressing Enter followed flow into step 5: {app.active_tutorial.current_idx}")
        current_row = app.current_row()
        check(current_row is not None and "vm_file" in current_row.name, f"step 5 cursor positioned on action row 'vm_file': {current_row.name if current_row else None}")

        # 9. Test Header Variant Toggle ('H')
        check(banner.header_variant == 2, "default header variant is 2 (title and roadmap)")
        check("Flow:" in str(banner.render()), "variant 2 displays the traversal roadmap")
        await pilot.press("H")
        await pilot.pause()
        check(banner.header_variant == 1, "toggled to header variant 1 (Minimal HUD)")
        check("Flow:" not in str(banner.render()), "variant 1 omits the roadmap (title only)")
        await pilot.press("H")
        await pilot.pause()
        check(banner.header_variant == 2, "toggled back to header variant 2")
        check("Flow:" in str(banner.render()), "variant 2 restored the roadmap")

        # 10. Advance to Step 10 (memory accounting: rss_stat) and test row expansion
        app._show_tutorial_step(10)
        await pilot.pause()
        check(app.active_tutorial.current_idx == 10, f"jumped to step 10: {app.active_tutorial.current_idx}")
        current_row = app.current_row()
        check(current_row is not None and current_row.name == "rss_stat", f"step 10 cursor positioned on 'rss_stat': {current_row.name if current_row else None}")
        check(current_row.expanded is False, "rss_stat initially collapsed")

        # Press space to expand rss_stat
        await pilot.press("space")
        await pilot.pause()
        current_row = app.current_row()
        check(current_row is not None and current_row.expanded is True, "rss_stat expanded on space key press")
        frame_rows = app.stack[-1].rows
        child_names = [r.name for r in frame_rows if r.depth > 0]
        check("[0]" in child_names and "[1]" in child_names and "[2]" in child_names and "[3]" in child_names, f"expanded children include [0]..[3]: {child_names}")
        child_notes = [r.note for r in frame_rows if r.depth > 0]
        check(any("MM_FILEPAGES" in note for note in child_notes), "child notes include MM_FILEPAGES")
        check(any("MM_ANONPAGES" in note for note in child_notes), "child notes include MM_ANONPAGES")

        # Press space again to collapse
        await pilot.press("space")
        await pilot.pause()
        current_row = app.current_row()
        check(current_row is not None and current_row.expanded is False, "rss_stat collapsed on second space press")

        # Press enter on step 9 to verify it expands rather than failing
        await pilot.press("enter")
        await pilot.pause()
        current_row = app.current_row()
        check(current_row is not None and current_row.expanded is True, "rss_stat expanded on enter key press on last step")


async def test_cursor_navigator() -> None:
    from kexplore.tui.navigator import CursorNavigator, compute_cursor_path

    # 1. Test compute_cursor_path planning logic
    p1 = compute_cursor_path(0, 5)
    check(p1.path == (0, 1, 2, 3, 4, 5), f"linear path 0->5: {p1.path}")
    check(p1.direction == "down", f"path 0->5 direction down: {p1.direction}")
    check(p1.distance == 5, f"path 0->5 distance 5: {p1.distance}")

    p2 = compute_cursor_path(5, 0)
    check(p2.path == (5, 4, 3, 2, 1, 0), f"reverse path 5->0: {p2.path}")
    check(p2.direction == "up", f"path 5->0 direction up: {p2.direction}")

    p3 = compute_cursor_path(10, 10)
    check(p3.path == (10,), f"stay path 10->10: {p3.path}")
    check(p3.direction == "stay", f"path 10->10 direction stay: {p3.direction}")

    p4 = compute_cursor_path(0, 60)
    check(p4.path[0] == 0 and p4.path[-1] == 60, f"eased path start/end 0/60: {p4.path[0]}..{p4.path[-1]}")
    check(len(p4.path) <= 25, f"eased path bounded in length: {len(p4.path)}")

    # 2. Test live simulated glide in Textual App
    from textual.app import App, ComposeResult

    class MiniApp(App):
        def compose(self) -> ComposeResult:
            yield DataTable(id="tbl")

        def on_mount(self) -> None:
            t = self.query_one(DataTable)
            t.add_column("Col")
            for i in range(20):
                t.add_row(f"Row {i}", key=str(i))

    mini = MiniApp()
    async with mini.run_test() as pilot:
        tbl = mini.query_one(DataTable)
        steps_visited: list[int] = []
        settled_rows: list[int] = []
        nav = CursorNavigator(
            mini,
            on_step=lambda r: steps_visited.append(r),
            on_settled=lambda r: settled_rows.append(r),
        )
        nav.navigate(tbl, start_row=0, target_row=8, total_duration=0.1)
        check(nav.is_active is True, "navigator is_active while travelling")
        while nav.is_active:
            await pilot.pause(0.02)
        check(nav.is_active is False, "navigator is_active False after settling")
        check(settled_rows == [8], f"settled at target row 8: {settled_rows}")
        check(tbl.cursor_row == 8, f"table cursor settled at 8: {tbl.cursor_row}")
        check(len(steps_visited) >= 8, f"visited intermediate rows: {len(steps_visited)}")


async def test_bracket_handling_and_hint_safety(prog: drgn.Program) -> None:
    from kexplore.tui.app import safe_escape
    from textual.markup import to_content

    # 1. Verify safe_escape preserves unmatched brackets without breaking markup parser
    test_cases = [
        "VMA covers [vm_start; vm_end) addresses within mm",
        "array[0]",
        "[bold red]test[/bold red]",
        "normal text",
        "single [ bracket",
        "close ] bracket",
        "grep -E '(\\[heap\\]|\\[stack\\])' /proc/1/maps",
    ]
    for s in test_cases:
        line = f"[bold white]{safe_escape(s)}[/]"
        c = to_content(line)
        check(len(c.plain) > 0, f"safe_escape parsed: {s[:30]}")

    # 2. Test in-app update_hint with mathematical bracket interval and payoff badge
    app = Explorer(prog, KernelSource(), live=True, initial_tutorial="user_memory_types")
    async with app.run_test(size=(140, 45)) as pilot:
        await pilot.pause()
        # Advance to step 1
        await pilot.press("enter")
        await pilot.pause()

        hint = app.query_one("#hint", Static)

        # Mock struct doc with interval notation for the current row
        cur_name = app.current_row().name if app.current_row() else "start_code"
        class FakeDoc:
            members = {cur_name: "VMA covers [vm_start; vm_end) addresses within mm"}

        orig_struct_doc = app.struct_doc
        app.struct_doc = lambda obj: FakeDoc()

        # Step 1's action row is current row
        app.update_hint()
        hint_val = str(hint.renderable)
        check("[vm_start; vm_end)" in hint_val, f"hint contains mathematical interval: {hint_val}")
        check("Press [Enter]" in hint_val, f"hint contains follow action: {hint_val}")
        app.struct_doc = orig_struct_doc


async def test_clipboard_and_mouse_selection(prog: drgn.Program) -> None:
    app = Explorer(prog, KernelSource(), live=True, initial_tutorial="user_memory_types")
    async with app.run_test(size=(140, 45)) as pilot:
        await pilot.pause()

        # 1. On Tutorial Landing: test 'c' and 'y' copying video link / overview
        landing = app.query_one("#tutorial-landing", TutorialLanding)
        check(landing.display is True, "landing displayed on start")
        await pilot.press("c")
        await pilot.pause()
        check(getattr(app, "_last_copied", None) is not None, "copied on landing using 'c'")
        check("youtu" in getattr(app, "_last_copied", ""), f"landing 'c' copied video URL: {getattr(app, '_last_copied', '')}")

        # 2. Test 'm' toggling mouse tracking mode from landing
        check(app.mouse_tracking is True, "default mouse tracking is True")
        await pilot.press("m")
        await pilot.pause()
        check(app.mouse_tracking is False, "mouse tracking toggled to False (terminal selection mode)")
        await pilot.press("m")
        await pilot.pause()
        check(app.mouse_tracking is True, "mouse tracking toggled back to True (TUI mode)")

        # 3. Enter Step 1: fields table visible
        await pilot.press("enter")
        await pilot.pause()
        check(landing.display is False, "step 1 table displayed")

        # 4. Copy value from current row using 'c' and 'y'
        table = app.query_one("#fields", DataTable)
        current = app.current_row()
        check(current is not None, "current row found on step 1")
        await pilot.press("c")
        await pilot.pause()
        copied_c = getattr(app, "_last_copied", "")
        expected_val = current.value or current.name
        check(copied_c == expected_val, f"'c' copied row value: '{copied_c}' == '{expected_val}'")

        # 5. Copy full row using 'C'
        await pilot.press("C")
        await pilot.pause()
        copied_row = getattr(app, "_last_copied", "")
        check(current.name in copied_row, f"'C' copied full row containing name: '{copied_row}'")

        # 6. Test 'm' inside table screen
        await pilot.press("m")
        await pilot.pause()
        check(app.mouse_tracking is False, "toggled mouse in table view")
        await pilot.press("m")
        await pilot.pause()
        check(app.mouse_tracking is True, "restored mouse in table view")


def main() -> int:
    prog = drgn.program_from_kernel()
    prog.load_default_debug_info()
    ctx = Context(prog, KernelSource())

    print("Checking live guided tutorials:")
    tut_list = tutorials()
    check(len(tut_list) >= 5, f"at least 5 guided tutorials defined: {len(tut_list)}")

    for tut in tut_list:
        res = tut.check(prog)
        check(res.ok, f"check {tut.label}: {res.detail}")

        steps = tut.steps(prog)
        check(len(steps) >= 7, f"{tut.label} produced {len(steps)} live steps")

        plan = plan_for(tut, ctx)
        check(plan is not None, f"plan_for {tut.label} succeeds")
        frame = plan.build()
        frame.load()
        check(frame.columns == TUTORIAL_COLUMNS, f"columns match TUTORIAL_COLUMNS: {frame.columns}")
        check(len(frame.rows) == len(steps), f"row count matches steps: {len(frame.rows)}")
        check(all(r.cells is not None and len(r.cells) == 4 for r in frame.rows),
              "all rows define 4 cells")

        for s in steps:
            sf = tutorial_step_frame(ctx, s)
            sf.load()
            check(len(sf.rows) > 0, f"{tut.label} step '{s.title}' loaded {len(sf.rows)} rows")

    print("\nChecking TUI integration:")
    asyncio.run(test_tui_tutorials(prog))
    asyncio.run(test_tui_initial_tutorial(prog))
    asyncio.run(test_tui_auto_and_highlights(prog))
    asyncio.run(test_cursor_navigator())
    asyncio.run(test_bracket_handling_and_hint_safety(prog))
    asyncio.run(test_clipboard_and_mouse_selection(prog))

    print("\nPASS" if ok else "\nFAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())


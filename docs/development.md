# Code layout and tests

## Layout

```
core/       mechanism, and no Linux knowledge: rows, types, layout, source,
            probes. core/graph.py never reads memory, so the drawing can be
            tested anywhere.
catalog/    what exists in Linux: entry points per subsystem, the curated
            links between structures, what raw field values mean.
operations/ views that are computed rather than browsed: step sequences,
            analyses, controlled experiments.
view/       catalog items turned into tables of rows. Imports no UI toolkit.
tui/        Textual: when to build a frame, which one is on screen, what the
            keys do to it.
```

## Tests

```sh
./run.sh --test              # everything, inside the VM
python3 tests/run_all.py     # on the host: the tests that need no kernel
```

Most tests attach to the live kernel, so they need the VM and root; `run_all.py`
says which it skipped and why. `tests/helpers/` holds programs that *make
something happen* so a measurement has something to see; they are not tests and
are not run.

`test_crawl.py` is the important one: it visits everything reachable in two hops
from every entry and reports what raises. Every bug that reached a user was a
type violating an assumption held elsewhere, and targeted tests only visit types
someone already thought about.

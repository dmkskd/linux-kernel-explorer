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
./run.sh --test              # everything, through the resolved backend
python3 tests/run_all.py     # on the host: the tests that need no kernel
```

Most tests attach to the live kernel, so they need root wherever the backend
(see the README) runs; `run_all.py` says which it skipped and why. `tests/helpers/` holds programs that *make
something happen* so a measurement has something to see; they are not tests and
are not run.

`test_crawl.py` is the important one: it visits everything reachable in two hops
from every entry and reports what raises. Every bug that reached a user was a
type violating an assumption held elsewhere, and targeted tests only visit types
someone already thought about.

## Recording the README demo

The README embeds an asciinema recording of a session. To refresh it:

```sh
./run.sh --record demo.cast    # record; the file lands on this host
asciinema upload demo.cast     # prints the cast URL
```

then replace the cast id in the two `asciinema.org/a/…` links in the README.
Recording wraps whichever backend `run.sh` resolves, so it works the same from
a mac on the lima backend as from a Linux host. Keep the terminal at a
sensible size (the player reproduces it) and pause a moment after pressing
`t`: the trace frame builds in the background, and a recording that cuts away
during the placeholder shows nothing.

# Kernel capability and layout compatibility

Views detect fields and symbols in the inspected kernel's DWARF. They do not
select implementations from distribution names or kernel release strings:
backports, configuration, and custom builds can change the layout independently.

An Entry can supply a capability callback. It returns None for a supported
layout, or an explanation when the required data is unavailable. Supported
providers then select their layout adapter from actual fields. Exceptions in
the capability callback or provider remain failures, not unsupported results.

There are three distinct outcomes:

| Outcome | UI | --check |
| --- | --- | --- |
| Supported, possibly empty | Structures or an empty collection | ok |
| Required data/layout unavailable | Unavailable on this kernel, with reason | SKIP, counted separately |
| Provider, probe, or memory access fails | Error | FAIL, nonzero exit status |

The crawl skips explicitly unavailable entries and still fails for errors in
supported entries. Capability selection is currently applied to the mutex,
futex, and block hardware queue entry points; it is a mechanism to extend to
other views as their layouts require it, not a claim of universal support.

## Current adapters

- Mutex waiting requires task_struct.blocked_on. A kernel without it does
  not expose the required wait targets for this view. The view explains this
  rather than returning an empty list or inferring wait targets from signals.
- Futex waiting supports the flat global table sized by hashsize, and newer
  hashmask-based tables. Process-private hashes are walked only when
  mm_struct.futex_phash exists. The older global table contains private and
  shared waiters.
- Hardware queues support queue_hw_ctx arrays and hctx_table xarrays. The
  catalog entry and request queue navigation link use the same adapter.

New adapters should have real-kernel navigation coverage on the relevant lab
profiles. Dispatcher tests also verify that unavailable data, empty results,
and implementation faults stay distinct. Every unsupported view must explain
the missing capability; broad exception handling is not capability detection.

# Proposal: make catalog knowledge first-class data

Status: proposal. This document does not change the runtime architecture.

## Problem and scope

drgn already provides an elegant abstraction for reading kernel objects,
following members, and resolving types. Keep it as the live kernel access
layer. A database of tasks, address spaces, or other live objects is not needed
for this proposal.

The opportunity is the information kexplore maintains around those objects:
what to explore, what a relationship means, how a field is decoded, which
userspace interface exposes it, and which operation or tutorial explains it.
That information is spread across Python dictionaries, lists, dataclasses,
and resolver definitions. Individual definitions are often well structured,
but there is no shared model for querying their connections or checking all
their references.

Give this authored knowledge stable identities and an explicit relational
model. Python continues to implement kernel access, discovery, computation,
and experiments. The browser, graph, command tracing, and tutorials consume
the same knowledge records.

The inspiration is [k8s-compass](https://github.com/dmkskd/k8s-compass), whose
[documented data model](https://github.com/dmkskd/k8s-compass/blob/main/docs/data-model.md)
connects API definitions, relationships, releases, and learning material through
tables. The relevant idea is a shared, queryable model; its Parquet delivery
and browser query engine are not requirements for kexplore.

## Existing information to model

| Information | Existing location | Proposed records |
| --- | --- | --- |
| Subsystems, entries, groups, labels, documentation | `catalog/registry.py` and subsystem modules | `subsystems`, `entries` |
| Curated relationships, traversal origins, userspace counterparts | `catalog/links.py` | `relationships`, `relationship_commands` |
| Derived values and their explanations | `catalog/links.py` | `derived_fields` |
| Macro values, flag meanings, field decoder associations | `catalog/decoders.py` | `decoders`, `decoder_values`, `field_decoders` |
| Commands associated with entries and fields | `catalog/userspace.py` | `commands`, `entry_commands`, `field_commands` |
| Published interfaces and serving functions | `catalog/procfs.py` | `interfaces`, `interface_handlers` |
| Operations, tutorial steps, function references, highlighted fields | `operations/walkthrough.py`, `operations/tour.py` | `operations`, `steps`, `step_fields`, `step_functions` |
| Probe scripts, measured endpoints, limitations, output definitions | `catalog/registry.py`, `catalog/measure.py` | `measurements`, `measurement_outputs` |

These are proposed logical tables, not a commitment to migrate every item at
once. Executable code interleaved with the definitions stays in Python.

## Concrete example: a field and its connections

Introduce an authored field reference for `task_struct.comm`. This identifies
the field we discuss, without copying its build-specific DWARF definition.

`fields`:

| id | type_name | member_path |
| --- | --- | --- |
| task.comm | task_struct | comm |

`commands`:

| id | text | purpose |
| --- | --- | --- |
| task.read_comm | cat /proc/<pid>/comm | Read a task's command name |

`field_commands`:

| field_id | command_id | equivalence |
| --- | --- | --- |
| task.comm | task.read_comm | Publishes the task's command name |

`interfaces` and `interface_handlers` would describe `/proc/<pid>/comm` and
its curated association with `proc_task_name`, already recorded in
`catalog/procfs.py`. An explicit `command_interfaces` association connects the
command to this interface instead of relying solely on parsing command text.

A proposed `step_fields` record could associate a tutorial step with
`task.comm`. A query can then discover every step explaining that field:

```sql
SELECT o.label, s.position, s.title
FROM operations AS o
JOIN steps AS s ON s.operation_id = o.id
JOIN step_fields AS sf ON sf.step_id = s.id
WHERE sf.field_id = 'task.comm'
ORDER BY o.id, s.position;
```

Another query could join the field's command, interface, and serving function.
The browser can offer these connections without a new dictionary or custom
lookup for each screen. The live value of `comm` still comes directly from
drgn when the user opens the task.

Commands are explanatory data unless explicitly marked as executable. Some
existing strings include alternatives and prose; loading a record must not
automatically execute it.

## Concrete example: relationships with executable resolvers

A simple relationship can describe a member access declaratively:

```yaml
id: task.address_space
source_type: task_struct
target_type: mm_struct
label: address space
description: The task's userspace address space, when present.
origin: task->mm
access_kind: member
member_path: mm
```

A generic member resolver uses drgn to retrieve `task.mm`. A relationship that
requires a list walk names a Python implementation:

```yaml
id: task.threads
source_type: task_struct
target_type: task_struct
label: threads
description: Tasks belonging to the same thread group.
origin: task->signal->thread_head
access_kind: resolver
resolver_id: task_threads
```

An explicit Python registry binds `task_threads` to a callable. Configuration
does not contain arbitrary Python expressions or dynamic import paths.

The browser and graph read the same relationship records. The resolver keeps
the existing lazy acquisition, bounded walks, and failure handling through
`nav.collect`. A relationship's label can change without breaking references
to its ID. Command associations refer to that ID, not to the display label.

The existing convention that a `Link` carries its origin and userspace context
should remain true for the runtime object. The loader assembles those values
from the model; consumers should not perform independent lookups by label.

## Model rules

- Give every referenced record a stable ID, separate from labels and ordering.
- Use explicit association tables for fields, commands, interfaces, functions,
  relationships, and steps. Avoid storing these connections only in prose.
- Store ordered steps with a position and explicit fields to highlight. Dynamic
  tutorial builders can bind records to live objects or choose applicable steps.
- Distinguish an unavailable userspace equivalent from an undocumented one.
  Preserve the meaning of existing intentionally empty command strings.
- Retain provenance and applicability for curated claims: source reference,
  evidence kind, and kernel requirements where known. A table entry about a
  serving function is a curated association, not proof that it ran.
- Keep measured function calls, source-derived references, direct helpers, and
  catalog associations distinguishable in command tracing.
- Obtain actual types, member offsets, symbols, and source comments from the
  matching kernel tools. Do not duplicate DWARF layouts in the authored model.
- Prefer existing drgn decoders where available. Authored macro tables must
  retain raw values and unknown-value behavior, since constants can change.

## Authoring and storage

Recommended starting point: version-controlled YAML records, validated against
an explicit schema and loaded into an in-memory SQLite database. SQLite is
available in Python's standard library and supports the joins this catalog
needs. YAML parsing would require a dependency added through the project's
dependency setup; JSON is an alternative if avoiding that dependency matters.

The editable records are authoritative. The database is a derived query index,
built deterministically and exposed through a catalog API. Small read-only
indexes can support frequent per-object lookups without querying on every row.
Split authoring files by subsystem when helpful, but validate them as one model.
First-class data requires shared identities and constraints, not one huge file.

DuckDB is also an option if analytical queries become important. Parquet can
serve exports or another frontend later. Neither is necessary for moving
authored knowledge out of Python, and neither replaces drgn.

Validate references before publishing the model: foreign keys, unique IDs,
step positions, access specifications, and named resolver bindings. Check
build-specific field availability when attached to a kernel; absence on one
kernel must not make the authored catalog invalid for every kernel.

## Runtime boundary

```text
Authored records -> validation -> catalog model / SQL index
                                      |
                          browser, graph, operations, tutorials
                                      |
                          registered Python implementations
                                      |
                       drgn, source tools, probe runners
```

Definitions are loaded without reading kernel memory, fetching source, or
starting probes. Acquisition occurs only when requested, as it does in the
existing explorer. Python objects continue to carry live drgn handles.

`Row` and `Frame` remain presentation models. Their formatted cells,
callbacks, expansion state, and sorting state do not belong in the knowledge
database. Experiments retain process lifecycle code; their descriptions and
output meanings can become records separately.

## Incremental migration

1. Define the schema and migrate one connected subset: task field commands,
   their procfs interfaces, and the task address-space/thread relationships.
   Give named implementations to the relevant resolvers.
2. Build adapters that produce the existing `Entry`, `Link`, and related
   runtime objects. Keep the catalog API, lazy resolution, and view behavior.
3. Demonstrate a joined lookup for a field's commands and serving functions,
   and show that browser and graph consume the same relationship record.
4. Add operation/tutorial field references to that subset. Keep dynamic
   discovery in Python, with authored step content separated where practical.
5. Extend to decoders, derived fields, and measurement definitions after the
   first subset establishes useful conventions.

During migration, each definition has one authoritative source. Do not keep
an editable Python definition and an editable data record for the same item.
Adapters can support migrated and unmigrated items side by side.

## Acceptance criteria and tradeoffs

The first implementation should let a contributor edit an explanation, add a
command association, or link a tutorial step to a field without editing Python.
A broken reference or missing resolver binding should fail validation. A single
query should retrieve a field's connected knowledge. Existing navigation must
still return real drgn objects and preserve empty, error, and truncated states.

Verification should cover model integrity and behavior through the existing
catalog, graph, view, and crawl checks. Test invalid references and resolver
bindings explicitly; validate migrated records against supported live kernels
without treating missing kernel features as schema errors.

This introduces schema maintenance, a loader, and a boundary between records
and executable implementations. A dictionary is still appropriate for a small
private implementation detail. Migration is justified where shared knowledge
needs cross-references, validation, or reuse across views. The initial subset
should establish that benefit before expanding the scope.

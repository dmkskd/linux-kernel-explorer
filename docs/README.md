# kexplore docs

[installation.md](installation.md) describes the recommended learning lab,
required DWARF, optional source trees, and native/container installation limits.

[how-it-works.md](how-it-works.md) covers drgn, debuginfod, pahole, addr2line
and bpftrace, and what each is responsible for.

[development.md](development.md) describes what each package is responsible for
and how to run the tests.

[measuring.md](measuring.md) is what the clone timing column cost to make
trustworthy: five ways the harness measured itself, and how each was
identified.

[tracing-a-command.md](tracing-a-command.md) is the stage-by-stage account of
what `t` does with a userspace command: which parts are measured on the machine,
which part is asserted by a table, and what is known to be wrong with the part
that is asserted.

[kernel-concepts.md](kernel-concepts.md) relates theoretical Linux kernel concepts
(memory types, page tables, address spaces, EEVDF scheduling, clone flags, and COW)
to concrete data structures and traces inside the explorer.

[operations.md](operations.md) is what separates an operation from a tour, which
of the six current entries meets that line, and what is open on each.

[ideas.md](ideas.md) collects what is not built yet: the three kinds of view
the current one cannot express, and the subsystems nothing in the catalog
reaches.

[catalog-data-model-proposal.md](catalog-data-model-proposal.md) proposes a
shared relational model for catalog knowledge, commands, relationships, and
tutorial references, while retaining drgn for live kernel access.

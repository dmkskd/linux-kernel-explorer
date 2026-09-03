# Helpers, not tests

Programs that *make something happen* on the machine, so that a measurement or
an analysis has something to see. Nothing here asserts anything, and
`run_all.py` does not run them.

- `clone_matrix.c`: clones once per CLONE_* flag combination and holds the
  children alive. Compiled and run by the clone analyses in
  `kexplore/operations/clone_experiment.py`; not standalone.
- `clone_cost.py`: makes forks and threads in a loop, so
  *process > measure > what does a fork cost versus a thread?* records
  something instead of an empty histogram.
- `clone_demo.py`: one fork and one thread, printed side by side.
- `stuck_socket.py`: leaves data unread in a socket receive queue, so
  *skb > non-empty socket queues* has a queue to show.

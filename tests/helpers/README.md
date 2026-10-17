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
- `sysv_ipc.py`: creates a System V message queue holding an unreceived
  message, a semaphore array with a task blocked in semop(2), and an attached
  shared memory segment, so the *ipc* entries have objects to list. Removes
  them on exit; after a SIGKILL, `ipcrm` does.
- `traffic.py`: loopback traffic by mode (`established`, `stream`, `stall`,
  `churn`, `backlog`, `nagle`, `udp`, `unix`, or `all`). The *socket* and
  *skb* entries then list ESTABLISHED, TIME_WAIT and SYN_SENT sockets, a full
  accept queue, non-empty receive and write queues, and one connection with
  Nagle enabled next to one with TCP_NODELAY. `delay` (CAP_NET_ADMIN, excluded
  from `all`) installs a netem root qdisc on lo with 1 s latency: packets wait
  in netem's tfifo, and each sent skb is on tcp_rtx_queue until its ACK
  arrives. It deletes the qdisc on exit. The module docstring describes each
  mode.

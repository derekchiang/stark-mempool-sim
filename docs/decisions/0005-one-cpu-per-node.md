# ADR 0005: Model one contended CPU per node

## Context

Verification, recursive proving, and block construction consume the same
machine. Separate unlimited servers would invent parallel capacity and hide
contention. The model assumes each job can use the whole machine internally;
under that assumption, serial full-speed execution is work-conserving and
finishes jobs no later than fair sharing.

## Decision

Each node has one non-preemptive CPU server for `VERIFY_RECV`,
`PROVE_RECURSIVE`, and `BUILD_BLOCK`. FIFO is the default discipline. The
optional priority discipline orders waiting verification before build before
prove jobs, while preserving FIFO ties and never displacing already admitted
work.

## Consequences

The model exposes utilisation, backlog, head-of-line blocking, and queue
refusal across job classes. It also admits two signed limitations. First,
serial whole-machine verification is pessimistic because real machines can
verify several mostly sequential jobs concurrently. Second, block timing is
generous: the chain fact is created at the slot boundary and build cost occupies
the CPU afterwards, so contention can reduce coverage but cannot make a block
miss its slot. Non-preemptive FIFO can also delay urgent verification behind a
long fold.

## Where it lives in the code

`starksim.netsim.NetworkSim.__init__` creates one `cpu` server per node.
`starksim.netsim._cpu_priority_rank` defines priority order.
`NetworkSim._deliver_body`, `_deliver_wrapper`, `_deliver_block`, `_tick`, and
`_build_block` submit the three job classes. `starksim.engine.Server` provides
serial, non-preemptive service.

## Status

Accepted and implemented, with the two limitations above retained as model
caveats.

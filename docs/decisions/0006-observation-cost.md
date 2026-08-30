# ADR 0006: Keep observation off the simulated critical path

## Context

Instrumentation can change a discrete-event simulation by consuming calendar
sequence numbers, adding events, or advancing time differently. It can also
dominate runtime: reclassifying every node against every followed transaction
after every event grows superlinearly with run length even when the simulation
itself does not.

## Decision

Tracing is synchronous observation, not simulated work. Periodic observations
use the engine's observer grid without entering the event calendar or increasing
the processed-event count. Knowledge changes are flushed once per completed
domain event and recomputed only for dirty nodes and affected transaction masks.
Trace focus and sampling reduce output volume and are recorded in the header.

## Consequences

Enabling a trace does not change same-time event ordering or the model's clock
trajectory. Knowledge records describe net event-boundary states rather than
unobservable intermediate mutations. Observation work scales with changed
state and the selected follow set; broad invalidation remains an explicit slow
path. Wall-clock tracing cost is real but cannot become simulated delay.

## Where it lives in the code

`starksim.engine.Engine.observe_every`, `_observe_through`, and `after_event`
provide non-calendar hooks. `starksim.netsim.KnowledgeTracer.mark`, `mark_all`,
and `flush` track bounded dirty state. `NetworkSim._trace_counters` emits the
observer grid. `starksim.trace.Tracer.emit` and `header` implement focus,
sampling, and observation metadata.

## Status

Accepted and implemented; instrumentation changes require trajectory and
complexity regression tests.

# ADR 0007: netsim keeps event orchestration in one module

## Context

Reporting and knowledge live in separate modules. Before extracting the
remaining sections, the coupling between them was measured by counting the
distinct `self` attributes each group of methods touches:

| section | attributes touched |
| --- | --- |
| transport (relay, egress, delivery, retries) | 62 |
| aggregation runtime (ticks, plans, proving, wrappers) | 52 |
| block building | 24 |

Transport and aggregation share 17. All three share `arena`, `costs`, `cpu`,
`engine`, `knowledge`, `nodes` and `tracer` — the simulation's core state
rather than incidental helpers, so those handlers do not expose a stable
ownership boundary.

A configuration value object was considered separately, against a keyword
constructor with dozens of parameters and construction sites throughout the
bench, tool and test tree.

## Decision

Keep transport, aggregation, block construction, handler registration,
constructor validation, and their accounting in `starksim/netsim.py`. Keep
keyword arguments as the construction API.

## Consequences

`netsim.py` remains the orchestration module. Splitting tightly coupled handlers
would move shared mutation behind cross-module ownership. `reporting.py` and
`knowledge.py` remain independently readable and do not import `netsim.py`. A
configuration object requires a deliberate API migration; it is not incidental
cleanup.

## Where it lives in the code

`starksim/netsim.py` (facade and orchestration), `starksim/reporting.py`,
`starksim/knowledge.py`. The facade contract is pinned by
`tests/test_netsim_characterization.py`: the constructor's parameter tuple, the
CSV column order and an exact row, the complete `variant()` and `metrics()` key
sets, a byte-identical trace, same-timestamp event ordering, and byte
conservation.

## Status

Accepted. Revisit when a specific dependency boundary or API migration is
proposed, and re-measure the coupling first: the attribute counts are the
argument, not the file size.

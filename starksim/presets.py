#!/usr/bin/env python3
"""Named configurations, as keyword arguments for `NetworkSim`.

Three readings of the model that the benches and the documentation refer to
by name. Each is a plain dict, so a caller can start from one and override:

    NetworkSim(overlay, **preset("high_throughput"), seed=3)

A preset names policy fields only. It does not choose the overlay, the size
model or the cost table, which stay with the workload that uses it.
"""

# The EIP as written. These are the constructor's own defaults, spelled out
# so that the correspondence is a checked object rather than an absence:
# `tests/test_presets.py` holds each value to the constructor, and
# `tests/test_eip_faithful.py` holds the constructor to the documented table.
EIP_FAITHFUL = {
    "aggregation_interval": 1000.0,
    "aggregation_trigger": "spread",
    "wrapper_mode": "recursive",
    "fold_threshold": "always",
    "fold_plan": "wide",
    "selection_strategy": "eip_priority",
    "inclusion_policy": "body_required",
    "wrapper_economy": "full",
    "supersede_policy": "keep_all",
    "relay_policy": "flood",
    "prove_cost_basis": "deps",
    "proposer_mode": "fixed",
    "chain_notify": "instant",
    "cpu_discipline": "fifo",
    "eviction_policy": "usefulness",
}

# The retired v1 model, as far as configuration reaches it: bytes free, every
# node one hop from every other, settlement known instantly, folding
# continuous and uniform, a dedicated builder, and claims enough for inclusion
# because v1 had no bodies. `bench/v1_calibration.py` adds what configuration
# cannot -- the complete graph and the constant fold cost -- and measures what
# the replacement cost.
V1_EQUIVALENT = {
    "capacity": None,
    "link_capacity": None,
    "egress_queue_bytes": None,
    "prove_cost_basis": "deps",
    "proving_budget": 1e9,
    "relay_policy": "flood",
    "chain_notify": "instant",
    "proposer_mode": "dedicated",
    "proposer_id": 0,
    "inclusion_policy": "claim_only",
    "selection_strategy": "random_pair",
    "fold_plan": "wide",
    "aggregation_trigger": "when_ready",
}

# The bandwidth levers that raise the transaction rate a 25 Mbit/s uplink
# sustains: every lever `bench/levers.py` measures except `capped8`. That one
# saves no bytes and, once the rate is high enough for a fold to hold more
# than a few dozen inputs, a tree of capped folds no longer fits the proving
# budget of one interval, so the roots a node emits claim a fraction of what
# a wide fold claims and inclusion falls to a quarter -- measured in
# `bench/throughput.py`, and recorded in docs/results.md. The name is what
# the measurement found, on that fixture; the fixture is the caveat.
HIGH_THROUGHPUT = {
    "relay_policy": "announce_fetch",
    "wrapper_economy": "lean",
    "fold_threshold": "crossover",
    "wrapper_mode": "cheapest",
}

PRESETS = {
    "eip_faithful": EIP_FAITHFUL,
    "v1_equivalent": V1_EQUIVALENT,
    "high_throughput": HIGH_THROUGHPUT,
}


def names():
    return tuple(PRESETS)


def preset(name):
    """A fresh copy of the named preset, so a caller's overrides stay local."""
    try:
        return dict(PRESETS[name])
    except KeyError:
        raise KeyError(f"unknown preset {name!r}; one of {', '.join(PRESETS)}"
                       ) from None

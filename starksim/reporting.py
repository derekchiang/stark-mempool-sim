#!/usr/bin/env python3
"""Analytical queries and stable machine-readable reporting."""

import math

from starksim.costs import (BUILD_BLOCK, PROVE_COST_CIRCUIT, PROVE_RECURSIVE,
                            VERIFY_RECV, CircuitCost)


def _pearson(pairs):
    """Pearson correlation over (x, y) pairs, or None when undefined."""
    if len(pairs) < 3:
        return None
    mean_x = sum(x for x, _ in pairs) / len(pairs)
    mean_y = sum(y for _, y in pairs) / len(pairs)
    covariance = 0.0
    variance_x = 0.0
    variance_y = 0.0
    for x, y in pairs:
        dx = x - mean_x
        dy = y - mean_y
        covariance += dx * dy
        variance_x += dx * dx
        variance_y += dy * dy
    if variance_x == 0.0 or variance_y == 0.0:
        return None
    return covariance / (variance_x * variance_y) ** 0.5


def delay_distance_correlation(sim, reference_node=None):
    """Pearson correlation between entry distance and inclusion delay.

    Only included transactions contribute. Returns None when there is nothing
    to correlate or distance does not vary.
    """
    if reference_node is None:
        reference_node = sim.proposers.fixed_id
    distance = hop_distance_from(sim, reference_node)
    pairs = []
    for entry, _proposer, delay in sim.tx_records:
        if entry is None or distance[entry] is None:
            continue
        pairs.append((float(distance[entry]), delay))
    return _pearson(pairs)


def hop_distance_from(sim, origin):
    """Hops from `origin` to every node, by breadth-first search."""
    distance = [None] * sim.overlay.node_count
    distance[origin] = 0
    frontier = [origin]
    while frontier:
        following = []
        for node_id in frontier:
            for peer in sim.overlay.peers(node_id):
                if distance[peer] is None:
                    distance[peer] = distance[node_id] + 1
                    following.append(peer)
        frontier = following
    return distance


def residency_fraction(sim, obj):
    """Share of nodes holding `obj` right now, rather than cumulative reach."""
    known = 0
    for node in sim.nodes:
        if node.view.holds(obj):
            known += 1
    return known / len(sim.nodes)


def _require_object_present(sim, obj, what):
    """Refuse reach queries for this object after it leaves any view.

    A global departure guard would block an answer about one object because an
    unrelated object was evicted.
    """
    if obj.id in sim._departed:
        raise ValueError(
            f"{what} cannot be answered for object {obj.id}: it has left at "
            "least one view, so current holdings under-report the nodes that "
            "ever admitted it. Reconstruct reach from the trace.")


def _require_window_elapsed(sim, obj, window, what):
    """Refuse a window the run has not actually observed."""
    if window is None:
        return
    if sim.engine.now < obj.born + window:
        raise ValueError(
            f"{what} cannot be answered for a {window} window: the run has "
            f"only reached {sim.engine.now} and the object was born at "
            f"{obj.born}, so {obj.born + window} has not been observed. Run "
            "longer, or ask for a window that has elapsed.")


def admission_delays(sim, obj, window=None):
    """Per-node delays from `obj`'s birth to admission, ascending.

    Nodes that never admitted it are absent, as are admissions later than
    `window`. Giving every object the same window prevents birth age from
    becoming part of the propagation statistic.
    """
    _require_object_present(sim, obj, "admission_delays")
    _require_window_elapsed(sim, obj, window, "admission_delays")
    delays = []
    for node in sim.nodes:
        admitted_at = node.view.admitted_at(obj)
        if admitted_at is None:
            continue
        delay = admitted_at - obj.born
        if window is not None and delay > window:
            continue
        delays.append(delay)
    delays.sort()
    return delays


def coverage_fraction_within(sim, obj, window):
    """Share of nodes still holding `obj` after admission within `window`.

    This equals cumulative reach only while nothing retires objects. A final
    state cannot reconstruct admissions for an object that later left a view.
    """
    return len(admission_delays(sim, obj, window)) / len(sim.nodes)


def time_to_coverage(sim, obj, fraction, window=None):
    """Delay by which `fraction` of the nodes held `obj`, or None.

    None is right-censoring: the threshold was not reached in the observation
    window, not a large delay suitable for averaging.
    """
    needed = math.ceil(fraction * len(sim.nodes))
    delays = admission_delays(sim, obj, window)
    if len(delays) < needed:
        return None
    return delays[needed - 1]


def inclusion_by_distance(sim, reference_node):
    """Per hop distance from `reference_node`: (born, included) counts.

    Unlike a delay correlation, this retains transactions that never reached a
    block and therefore exposes distance-dependent exclusion.
    """
    distance = hop_distance_from(sim, reference_node)
    born = {}
    included = {}
    for leaf_index, entry in sim.entry_of.items():
        if entry is None or distance[entry] is None:
            continue
        hops = distance[entry]
        born[hops] = born.get(hops, 0) + 1
        if sim.chain.included >> leaf_index & 1:
            included[hops] = included.get(hops, 0) + 1
    table = {}
    for hops in sorted(born):
        table[hops] = (born[hops], included.get(hops, 0))
    return table


def egress_utilisation(sim, node_id, window_end=None):
    """Share of the run this node's uplink spent transmitting."""
    if window_end is None:
        window_end = sim.engine.now
    if window_end <= 0:
        return 0.0
    return sim.egress[node_id].busy_time_at(window_end) / window_end


def max_egress_depth(sim):
    return max(server.max_waiting for server in sim.egress)


def variant(sim):
    """What distinguishes one run from another.

    A run must remain attributable from its output alone, without consulting
    the invocation that produced it.
    """
    degrees = set(sim.overlay.degrees())
    if len(degrees) == 1:
        degree = degrees.pop()
    else:
        degree = -1
    latency = sim.overlay.latency_model
    identity = {
        "nodes": sim.overlay.node_count,
        "degree": degree,
        "latency_base": latency.base,
        "latency_jitter": latency.jitter_frac,
        "latency_seed": str(latency.seed),
        "capacity": -1.0 if sim.capacity is None else sim.capacity,
        "size_triple": sim.size_model.triple,
        "size_hash_ref": sim.size_model.hash_ref,
        "size_sig_witness": sim.size_model.sig_witness,
        "size_stark_witness": sim.size_model.stark_witness,
        "size_recursive_proof": sim.size_model.recursive_proof,
        "size_tx_body": sim.size_model.tx_body,
        "size_announce_overhead": sim.size_model.announce_overhead,
        "seen_limit": (-1 if sim.nodes[0].view.seen.limit is None
                       else sim.nodes[0].view.seen.limit),
        "candidate_limit": (-1 if sim.nodes[0].candidate_limit is None
                            else sim.nodes[0].candidate_limit),
        "coverage_cache_limit": (-1 if sim.arena.cache_limit is None
                                 else sim.arena.cache_limit),
        "egress_queue_bytes": (-1 if sim.egress_queue_bytes is None
                               else sim.egress_queue_bytes),
        "cpu_queue_jobs": (-1 if sim.cpu_queue_jobs is None
                           else sim.cpu_queue_jobs),
        "request_timeout": (-1.0 if sim.request_timeout is None
                            else sim.request_timeout),
        "link_capacity": (-1.0 if sim.link_capacity is None
                          else sim.link_capacity),
        "relay_policy": sim.relay_policy,
        "chain_notify": sim.notifier.kind,
        "proposer_mode": sim.proposers.mode,
        # Two fixed proposers on the same topology include different
        # transactions, so the identity is not complete without which node it
        # is. It is also read under every mode as the reference point for the
        # distance correlations, which is why it is never inert.
        "proposer_id": sim.proposers.fixed_id,
        "pin_own_aggregate": sim.pin_own_aggregate,
        "entry_nodes": len(sim.entry_nodes),
        "slot_length": sim.slot_length,
        "block_budget": sim.block_budget,
        "aggregation_interval": sim.aggregation_interval,
        "aggregation_trigger": sim.aggregation_trigger,
        "selection_strategy": sim.selection_strategy,
        "fold_plan": sim.fold_plan,
        "supersede_policy": sim.supersede_policy,
        "eviction_policy": sim.eviction_policy,
        "wrapper_economy": sim.wrapper_economy,
        "inclusion_policy": sim.inclusion_policy,
        "cpu_discipline": sim.cpu_discipline,
        "prove_cost_basis": sim.prove_cost_basis,
        "wrapper_mode": sim.wrapper_mode,
        "fold_threshold": sim.fold_threshold,
        "fold_amortize": sim.fold_amortize,
        "on_tick_overrun": sim.on_tick_overrun,
        "proving_budget": sim.proving_budget,
        "cost_verify_recv": sim.costs.base(VERIFY_RECV),
        "cost_prove_recursive": sim.costs.base(PROVE_RECURSIVE),
        "cost_build_block": sim.costs.base(BUILD_BLOCK),
        "cost_verify_recv_100": sim.costs.base(VERIFY_RECV, 100),
        "cost_prove_recursive_100": sim.costs.base(PROVE_RECURSIVE, 100),
        "cost_jitter": sim.costs.jitter_frac,
        # -1 where no composition pricing is configured, the sentinel for
        # "this run cannot answer this": the flat cost above is then the
        # whole story.
        "verify_sig_ms": (-1.0 if sim.verify_cost is None
                          else sim.verify_cost.per_signature),
        "verify_stark_ms": (-1.0 if sim.verify_cost is None
                            else sim.verify_cost.per_stark),
        "verify_message_ms": (-1.0 if sim.verify_cost is None
                              else sim.verify_cost.per_message),
        "seed": str(sim.seed),
    }
    identity.update(_circuit_identity(sim))
    return identity


# A `CircuitCost` is not identified by the scalar probes above. Those sample it
# at one and a hundred *nested proofs*, so two runs whose signature weight or
# trace cap differ by orders of magnitude -- deciding whether a fold is possible
# at all -- come out byte-identical in the variant, the CSV row and the trace
# header. A result row must identify the run that produced it, so every
# parameter that changes an outcome is recorded. `-1` where no circuit cost is
# in force, following the structural-absence convention.
CIRCUIT_FIELDS = ("ms_per_hash", "overhead_hashes", "sphincs_hashes",
                  "leanstark_hashes", "recursive_hashes",
                  "trace_rows_per_hash", "trace_cap")


def _circuit_identity(sim):
    prover = sim.costs.costs.get(PROVE_RECURSIVE)
    identity = {}
    for name in CIRCUIT_FIELDS:
        if isinstance(prover, CircuitCost):
            identity["circuit_" + name] = getattr(prover, name)
        else:
            identity["circuit_" + name] = -1
    return identity


def metrics(sim):
    """Everything measured, alongside the variant that produced it."""
    result = dict(variant(sim))
    blocks = sim.chain.blocks
    if blocks:
        per_block = sim.chain.included_count() / len(blocks)
    else:
        # No block was built, so transactions per block is 0/0 -- undefined, not
        # zero. Reporting zero says the proposer built blocks and put nothing in
        # them, which is a result; this configuration cannot answer the question.
        per_block = -1.0
    result.update({
        "slots_run": sim.slots_run,
        "arrived_txs": sim.arrived_txs,
        "included_txs": sim.chain.included_count(),
        "expired_txs": sim.chain.expired_count(),
        "pending_txs": (sim.arrived_txs - sim.chain.included_count()
                        - sim.chain.expired_count()),
        "wasted_block_coverage": sum(block.wasted().bit_count()
                                     for block in blocks),
        "discarded_after_work": sim.discarded_after_work,
        "pruned": sum(node.pruned for node in sim.nodes),
        "dropped_messages": sim.dropped_messages,
        "dropped_bytes": sim.dropped_bytes,
        "requests_timed_out": sim.requests_timed_out,
        "requests_retried": sim.requests_retried,
        "evicted": sum(node.evicted for node in sim.nodes),
        "superseded": sum(node.superseded for node in sim.nodes),
        "seen_forgotten": sum(node.view.seen.forgotten for node in sim.nodes),
        "coverage_cache_bytes": sim.arena.coverage_cache_bytes(),
        "coverage_cache_evictions": sim.arena.cache_evictions,
        "ticks_run": sim.ticks_run,
        "wrappers_emitted": sim.wrappers_emitted,
        "wrapper_sends_suppressed": sim.wrapper_sends_suppressed,
        "wrapper_refs_unresolved": sim.wrapper_refs_unresolved,
        "build_jobs_refused": sim.build_jobs_refused,
        "proofs_completed": sim.proofs_completed,
        "plans_abandoned": sim.plans_abandoned,
        "proof_jobs_cancelled": sim.proof_jobs_cancelled,
        # Only the circuit basis has a trace ceiling to exceed. Under a scalar
        # price nothing is refused because nothing is measured, and a zero there
        # would read as "no job came near the limit".
        "proof_jobs_over_capacity": (sim.proof_jobs_over_capacity
                                     if sim.prove_cost_basis == PROVE_COST_CIRCUIT
                                     else -1),
        "mode_zero_wrappers": sim.mode_zero_wrappers,
        "folds_deferred": sim.folds_deferred,
        "stale_proofs": sim.stale_proofs,
        "block_notices": sim.block_notices,
        "stale_deps_proved": (sim.stale_deps_proved
                              if sim.divergence_measurable() else -1),
        "txs_per_block": per_block,
        "delay_mean_slots": sim.delay_mean(),
        "delay_p95_slots": sim.delay_percentile(0.95),
        "messages_offered": sim.messages_offered,
        "messages_delivered": sim.messages_delivered,
        "duplicates": sim.duplicates,
        "wire_bytes": sim.total_wire_bytes(),
        "offered_bytes": sim.total_offered_bytes(),
        "ingress_bytes": sim.total_ingress_bytes(),
        "duplicate_bytes": sim.duplicate_bytes,
        "egress_utilisation": sim.mean_egress_utilisation(),
        "cpu_utilisation": sim.mean_cpu_utilisation(),
        "max_egress_depth": max_egress_depth(sim),
        "max_cpu_depth": sim.max_cpu_depth(),
        "unserved_wants": sim.unserved_wants,
    })
    return result


# Column order is the CSV contract: appending is safe, reordering is not.
# A value of -1 or -1.0 is a structural sentinel meaning this run cannot answer
# the field; it is not a measured negative value or a zero.
CSV_COLUMNS = [
    "nodes", "degree", "latency_base", "latency_jitter", "latency_seed",
    "capacity",
    "link_capacity", "relay_policy", "chain_notify", "proposer_mode",
    "slot_length", "block_budget", "aggregation_interval",
    "selection_strategy", "fold_plan", "on_tick_overrun", "proving_budget",
    "entry_nodes",
    "cost_verify_recv", "cost_prove_recursive",
    "cost_build_block", "cost_verify_recv_100", "cost_prove_recursive_100",
    "cost_jitter", "seed",
    "slots_run", "arrived_txs", "included_txs", "pending_txs", "txs_per_block",
    "delay_mean_slots", "delay_p95_slots",
    "messages_offered", "messages_delivered", "duplicates",
    "wire_bytes", "offered_bytes", "ingress_bytes", "duplicate_bytes",
    "egress_utilisation", "cpu_utilisation", "max_egress_depth",
    "max_cpu_depth", "unserved_wants",
    "expired_txs", "wasted_block_coverage", "discarded_after_work",
    "pruned", "dropped_messages", "dropped_bytes",
    "requests_timed_out", "requests_retried",
    "egress_queue_bytes", "cpu_queue_jobs", "request_timeout",
    "size_triple", "size_hash_ref", "size_sig_witness", "size_stark_witness",
    "size_recursive_proof", "size_tx_body", "size_announce_overhead",
    "seen_limit", "candidate_limit", "coverage_cache_limit",
    "evicted", "superseded", "seen_forgotten", "coverage_cache_bytes",
    "coverage_cache_evictions",
    "ticks_run", "wrappers_emitted", "proofs_completed", "plans_abandoned",
    "stale_proofs", "block_notices", "stale_deps_proved",
    "proof_jobs_cancelled", "supersede_policy", "wrapper_economy",
    "wrapper_sends_suppressed", "wrapper_refs_unresolved",
    "inclusion_policy", "cpu_discipline", "build_jobs_refused",
    "eviction_policy", "prove_cost_basis", "proof_jobs_over_capacity",
    "wrapper_mode", "mode_zero_wrappers", "fold_threshold",
    "folds_deferred",
    "aggregation_trigger", "pin_own_aggregate", "proposer_id",
    "circuit_ms_per_hash", "circuit_overhead_hashes", "circuit_sphincs_hashes",
    "circuit_leanstark_hashes", "circuit_recursive_hashes",
    "circuit_trace_rows_per_hash", "circuit_trace_cap",
    "verify_sig_ms", "verify_stark_ms", "verify_message_ms",
    "fold_amortize",
]


def csv_header():
    return ",".join(CSV_COLUMNS)


def csv_row(run_metrics):
    fields = []
    for column in CSV_COLUMNS:
        value = run_metrics[column]
        if isinstance(value, float):
            if math.isnan(value):
                fields.append("nan")
            else:
                fields.append("%.6f" % value)
        else:
            fields.append(str(value))
    return ",".join(fields)

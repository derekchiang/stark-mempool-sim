#!/usr/bin/env python3
"""Facade contracts that must survive the NetworkSim module split."""

import inspect
import io
import unittest

from starksim.arena import KIND_PQ, KIND_PRIVACY
from starksim.chain import ChainState
from starksim.costs import CostTable
from starksim.engine import Engine
from starksim.netsim import CSV_COLUMNS, NetworkSim, csv_row
from starksim.network import Overlay, SizeModel

from tests.support import two_node_overlay


CONSTRUCTOR_PARAMETERS = (
    "overlay", "seed", "size_model", "capacity", "link_capacity",
    "relay_policy", "proposer_mode", "proposer_id",
    "pin_own_aggregate",
    "engine", "chain",
    "chain_notify", "costs", "slot_length", "block_budget", "seen_limit",
    "candidate_limit", "coverage_cache_limit", "aggregation_interval",
    "aggregation_trigger",
    "selection_strategy", "fold_plan", "supersede_policy",
    "eviction_policy", "wrapper_economy", "inclusion_policy",
    "cpu_discipline", "prove_cost_basis", "wrapper_mode",
    "fold_threshold", "on_tick_overrun",
    "proving_budget", "entry_subset", "trace",
    "trace_focus", "trace_sample", "trace_counter_interval", "trace_follow",
    "trace_follow_rate", "trace_follow_seed", "egress_queue_bytes",
    "cpu_queue_jobs", "request_timeout",
)

EXPECTED_CSV_COLUMNS = [
    "nodes", "degree", "latency_base", "latency_jitter", "latency_seed",
    "capacity", "link_capacity", "relay_policy", "chain_notify",
    "proposer_mode", "slot_length", "block_budget", "aggregation_interval",
    "selection_strategy", "fold_plan", "on_tick_overrun",
    "proving_budget", "entry_nodes", "cost_verify_recv",
    "cost_prove_recursive", "cost_build_block", "cost_verify_recv_100",
    "cost_prove_recursive_100", "cost_jitter", "seed", "slots_run",
    "arrived_txs", "included_txs", "pending_txs", "txs_per_block",
    "delay_mean_slots", "delay_p95_slots", "messages_offered",
    "messages_delivered", "duplicates", "wire_bytes", "offered_bytes",
    "ingress_bytes", "duplicate_bytes", "egress_utilisation",
    "cpu_utilisation", "max_egress_depth", "max_cpu_depth",
    "unserved_wants", "expired_txs", "wasted_block_coverage",
    "discarded_after_work", "pruned", "dropped_messages", "dropped_bytes",
    "requests_timed_out", "requests_retried", "egress_queue_bytes",
    "cpu_queue_jobs", "request_timeout", "size_triple", "size_hash_ref",
    "size_sig_witness", "size_stark_witness", "size_recursive_proof",
    "size_tx_body", "size_announce_overhead", "seen_limit",
    "candidate_limit", "coverage_cache_limit", "evicted", "superseded",
    "seen_forgotten", "coverage_cache_bytes", "coverage_cache_evictions",
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
]

EXPECTED_CSV_ROW = (
    "2,1,1.000000,0.000000,0,-1.000000,-1.000000,flood,instant,"
    "fixed,4000.000000,5,1000.000000,eip_priority,wide,fifo,"
    "1000.000000,2,0.000000,0.000000,0.000000,0.000000,"
    "0.000000,0.000000,7,0,1,0,1,-1.000000,nan,nan,1,1,0,"
    "3168.000000,3168,3168,0,0.000000,0.000000,0,0,0,0,0,0,0,0,0,0,"
    "0,-1,-1,-1.000000,96,32,2560,262144,262144,512,0,-1,-1,-1,0,0,"
    "0,0,0,0,0,0,0,0,0,-1,0,keep_all,full,0,0,body_required,fifo,0,"
    "usefulness,deps,-1,recursive,0,always,0,spread,True,0,"
    "-1,-1,-1,-1,-1,-1,-1"
)

VARIANT_KEYS = {
    "nodes", "degree", "latency_base", "latency_jitter", "latency_seed",
    "capacity", "size_triple", "size_hash_ref", "size_sig_witness",
    "size_stark_witness", "size_recursive_proof", "size_tx_body",
    "size_announce_overhead", "seen_limit", "candidate_limit",
    "coverage_cache_limit", "egress_queue_bytes", "cpu_queue_jobs",
    "request_timeout", "link_capacity", "relay_policy", "chain_notify",
    "proposer_mode", "entry_nodes", "slot_length",
    "block_budget", "aggregation_interval",
    "selection_strategy", "fold_plan", "supersede_policy", "eviction_policy",
    "wrapper_economy", "inclusion_policy", "cpu_discipline",
    "prove_cost_basis", "wrapper_mode", "fold_threshold",
    "on_tick_overrun",
    "proving_budget",
    "cost_verify_recv", "cost_prove_recursive", "cost_build_block",
    "cost_verify_recv_100", "cost_prove_recursive_100", "cost_jitter", "seed",
    "aggregation_trigger", "pin_own_aggregate", "proposer_id",
    "circuit_ms_per_hash", "circuit_overhead_hashes", "circuit_sphincs_hashes",
    "circuit_leanstark_hashes", "circuit_recursive_hashes",
    "circuit_trace_rows_per_hash", "circuit_trace_cap",
}

METRIC_ONLY_KEYS = {
    "slots_run", "arrived_txs", "included_txs", "expired_txs", "pending_txs",
    "wasted_block_coverage", "discarded_after_work", "pruned",
    "dropped_messages", "dropped_bytes", "requests_timed_out",
    "requests_retried", "evicted", "superseded", "seen_forgotten",
    "coverage_cache_bytes", "coverage_cache_evictions", "ticks_run",
    "wrappers_emitted", "wrapper_sends_suppressed", "wrapper_refs_unresolved",
    "build_jobs_refused", "proofs_completed", "plans_abandoned",
    "proof_jobs_cancelled", "stale_proofs", "block_notices",
    "stale_deps_proved", "txs_per_block", "delay_mean_slots",
    "delay_p95_slots", "messages_offered", "messages_delivered", "duplicates",
    "wire_bytes", "offered_bytes", "ingress_bytes", "duplicate_bytes",
    "egress_utilisation", "cpu_utilisation", "max_egress_depth",
    "max_cpu_depth", "unserved_wants", "proof_jobs_over_capacity",
    "mode_zero_wrappers", "folds_deferred",
}

EXPECTED_TRACE = (
    '{"e":"cfg","nodes":2,"degree":1,"latency_base":1.0,'
    '"latency_jitter":0.0,"latency_seed":"0","capacity":-1.0,'
    '"size_triple":96,"size_hash_ref":32,"size_sig_witness":2560,'
    '"size_stark_witness":262144,"size_recursive_proof":262144,'
    '"size_tx_body":512,"size_announce_overhead":0,"seen_limit":-1,'
    '"candidate_limit":-1,"coverage_cache_limit":-1,'
    '"egress_queue_bytes":-1,"cpu_queue_jobs":-1,"request_timeout":-1.0,'
    '"link_capacity":-1.0,"relay_policy":"flood","chain_notify":"instant",'
    '"proposer_mode":"fixed","proposer_id":0,'
    '"pin_own_aggregate":true,'
    '"entry_nodes":2,'
    '"slot_length":4000.0,"block_budget":5,"aggregation_interval":1000.0,'
    '"aggregation_trigger":"spread",'
    '"selection_strategy":"eip_priority",'
    '"fold_plan":"wide","supersede_policy":"keep_all",'
    '"eviction_policy":"usefulness","wrapper_economy":"full",'
    '"inclusion_policy":"body_required","cpu_discipline":"fifo",'
    '"prove_cost_basis":"deps","wrapper_mode":"recursive",'
    '"fold_threshold":"always",'
    '"on_tick_overrun":"fifo",'
    '"proving_budget":1000.0,"cost_verify_recv":0.0,'
    '"cost_prove_recursive":0.0,"cost_build_block":0.0,'
    '"cost_verify_recv_100":0.0,"cost_prove_recursive_100":0.0,'
    '"cost_jitter":0.0,"seed":"7","circuit_ms_per_hash":-1,'
    '"circuit_overhead_hashes":-1,"circuit_sphincs_hashes":-1,'
    '"circuit_leanstark_hashes":-1,"circuit_recursive_hashes":-1,'
    '"circuit_trace_rows_per_hash":-1,"circuit_trace_cap":-1,'
    '"trace_schema":3,"trace_sample":1,'
    '"trace_counter_interval":1000.0,"topology":[[0,1]],'
    '"trace_follow":{"mode":"none"},"trace_focus":"all"}\n'
    '{"e":"cnt","n":0,"t":0.0,"seg":"segment_start","eb":0.0,'
    '"cb":0.0,"ob":0,"sb":0,"ib":0,"obk":{},"sbk":{},"ibk":{},'
    '"idb":0,"eq":0,"eqb":0,"cq":0,"dr":0,"drb":0,"held":0,'
    '"stale":-1}\n'
    '{"e":"cnt","n":1,"t":0.0,"seg":"segment_start","eb":0.0,'
    '"cb":0.0,"ob":0,"sb":0,"ib":0,"obk":{},"sbk":{},"ibk":{},'
    '"idb":0,"eq":0,"eqb":0,"cq":0,"dr":0,"drb":0,"held":0,'
    '"stale":-1}\n'
    '{"e":"arr","n":0,"t":0.0,"o":0,"tx":0,"k":"q","fee":0.0}\n'
    '{"e":"snd","n":0,"t":0.0,"to":1,"k":"object","b":3168}\n'
    '{"e":"adm","n":1,"t":1.0,"o":0,"born":0.0}\n'
    '{"e":"cnt","n":0,"t":1.0,"seg":"segment_end","eb":0.0,'
    '"cb":0.0,"ob":3168,"sb":3168,"ib":0,'
    '"obk":{"object":3168},"sbk":{"object":3168},"ibk":{},"idb":0,'
    '"eq":0,"eqb":0,"cq":0,"dr":0,"drb":0,"held":1,"stale":-1}\n'
    '{"e":"cnt","n":1,"t":1.0,"seg":"segment_end","eb":0.0,'
    '"cb":0.0,"ob":0,"sb":0,"ib":3168,"obk":{},"sbk":{},'
    '"ibk":{"object":3168},"idb":0,"eq":0,"eqb":0,"cq":0,"dr":0,'
    '"drb":0,"held":1,"stale":-1}\n'
    '{"e":"seg","t":1.0,"start":0.0,"why":"drained","events":3}\n'
)


def fixed_run(trace=None):
    sim = NetworkSim(two_node_overlay(1.0), seed=7, trace=trace)
    sim.schedule_birth(0.0, KIND_PQ, node_id=0)
    sim.run()
    return sim


class TestNetworkSimFacade(unittest.TestCase):
    def test_constructor_keyword_contract(self):
        self.assertEqual(tuple(inspect.signature(NetworkSim).parameters),
                         CONSTRUCTOR_PARAMETERS)
        trace = io.StringIO()
        sim = NetworkSim(
            two_node_overlay(), seed=9, size_model=SizeModel(), capacity=1000.0,
            link_capacity=500.0, relay_policy="announce_fetch",
            proposer_mode="rotating", proposer_id=1, engine=Engine(),
            chain=ChainState(), chain_notify="overlay", costs=CostTable(),
            slot_length=500.0, block_budget=3, seen_limit=20,
            candidate_limit=10, coverage_cache_limit=100,
            aggregation_interval=100.0, aggregation_trigger="aligned",
            selection_strategy="fee_density", fold_plan="capped8",
            supersede_policy="newest_if_covered", eviction_policy="fee_value",
            wrapper_economy="lean", inclusion_policy="claim_only",
            cpu_discipline="priority", prove_cost_basis="deps",
            on_tick_overrun="abandon", proving_budget=80.0,
            entry_subset=1, trace=trace,
            trace_focus=[0], trace_sample=2, trace_counter_interval=50.0,
            trace_follow=[0], trace_follow_rate=0.0, trace_follow_seed="fixed",
            egress_queue_bytes=10000, cpu_queue_jobs=5, request_timeout=200.0)
        self.assertEqual(sim.variant()["selection_strategy"],
                         "fee_density")

    def test_unknown_policy_values_are_rejected(self):
        invalid = {
            "relay_policy": "sideways", "proposer_mode": "sideways",
            "chain_notify": "sideways", "aggregation_trigger": "sideways",
            "supersede_policy": "sideways", "eviction_policy": "sideways",
            "wrapper_economy": "sideways", "inclusion_policy": "sideways",
            "cpu_discipline": "sideways", "prove_cost_basis": "sideways",
            "on_tick_overrun": "sideways",
        }
        for keyword, value in invalid.items():
            with self.subTest(keyword=keyword):
                with self.assertRaises((KeyError, ValueError)):
                    NetworkSim(two_node_overlay(), **{keyword: value})
        # Registry-backed names are refused at construction like the string
        # modes above, so a typo blames the caller rather than the first tick
        # that happened to have work.
        for keyword in ("selection_strategy", "fold_plan"):
            with self.subTest(keyword=keyword):
                with self.assertRaises(KeyError):
                    NetworkSim(two_node_overlay(), **{keyword: "sideways"})

    def test_csv_contract_is_deterministic_and_ordered(self):
        self.assertEqual(CSV_COLUMNS, EXPECTED_CSV_COLUMNS)
        first = csv_row(fixed_run().metrics())
        second = csv_row(fixed_run().metrics())
        self.assertEqual(first, EXPECTED_CSV_ROW)
        self.assertEqual(second, EXPECTED_CSV_ROW)

    def test_variant_and_metric_key_sets_are_complete(self):
        sim = fixed_run()
        self.assertEqual(set(sim.variant()), VARIANT_KEYS)
        self.assertEqual(set(sim.metrics()), VARIANT_KEYS | METRIC_ONLY_KEYS)

    def test_full_trace_is_byte_identical(self):
        trace = io.StringIO()
        fixed_run(trace)
        self.assertEqual(trace.getvalue(), EXPECTED_TRACE)

    def test_same_timestamp_event_order_is_stable(self):
        sim = NetworkSim(two_node_overlay(0.0), seed=1, aggregation_trigger="aligned",
                         aggregation_interval=100.0, slot_length=100.0)
        observed = []
        sim.engine.after_event(
            lambda kind: observed.append((kind, sim.engine.now)))
        sim.schedule_birth(100.0, KIND_PQ, node_id=0)
        sim.schedule_ticks(1)
        sim.schedule_slots(1)
        sim.run()
        boundary = [kind for kind, time in observed
                    if time == 100.0 and kind in {"birth", "tick", "slot"}]
        self.assertEqual(boundary, ["birth", "tick", "tick", "slot"])

    def test_saturated_accounting_is_conserved(self):
        adjacency = [[1, 2, 3], [0], [0], [0]]
        sim = NetworkSim(Overlay.from_adjacency(adjacency, base_latency=1.0),
                         seed=1, capacity=1000.0,
                         egress_queue_bytes=300_000)
        for _ in range(20):
            sim.schedule_birth(0.0, KIND_PRIVACY, node_id=0)
        sim.run()
        self.assertGreater(sim.dropped_bytes, 0)
        self.assertEqual(sim.total_offered_bytes(), sim.total_sent_bytes())
        self.assertEqual(sim.total_sent_bytes(), sim.total_ingress_bytes())
        self.assertEqual(sim.messages_offered, sim.messages_delivered)
        self.assertEqual(sim.messages_delivered, sum(sim.ingress_messages))
        self.assertEqual(sim.dropped_messages,
                         sum(server.dropped for server in sim.egress))
        self.assertEqual(sim.dropped_bytes,
                         sum(server.dropped_weight for server in sim.egress))
        self.assertEqual(sim.dropped_bytes, sum(sim.dropped_bytes_by_node))


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""Receive verification priced by what a delivery carries."""

import unittest

from starksim.arena import KIND_PQ, KIND_PRIVACY
from starksim.costs import CostTable, NativeVerifyCost
from starksim.netsim import NetworkSim, csv_row
from tests.support import make_sim, two_node_overlay


class TestNativeVerifyCost(unittest.TestCase):
    def test_a_message_is_priced_by_its_evidence(self):
        cost = NativeVerifyCost(per_signature=0.05, per_stark=15.0,
                                per_message=0.5)
        self.assertEqual(cost.message(0, 0), 0.5)
        self.assertEqual(cost.message(3, 0), 0.5 + 0.15)
        self.assertEqual(cost.message(0, 2), 0.5 + 30.0)
        self.assertEqual(cost.message(3, 2), 0.5 + 0.15 + 30.0)

    def test_a_negative_price_is_refused(self):
        for kwargs in ({"per_signature": -1, "per_stark": 1},
                       {"per_signature": 1, "per_stark": -1},
                       {"per_signature": 1, "per_stark": 1,
                        "per_message": -1}):
            with self.assertRaises(ValueError):
                NativeVerifyCost(**kwargs)


class TestDispatch(unittest.TestCase):
    def test_without_a_verify_cost_the_flat_table_is_charged(self):
        sim = make_sim(costs=CostTable(verify_recv=3.0))
        self.assertEqual(sim._verify_duration(0, 1, 7), 3.0)
        self.assertEqual(sim._verify_duration(5, 0, 1), 3.0)

    def test_with_a_verify_cost_the_composition_is_charged(self):
        sim = make_sim(costs=CostTable(verify_recv=3.0),
                       verify_cost=NativeVerifyCost(per_signature=1.0,
                                                    per_stark=100.0))
        # The flat cost and the fallback count must both be ignored.
        self.assertEqual(sim._verify_duration(2, 0, 7), 2.0)
        self.assertEqual(sim._verify_duration(0, 1, 7), 100.0)


class TestDeliveries(unittest.TestCase):
    """End-to-end: what each delivery kind pays on the receiver's CPU.

    One birth relayed over a two-node overlay; the receiver's CPU busy time
    at the end of the run is the verification it paid, since nothing else
    runs on an idle receiver that never folds (proving costs zero here).
    """

    def receiver_busy(self, kind, verify_cost):
        sim = make_sim(costs=CostTable(),  # verify_recv 0: blocks stay free
                       verify_cost=verify_cost,
                       aggregation_trigger="aligned",
                       proving_budget=1e9)
        sim.schedule_birth(0.0, kind, node_id=0)
        sim.run(until=2000.0)
        return sim.cpu[1].busy_time_at(sim.engine.now)

    def test_a_signature_body_pays_the_signature_price(self):
        cost = NativeVerifyCost(per_signature=7.0, per_stark=1000.0)
        self.assertEqual(self.receiver_busy(KIND_PQ, cost), 7.0)

    def test_a_privacy_body_pays_the_stark_price(self):
        cost = NativeVerifyCost(per_signature=7.0, per_stark=1000.0)
        self.assertEqual(self.receiver_busy(KIND_PRIVACY, cost), 1000.0)

    def delivered_wrapper_duration(self, mode):
        """CPU time node 0 pays to verify one constructed wrapper delivery.

        The aggregate claims two signature leaves and one privacy leaf, so a
        mode 0 wrapper ships those three witnesses while a mode 1 wrapper
        ships one recursive proof.
        """
        from starksim.payload import (SizeBreakdown, WireMessage, WrapperCore,
                                      WrapperPayload)
        sim = make_sim(costs=CostTable(),
                       verify_cost=NativeVerifyCost(per_signature=1.0,
                                                    per_stark=100.0),
                       proposer_id=0)
        leaves = [sim.arena.new_leaf(kind, 0.0)
                  for kind in (KIND_PQ, KIND_PQ, KIND_PRIVACY)]
        aggregate = sim.arena.new_aggregate(leaves, 1.0, 1, claimed=0b111)
        core = WrapperCore(wrapper_id=1, proof_object_id=aggregate.id,
                           creator=1, tick=0, mode=mode, tx_mask=0b111,
                           dep_count=3)
        message = WireMessage(sender=1, receiver=0,
                              payload=WrapperPayload(core=core,
                                                     body_mask=0b111,
                                                     size=SizeBreakdown()))
        sim._on_deliver(sim.engine, message)
        sim.run(until=10_000.0)  # let the verification job complete
        return sim.cpu[0].busy_time_at(sim.engine.now)

    def test_a_mode_zero_wrapper_pays_for_every_witness_it_ships(self):
        self.assertEqual(self.delivered_wrapper_duration(0),
                         2 * 1.0 + 100.0)

    def test_a_recursive_wrapper_pays_one_stark(self):
        self.assertEqual(self.delivered_wrapper_duration(1), 100.0)

    def test_an_aggregate_body_pays_one_stark(self):
        # Its dependency triples are data; the recursive proof is what gets
        # verified, however many transactions it claims.
        from starksim.payload import WireMessage
        sim = make_sim(costs=CostTable(),
                       verify_cost=NativeVerifyCost(per_signature=1.0,
                                                    per_stark=100.0),
                       proposer_id=0)
        leaves = [sim.arena.new_leaf(KIND_PQ, 0.0) for _ in range(3)]
        aggregate = sim.arena.new_aggregate(leaves, 1.0, 1, claimed=0b111)
        payload = sim.size_model.object_payload(aggregate, dep_count=3)
        sim._on_deliver(sim.engine, WireMessage(sender=1, receiver=0,
                                                payload=payload))
        sim.run(until=10_000.0)
        self.assertEqual(sim.cpu[0].busy_time_at(sim.engine.now), 100.0)

    def test_a_block_notice_pays_one_stark(self):
        # The notice carries the block itself, which the size model prices at
        # one recursive proof -- settlement is believed only after paying to
        # check it. `chain_notify="overlay"` is what makes block messages
        # exist at all.
        sim = make_sim(costs=CostTable(),
                       verify_cost=NativeVerifyCost(per_signature=1.0,
                                                    per_stark=100.0),
                       chain_notify="overlay", proposer_id=0)
        sim.schedule_birth(0.0, KIND_PQ, node_id=1)
        sim.schedule_slots(1)
        sim.run(until=20_000.0)
        # Node 0 (the proposer) received one signature body; node 1 received
        # the block notice for the block that included its transaction.
        self.assertEqual(sim.cpu[0].busy_time_at(sim.engine.now), 1.0)
        self.assertEqual(sim.cpu[1].busy_time_at(sim.engine.now), 100.0)

    def test_a_duplicate_delivery_pays_nothing(self):
        # Duplicate suppression runs before the charge, so the second copy
        # of a flooded body costs no CPU -- including the per-verification
        # overhead, which is not a per-message toll.
        from starksim.network import Overlay
        triangle = Overlay.from_adjacency([[1, 2], [0, 2], [0, 1]],
                                          base_latency=10.0)
        sim = make_sim(overlay=triangle,
                       costs=CostTable(),
                       verify_cost=NativeVerifyCost(per_signature=3.0,
                                                    per_stark=0.0,
                                                    per_message=0.5))
        sim.schedule_birth(0.0, KIND_PQ, node_id=0)
        sim.run(until=10_000.0)
        self.assertGreater(sim.duplicates, 0)
        for receiver in (1, 2):
            self.assertEqual(sim.cpu[receiver].busy_time_at(sim.engine.now),
                             3.5)


class TestIdentity(unittest.TestCase):
    def test_the_variant_carries_the_prices_or_the_sentinel(self):
        flat = make_sim()
        self.assertEqual(flat.variant()["verify_sig_ms"], -1.0)
        self.assertEqual(flat.variant()["verify_stark_ms"], -1.0)
        self.assertEqual(flat.variant()["verify_message_ms"], -1.0)
        priced = make_sim(verify_cost=NativeVerifyCost(
            per_signature=0.1, per_stark=15.5, per_message=0.25))
        self.assertEqual(priced.variant()["verify_sig_ms"], 0.1)
        self.assertEqual(priced.variant()["verify_stark_ms"], 15.5)
        self.assertEqual(priced.variant()["verify_message_ms"], 0.25)
        # And the CSV row serializes them, so a published sweep attributes it.
        # The trailing column is fold_amortize at its default.
        row = csv_row(priced.metrics())
        self.assertTrue(row.endswith("0.100000,15.500000,0.250000,1.000000"))

    def test_degenerate_prices_reproduce_the_flat_run_exactly(self):
        # per_message alone equals a flat verify_recv with zero jitter, so
        # the two configurations must produce identical measurements: the
        # dispatch adds machinery, never behaviour, at that point.
        def run(**kwargs):
            sim = make_sim(overlay=two_node_overlay(10.0),
                           aggregation_trigger="aligned",
                           proving_budget=1e9, **kwargs)
            for t, kind in ((0.0, KIND_PQ), (50.0, KIND_PRIVACY)):
                sim.schedule_birth(t, kind, node_id=0)
            sim.schedule_ticks(2)
            sim.schedule_slots(1)
            sim.run()
            metrics = sim.metrics()
            for key in ("verify_sig_ms", "verify_stark_ms",
                        "verify_message_ms", "cost_verify_recv",
                        "cost_verify_recv_100"):
                metrics.pop(key)
            return metrics
        flat = run(costs=CostTable(verify_recv=2.0))
        priced = run(costs=CostTable(),
                     verify_cost=NativeVerifyCost(per_signature=0.0,
                                                  per_stark=0.0,
                                                  per_message=2.0))
        # Blocks pay the flat table in both worlds; zero it there so the
        # comparison isolates bodies and wrappers.
        self.assertEqual(flat, priced)


if __name__ == "__main__":
    unittest.main()

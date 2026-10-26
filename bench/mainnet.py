#!/usr/bin/env python3
"""
A mainnet-shaped workload: 12-second slots, a mixed transaction population,
and proving priced by measured leanVM-b cycle counts.

No other bench mixes transaction kinds on a priced wire: `bench.propagation`
measures each kind separately, and `bench.v1_calibration` mixes two streams
only on v1's free-bytes model. Mainnet would not be so tidy — bare
post-quantum signatures and privacy transactions share one mempool, and the
in-circuit price of a privacy witness is the one weight no measurement
covers. This bench mixes the two kinds at a stated share and sweeps the
unmeasured quantities over their bands, so a conclusion can be read as
"holds across the band" or "decided by a number nobody has yet".

Calibration, from leanEthereum/leanVM-b (retrieved 2026-08-30, Mac M4 Max),
kept as exact quotients of the published measurements: verifying one
leanSPHINCS signature in-circuit costs 2 677 883 / 245 cycles (~10 930),
verifying one nested recursive proof costs 746 499 / 2 cycles (~373 250),
and one 2-to-1 fold proves in 417 ms, which anchors cycles to milliseconds.
That single linear scale is itself uncertain: applied to the measured
SPHINCS aggregation it predicts 1 496 ms where 1 194 ms was measured, so
the two measured circuits disagree by 25% on milliseconds per cycle, and
absolute durations here carry at least that error. `--anchor-ms` is where
scale uncertainty lives — a larger anchor stands for a slower machine, and
417 ms is a fast desktop.

`--calibration leanmultisig` replaces those weights with proving times
measured on leanMultisig dd51f0f (Apple M4 Max, `--log-inv-rate 2`): 58 ms
per fold, 90 ms per nested proof and 1.8 ms per leanSPHINCS signature, from
folds of 1-16 children and of two children plus 16-256 raw signatures. Its
2-to-1 fold is 238 ms; leanMultisig has no leanSTARK-witness input, so
`--leanstark-factor` stays a multiple of the nested-proof weight.

The leanSTARK witness weight is NOT measured anywhere; `--leanstark-factor`
expresses it as a multiple of the nested-proof weight. The default band of
1-2 reflects that a WHIR-family verifier's work grows slowly with the size
of the proven statement — an argument, not a measurement.

Receive verification is priced by what each delivery carries
(`NativeVerifyCost`): a signature body pays `--verify-sig-ms`, anything
STARK-class — a privacy body, an aggregate, a mode 1 wrapper, a block
notice — pays `--verify-stark-ms`, a mode 0 wrapper pays for every witness
it ships, and announces and wants are free. Both prices are sweep axes.
The STARK price extrapolates leanVM-b's one measured native verification
(14.1-17.1 ms for one proof size) to every STARK-class object; 15.6 is the
midpoint. The signature price has no measurement at all, and a mode 0
wrapper multiplies it by every signature it carries, so it is swept rather
than declared negligible. Under the default `chain_notify="instant"` no
block messages exist, so no block verification is charged.

    python -m bench.mainnet
    python -m bench.mainnet --tps 5,15,50 --privacy-share 0.0,0.2,0.5
    python -m bench.mainnet --leanstark-factor 1,2 --anchor-ms 417,1000
    python -m bench.mainnet --verify-stark-ms 14.1,17.1 --verify-sig-ms 0.1,0.9
    python -m bench.mainnet --presets high_throughput,high_throughput+fold_plan=capped8

Deliberate omissions, so a row is not misread: no trace ceiling is
modelled — the EIP's 2**24 is KoalaBear rows on a different stack and
leanVM-b states no cycle limit, so a cap here would be a threshold made of
mismatched units. Arrivals are uniformly spaced: rate is the realism
dimension, burstiness is not modelled. Fees cycle 0-4 so `eip_priority`
has an ordering to act on; the cycle is recorded with each run.
"""

import argparse
import math
import random
import statistics
import sys

from bench.common import (configuration, emit_json_record, print_table_header,
                          random_regular_overlay, schedule_horizon,
                          workload_parameters)
from starksim.arena import KIND_AGGREGATE, KIND_PQ, KIND_PRIVACY
from starksim.costs import CircuitCost, CostTable, NativeVerifyCost
from starksim.netsim import NetworkSim
from starksim.network import SizeModel, bytes_per_ms

# leanVM-b's measured verification work, in VM cycles, as exact quotients of
# the published measurements (provenance above): a rounded weight would also
# shift the anchor's denominator.
LEANVM_B_SPHINCS_CYCLES = 2_677_883 / 245
LEANVM_B_RECURSIVE_CYCLES = 746_499 / 2
LEANVM_B_PAIRWISE_MS = 417.0

# leanMultisig at dd51f0f, measured on an Apple M4 Max (16 cores) with
# `cargo leanvm --log-inv-rate 2`, in milliseconds. Folds of 1-16 child
# proofs fit 58 ms + 90 ms per child; raw leanSPHINCS signatures folded in
# beside two children add 1.8 ms each. The weights are milliseconds rather
# than cycles because cycles undercount signature work: a child is ~123
# signatures' worth of cycles but ~50 signatures' worth of proving time.
# The fixed term is what a cycle-anchored calibration has no place for.
LEANMULTISIG_OVERHEAD_MS = 58.0
LEANMULTISIG_SPHINCS_MS = 1.8
LEANMULTISIG_RECURSIVE_MS = 90.0
LEANMULTISIG_PAIRWISE_MS = (LEANMULTISIG_OVERHEAD_MS
                            + 2 * LEANMULTISIG_RECURSIVE_MS)

# Each calibration: (overhead, signature, nested-proof weight, the 2-to-1
# fold its own measurement implies). The weights only fix ratios;
# `anchored_on_pairwise_fold` attaches the millisecond scale.
CALIBRATIONS = {
    "leanvm-b": (0.0, LEANVM_B_SPHINCS_CYCLES, LEANVM_B_RECURSIVE_CYCLES,
                 LEANVM_B_PAIRWISE_MS),
    "leanmultisig": (LEANMULTISIG_OVERHEAD_MS, LEANMULTISIG_SPHINCS_MS,
                     LEANMULTISIG_RECURSIVE_MS, LEANMULTISIG_PAIRWISE_MS),
}

# Fields this bench derives from its own flags or its calibration. A preset
# (or a `+field=value` override) reaching one of these would relabel the run
# without changing its record -- the header printing an uplink the run does
# not use, or the topology built from a different seed than the variant
# names -- so such presets are refused up front. `prove_cost_basis` is
# deliberately absent: a preset naming another basis is overridden by
# design, because the calibration is the bench's subject and the comparison
# across presets must stay paired.
OWNED_FIELDS = ("seed", "capacity", "link_capacity", "size_model", "costs",
                "verify_cost", "egress_queue_bytes", "slot_length",
                "proving_budget")


def mixed_kinds(transactions, privacy_share, seed):
    """The kind of each born transaction, a function of the seed alone.

    The privacy count is `round(share * transactions)` rather than drawn per
    birth, so the realised mix cannot drift from the label on a short run --
    though the rounding itself can: the caller validates the share and the
    run's record carries the realised count. Only the interleaving is
    random, from a dedicated stream.
    """
    privacy = int(round(transactions * privacy_share))
    kinds = [KIND_PRIVACY] * privacy + [KIND_PQ] * (transactions - privacy)
    random.Random(f"{seed}:mix").shuffle(kinds)
    return kinds


def calibrated_prover(anchor_ms, leanstark_factor, calibration="leanvm-b"):
    # Weights are cycles (leanvm-b) or milliseconds (leanmultisig), not
    # hashes; `anchored_on_pairwise_fold` divides the anchor by a 2-to-1
    # fold's total, so the unit cancels. The infinite trace cap records "no
    # ceiling known" (docstring: the EIP's ceiling belongs to a different
    # stack), and rows-per-unit 1 records only the unit -- with no cap it
    # prices nothing.
    overhead, signature, recursive, _ = CALIBRATIONS[calibration]
    return CircuitCost.anchored_on_pairwise_fold(
        anchor_ms,
        overhead_hashes=overhead,
        sphincs_hashes=signature,
        leanstark_hashes=recursive * leanstark_factor,
        recursive_hashes=recursive,
        trace_rows_per_hash=1.0, trace_cap=math.inf)


def mean_root_claim(sim):
    """Mean transactions claimed per root aggregate.

    A root is an aggregate never taken as a fold input, so this is the
    width selection actually achieved: the tell for a binding proving
    budget (docs/pitfalls.md). Inputs-per-fold would not serve -- a binary
    plan sits at two by construction however wide its root's claim is.
    """
    claims = [sim.arena.claim(obj).bit_count()
              for obj in sim.arena.objects.values()
              if obj.kind == KIND_AGGREGATE and obj.consumed == 0]
    if not claims:
        return 0.0
    return statistics.fmean(claims)


def one_run(args, name, transactions, privacy_share, leanstark_factor,
            anchor_ms, sig_ms, stark_ms, seed):
    overlay = random_regular_overlay(args.nodes, args.degree, args.latency,
                                     seed)
    settings = dict(seed=seed, capacity=bytes_per_ms(args.uplink_mbps),
                    size_model=SizeModel(),
                    costs=CostTable(
                        prove_recursive=calibrated_prover(
                            anchor_ms, leanstark_factor, args.calibration),
                        seed=seed),
                    verify_cost=NativeVerifyCost(sig_ms, stark_ms),
                    egress_queue_bytes=args.egress_bytes or None,
                    slot_length=args.slot_length)
    settings.update(configuration(name))
    settings["prove_cost_basis"] = "circuit"
    if args.proving_budget is not None:
        settings["proving_budget"] = args.proving_budget
    sim = NetworkSim(overlay, **settings)

    window = args.slots * args.slot_length
    spacing = window / max(1, transactions)
    kinds = mixed_kinds(transactions, privacy_share, seed)
    for index, kind in enumerate(kinds):
        sim.schedule_birth(index * spacing, kind, fee=float(index % 5))
    horizon = window + args.drain_slots * args.slot_length
    schedule_horizon(sim, horizon)
    # Utilisation is read at the end of the birth window: read at the
    # horizon it would count the drain's idle time in its denominator and
    # fall as the drain grows (docs/pitfalls.md: a statistic can measure its
    # own procedure). Inclusion and delay are read after the drain.
    sim.run(until=window)
    cpu_load_window = sim.mean_cpu_utilisation()
    sim.run(until=horizon)

    metrics = sim.metrics()
    outcome = {
        "offered": metrics["offered_bytes"],
        "wire": metrics["wire_bytes"],
        "included": metrics["included_txs"],
        "arrived": metrics["arrived_txs"],
        "delay": metrics["delay_mean_slots"],
        "cpu_load_window": cpu_load_window,
        "proofs": metrics["proofs_completed"],
        "root_claim": mean_root_claim(sim),
    }
    return outcome, sim


def mean_of(runs, field):
    return statistics.fmean(run[field] for run in runs)


def floats(text):
    return [float(piece) for piece in text.split(",")]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nodes", type=int, default=40)
    parser.add_argument("--degree", type=int, default=6)
    parser.add_argument("--uplink-mbps", type=float, default=25.0)
    parser.add_argument("--latency", type=float, default=100.0)
    parser.add_argument("--slot-length", type=float, default=12000.0,
                        help="ms per slot; mainnet-shaped is 12000")
    parser.add_argument("--slots", type=int, default=5,
                        help="slots over which transactions are born")
    parser.add_argument("--drain-slots", type=int, default=2,
                        help="quiet slots after the load, for inclusion")
    parser.add_argument("--tps", default="15",
                        help="transactions per second, one sweep value each")
    parser.add_argument("--privacy-share", default="0.0,0.2",
                        help="fraction of born transactions that are privacy "
                             "transactions, within [0, 1]; one sweep value "
                             "each")
    parser.add_argument("--leanstark-factor", default="1",
                        help="in-circuit weight of one leanSTARK witness as a "
                             "multiple of the nested-proof weight; an "
                             "unmeasured assumption, default band 1-2")
    parser.add_argument("--calibration", choices=sorted(CALIBRATIONS),
                        default="leanvm-b",
                        help="whose measurements set the circuit weights: "
                             "leanvm-b (cycle counts, no per-fold overhead) "
                             "or leanmultisig (dd51f0f proving times: 58 ms "
                             "per fold, 90 ms per nested proof, 1.8 ms per "
                             "signature)")
    parser.add_argument("--anchor-ms", default=None,
                        help="measured ms for one 2-to-1 fold; the hardware "
                             "axis -- the default is the calibration's own "
                             "Mac M4 Max figure (417 for leanvm-b, 238 for "
                             "leanmultisig), larger stands for a slower "
                             "machine")
    parser.add_argument("--verify-sig-ms", default="0.1",
                        help="native ms to verify one bare signature; "
                             "unmeasured, and a mode 0 wrapper multiplies it "
                             "by every signature it carries; one sweep value "
                             "each")
    parser.add_argument("--verify-stark-ms", default="15.6",
                        help="native ms to verify one STARK-class object; "
                             "extrapolated from leanVM-b's one measured "
                             "proof, 14.1-17.1 ms; one sweep value each")
    parser.add_argument("--proving-budget", type=float, default=None,
                        help="ms of proving per plan; default leaves the "
                             "model's one-aggregation-interval budget, which "
                             "binds on privacy-heavy plans -- root claim "
                             "width in the output is the tell")
    parser.add_argument("--egress-bytes", type=int, default=4_000_000,
                        help="send-buffer bound per node; 0 for unbounded")
    parser.add_argument("--presets", default="eip_faithful,high_throughput",
                        help="named configurations to compare, in order; "
                             "`name+field=value` holds a string override")
    parser.add_argument("--seeds", type=int, default=2)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    rates = []
    for tps in floats(args.tps):
        transactions = int(round(tps * args.slots * args.slot_length / 1000.0))
        if transactions < 1:
            parser.error(f"--tps {tps:g}: rounds to zero transactions over "
                         f"{args.slots} slots of {args.slot_length:g} ms")
        rates.append((tps, transactions))
    shares = floats(args.privacy_share)
    for share in shares:
        if not 0.0 <= share <= 1.0:
            parser.error(f"--privacy-share {share:g}: a share lies in [0, 1]")
    factors = floats(args.leanstark_factor)
    if args.anchor_ms is None:
        args.anchor_ms = str(CALIBRATIONS[args.calibration][3])
    anchors = floats(args.anchor_ms)
    sig_prices = floats(args.verify_sig_ms)
    stark_prices = floats(args.verify_stark_ms)
    presets = args.presets.split(",")
    for name in presets:
        fields = configuration(name)  # refuse a typo before spending minutes
        for field in OWNED_FIELDS:
            if field in fields:
                parser.error(
                    f"--presets {name}: sets {field}, which this bench "
                    "derives from its own flags; the run's record could not "
                    "attribute it")

    results = {}
    for tps, transactions in rates:
        for share in shares:
            privacy_born = int(round(transactions * share))
            for factor in factors:
                for anchor in anchors:
                    for sig_ms in sig_prices:
                        for stark_ms in stark_prices:
                            for name in presets:
                                runs = []
                                for seed in range(args.seeds):
                                    outcome, sim = one_run(
                                        args, name, transactions, share,
                                        factor, anchor, sig_ms, stark_ms,
                                        seed)
                                    runs.append(outcome)
                                    if args.json:
                                        emit_json_record(
                                            "mainnet",
                                            workload_parameters(
                                                args, preset=name, tps=tps,
                                                transactions=transactions,
                                                privacy_share=share,
                                                privacy_born=privacy_born,
                                                leanstark_factor=factor,
                                                anchor_ms=anchor,
                                                verify_sig_ms=sig_ms,
                                                verify_stark_ms=stark_ms,
                                                fee_model="cycle5"),
                                            seed, sim, outcome)
                                results[(tps, share, factor, anchor, sig_ms,
                                         stark_ms, name)] = runs
    if args.json:
        return 0

    budget_note = (f"{args.proving_budget:g} ms" if args.proving_budget
                   is not None else "one aggregation interval (default)")
    egress_note = (f"{args.egress_bytes / 1e6:g} MB" if args.egress_bytes
                   else "unbounded")
    print(f"nodes={args.nodes} degree={args.degree} "
          f"uplink={args.uplink_mbps:.0f} Mbit/s latency={args.latency:.0f} ms "
          f"slot={args.slot_length / 1000:g} s, born over {args.slots} slots, "
          f"{args.drain_slots} drain, seeds={args.seeds}")
    if args.calibration == "leanmultisig":
        print("circuit weights (leanMultisig dd51f0f ms): overhead 58 "
              "signature 1.8 nested 90; leanSTARK witness = factor x nested, "
              "unmeasured")
    else:
        print("circuit weights (leanVM-b cycles): signature~10930 "
              "nested~373250; leanSTARK witness = factor x nested, "
              "unmeasured")
    print(f"egress bound {egress_note} per node, proving budget {budget_note}, "
          f"fees cycle 0-4")
    print(f"native verify swept: sig {args.verify_sig_ms} ms, "
          f"STARK-class {args.verify_stark_ms} ms (leanVM-b measures "
          f"14.1-17.1 for one proof size); no block messages under "
          f"instant notify")
    print()
    width = max(len(name) for name in presets)
    print_table_header(
        f"{'tx/s':>5} {'privacy':>7} {'lstark':>6} {'anchor':>6} "
        f"{'vsig':>5} {'vstk':>5} "
        f"{'preset':>{width}} {'offered MB':>11} {'included':>12} "
        f"{'delay':>6} {'cpu%':>5} {'claim':>6} {'proofs':>7}")
    for tps, transactions in rates:
        for share in shares:
            for factor in factors:
                for anchor in anchors:
                    for sig_ms in sig_prices:
                        for stark_ms in stark_prices:
                            for index, name in enumerate(presets):
                                runs = results[(tps, share, factor, anchor,
                                                sig_ms, stark_ms, name)]
                                lead = (f"{tps:>5g} {share:>7.2f} "
                                        f"{factor:>6g} {anchor:>6g} "
                                        f"{sig_ms:>5g} {stark_ms:>5g}"
                                        ) if index == 0 else " " * 39
                                print(f"{lead} {name:>{width}} "
                              f"{mean_of(runs, 'offered') / 1e6:>11.1f} "
                              f"{mean_of(runs, 'included'):>6.1f}/"
                              f"{mean_of(runs, 'arrived'):<5.0f} "
                              f"{mean_of(runs, 'delay'):>6.2f} "
                              f"{mean_of(runs, 'cpu_load_window') * 100:>5.1f} "
                              f"{mean_of(runs, 'root_claim'):>6.1f} "
                              f"{mean_of(runs, 'proofs'):>7.0f}")
    print()
    print("included is counted after the drain; delay averages included")
    print("transactions only. cpu% is utilisation over the birth window,")
    print("read before the drain. claim is mean transactions claimed per")
    print("root aggregate (one never folded further): near two when the")
    print("proving budget binds selection, whatever the plan shape. Cells")
    print("are means over seeds; publication evidence is the --json records.")
    print("No trace ceiling is modelled, and the leanSTARK weight is an")
    print("assumption swept over its band, not a measurement.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

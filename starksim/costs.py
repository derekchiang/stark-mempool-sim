#!/usr/bin/env python3
"""
How long each kind of computation takes.

Supplied from outside rather than hardcoded, because these are the numbers the
study varies. There is no verification *logic* anywhere in this simulator --
objects are correct by construction -- so `verify_recv` is a duration, not a
predicate, and zero is a legitimate value for it. It is still charged rather
than assumed free: store-and-forward puts it on the propagation path, where it
competes with proving for the node's single CPU.

Three kinds, deliberately few:

- `verify_recv`    -- processing one received message before it can be relayed
- `prove_recursive`-- producing one aggregate
- `build_block`    -- the proposer assembling a block

Verification is one duration per received message. Splitting it by cryptographic
scheme would model internals this simulator deliberately omits.

A value is either a constant or a callable taking a size. For recursive proving,
the configured cost basis decides whether that size counts claimed dependencies
or fold inputs.
"""

import random

VERIFY_RECV = "verify_recv"
PROVE_RECURSIVE = "prove_recursive"
BUILD_BLOCK = "build_block"

KINDS = (VERIFY_RECV, PROVE_RECURSIVE, BUILD_BLOCK)


# How a proving job's price is measured. Named here rather than in the wiring
# because both the wiring and the reporting layer must agree on them, and
# reporting sits below the wiring.
PROVE_COST_DEPS = "deps"
PROVE_COST_INPUTS = "inputs"
PROVE_COST_CIRCUIT = "circuit"
PROVE_COST_BASES = (PROVE_COST_DEPS, PROVE_COST_INPUTS, PROVE_COST_CIRCUIT)


class CostTable:
    """Durations per computation kind, with optional per-job jitter."""

    __slots__ = ("costs", "jitter_frac", "_rng", "prove_cost_basis")

    def __init__(self, verify_recv=0.0, prove_recursive=0.0, build_block=0.0,
                 jitter_frac=0.0, seed=0):
        if jitter_frac < 0:
            raise ValueError("jitter_frac must be >= 0")
        self.costs = {
            VERIFY_RECV: verify_recv,
            PROVE_RECURSIVE: prove_recursive,
            BUILD_BLOCK: build_block,
        }
        for kind, value in self.costs.items():
            if not callable(value) and value < 0:
                raise ValueError(f"{kind} cost must be >= 0")
        self.jitter_frac = float(jitter_frac)
        # Jitter is drawn per job rather than derived from an identity: a job's
        # identity is its position in a queue, which is not stable across
        # parameter changes anyway, so there is nothing to keep comparable. One
        # dedicated stream keeps it out of the way of every other draw.
        self._rng = random.Random(f"{seed}:compute")
        # The basis a proving job is priced by, so an estimate can be taken in
        # the unit execution charges. It travels with the table because that is
        # the only thing a selection strategy is handed. One name for one
        # concept: the wiring stamps this through `for_basis`, and it is not a
        # constructor argument because the simulator overwrites whatever a
        # caller chose -- an argument that cannot affect anything is exactly
        # what this codebase refuses elsewhere.
        self.prove_cost_basis = PROVE_COST_DEPS

    def for_basis(self, prove_cost_basis):
        """This table, priced the way `prove_cost_basis` prices.

        A copy rather than a mutation: a table may be shared between runs, and
        a basis stamped onto one of them would silently follow it into the
        others.
        """
        if prove_cost_basis not in PROVE_COST_BASES:
            raise ValueError(f"unknown prove_cost_basis {prove_cost_basis!r}")
        clone = CostTable()
        clone.prove_cost_basis = prove_cost_basis
        clone.costs = dict(self.costs)
        clone.jitter_frac = self.jitter_frac
        clone._rng = self._rng
        return clone

    def base(self, kind, size=1):
        value = self.costs[kind]
        if callable(value):
            return float(value(size))
        return float(value)

    def duration(self, kind, size=1):
        """Service time for one job of this kind."""
        cost = self.base(kind, size)
        if cost == 0.0 or self.jitter_frac == 0.0:
            return cost
        return cost + self._rng.uniform(0.0, self.jitter_frac * cost)

    def is_free(self, kind, size=1):
        """Whether this kind costs nothing for this size, so no job is needed.

        This query exists for focused cost-table tests; production code checks
        the computed duration it is about to schedule.

        Asks the *computed* duration rather than inspecting the stored value. A
        callable returning zero is as free as a literal zero. Inspecting the
        stored callable itself would instead queue a zero-duration job behind
        existing CPU work.
        """
        return self.base(kind, size) == 0.0


# Hash-evaluation counts for the three things EIP-8288's recursive circuit
# verifies (`eip-8288.md:188`: a flat list of leanSPHINCS signatures, leanSTARK
# witnesses, and nested recursive STARKs). Hashes are the unit because both
# sides can be estimated in it and neither can be measured in milliseconds
# without a circuit.
#
# A SPHINCS-style verification is ~470 hashes. Two independent routes agree:
# the fitted EVM model `ExecGas = 36118 + 194 * ops` puts the C13 parameter set
# at 468 from its 127 K gas, and the scheme's own formulas give
# FORS `(k-1)(1+a)+2` = 121, WOTS+ average `l(w-1)/2 * d` = 301 and Merkle
# `h` = 24, for 446.
#
# A recursive STARK verification is FRI-dominated -- queries x Merkle depth x
# commitment rounds -- which spans roughly 5 000 (64 x 21 x 4) to 19 000
# (100 x 24 x 8). 10 000 is the middle of that range and is an estimate, not a
# measurement. Verifying a leanSTARK witness is also a STARK verification, so it
# defaults to the same figure.
SPHINCS_HASHES = 468
LEANSTARK_HASHES = 10_000
RECURSIVE_HASHES = 10_000

# The protocol's hard ceiling on one proof: the 2**24 KoalaBear trace length
# with a fixed trace width (`eip-8288.md:416`). Rows per hash depends on the
# in-circuit hash, which is the dominant unknown here -- a dedicated Poseidon2
# AIR is a few rows per permutation, a Keccak-in-AIR is two orders more.
TRACE_CAP = 2 ** 24
TRACE_ROWS_PER_HASH = 3



class CircuitCost:
    """Proving cost as in-circuit verification work, priced by input kind.

    A fold's cost is not a function of how many inputs it has. The circuit
    verifies signatures, leanSTARK witnesses and nested recursive proofs with
    three different verifiers, and on the estimates above one nested proof is
    worth about twenty signatures. Charging per input therefore bills a
    signature at a nested proof's price, which is the difference between
    "proving a hundred signatures is cheap" and "it is impossible".

    Callable with a single count for the paths that have only a scalar to hand
    -- selection estimates and variant identity -- where it answers the
    conservative case of that many nested proofs.
    """

    __slots__ = ("ms_per_hash", "overhead_hashes", "sphincs_hashes",
                 "leanstark_hashes", "recursive_hashes",
                 "trace_rows_per_hash", "trace_cap")

    def __init__(self, ms_per_hash, overhead_hashes=0.0,
                 sphincs_hashes=SPHINCS_HASHES,
                 leanstark_hashes=LEANSTARK_HASHES,
                 recursive_hashes=RECURSIVE_HASHES,
                 trace_rows_per_hash=TRACE_ROWS_PER_HASH,
                 trace_cap=TRACE_CAP):
        if ms_per_hash < 0:
            raise ValueError("ms_per_hash must be >= 0")
        self.ms_per_hash = float(ms_per_hash)
        self.overhead_hashes = float(overhead_hashes)
        self.sphincs_hashes = float(sphincs_hashes)
        self.leanstark_hashes = float(leanstark_hashes)
        self.recursive_hashes = float(recursive_hashes)
        self.trace_rows_per_hash = float(trace_rows_per_hash)
        self.trace_cap = trace_cap

    @classmethod
    def anchored_on_pairwise_fold(cls, milliseconds, **kwargs):
        """Calibrate from one measured 2-to-1 fold, which is two recursive
        verifications. That is the measurement this project actually has, so it
        is the only honest place to attach a millisecond scale."""
        probe = cls(ms_per_hash=1.0, **kwargs)
        hashes = probe.hashes(0, 0, 2)
        if hashes <= 0:
            raise ValueError("a pairwise fold must cost some hashes")
        return cls(ms_per_hash=milliseconds / hashes, **kwargs)

    def hashes(self, signatures, leanstarks, recursive):
        return (self.overhead_hashes
                + self.sphincs_hashes * signatures
                + self.leanstark_hashes * leanstarks
                + self.recursive_hashes * recursive)

    def duration(self, signatures, leanstarks, recursive):
        return self.ms_per_hash * self.hashes(signatures, leanstarks, recursive)

    def trace_rows(self, signatures, leanstarks, recursive):
        return self.trace_rows_per_hash * self.hashes(
            signatures, leanstarks, recursive)

    def fits(self, signatures, leanstarks, recursive):
        """Whether one proof can hold this much work at all."""
        return self.trace_rows(signatures, leanstarks, recursive) <= self.trace_cap

    def __call__(self, count):
        # The conservative reading of a bare count: that many nested proofs.
        return self.duration(0, 0, count)

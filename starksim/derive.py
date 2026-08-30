#!/usr/bin/env python3
"""
Deriving values instead of drawing them.

A simulation parameter sweep is only interpretable if changing one input changes
one thing. Drawing from a shared generator breaks that: values depend on how
many draws came before, so adding a node reshuffles every link's latency and
asking about slot 7 before slot 3 changes what slot 3 answers.

The fix is to make a value a pure function of the identity it belongs to --
`(seed, edge)`, `(seed, slot)` -- rather than of the order it was requested in.
That buys three things at once: independence between parameters, idempotence
under repeated queries, and laziness, since nothing has to be precomputed.

blake2b rather than the built-in `hash()`: Python randomises string hashing per
process, so anything derived from it would not reproduce across runs.
"""

import hashlib


def stable_fraction(seed, *parts):
    """A value in [0, 1), stable for this (seed, parts) across processes."""
    key = ":".join(str(part) for part in (seed,) + parts).encode()
    digest = hashlib.blake2b(key, digest_size=8).digest()
    return int.from_bytes(digest, "big") / 2.0 ** 64


def stable_index(bound, seed, *parts):
    """An integer in [0, bound), stable for this (seed, parts)."""
    if bound <= 0:
        raise ValueError("bound must be > 0")
    return int(stable_fraction(seed, *parts) * bound)

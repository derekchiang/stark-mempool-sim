#!/usr/bin/env python3
"""
What a message carries, and how big that makes it.

A wrapper contains transactions sent either in full or as hashes, plus a
dependency list and a proof. Its size therefore depends on the recipient's
known bodies, not only on the content identity.

Two disciplines here, both deliberate:

- **Payloads carry identities and masks, never object references.** An
  announcement says "I have object 7", not "here is object 7". A node that
  wants the thing looks it up through the arena, which is a visible step; being
  handed a live reference is how a node accidentally learns what it was not
  told.
- **Size is computed once, when the payload is built, and decomposed.** The
  transport layer reads one number and never inspects content. Keeping the
  decomposition means a byte total can be checked against its parts instead of
  taken on faith.
"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SizeBreakdown:
    """Bytes by what they are, so a total can be audited against its parts."""

    bodies: int = 0
    hashes: int = 0
    dependencies: int = 0
    proof: int = 0
    control: int = 0

    def total(self):
        return (self.bodies + self.hashes + self.dependencies + self.proof
                + self.control)


@dataclass(frozen=True, slots=True)
class AnnouncePayload:
    """"I have this." An identity and nothing else."""

    content_id: int
    size: SizeBreakdown

    kind = "announce"


@dataclass(frozen=True, slots=True)
class WantPayload:
    """"Send me that.\""""

    content_id: int
    size: SizeBreakdown

    kind = "want"


@dataclass(frozen=True, slots=True)
class ObjectPayload:
    """A single object sent in full by flood or fetch relay.

    `content_id` rather than `object_id` so that every payload naming one thing
    names it the same way, and resolving an identity is one code path.
    """

    content_id: int
    size: SizeBreakdown
    # Whether a peer asked for this, or it was pushed unbidden. Identical on the
    # wire -- same bytes, same kind -- but the distinction is the signature of
    # the relay policy, so it is worth counting separately.
    solicited: bool = False

    kind = "object"


@dataclass(frozen=True, slots=True)
class WrapperCore:
    """The recipient-independent part of a wrapper.

    One core per tick per node, shared by every copy sent out. `wrapper_id` is
    separate from `proof_object_id` on purpose: a tick that re-emits an existing
    aggregate unchanged produces a new wrapper but no new proof, and conflating
    them would either lose the wrapper event or invent a proof.
    """

    wrapper_id: int
    proof_object_id: int
    creator: int
    tick: int
    mode: int          # 0 = raw witnesses, 1 = one recursive proof
    tx_mask: int       # every transaction the wrapper claims
    dep_count: int

    def __post_init__(self):
        if self.mode not in (0, 1):
            raise ValueError(f"wrapper mode must be 0 or 1, got {self.mode}")


@dataclass(frozen=True, slots=True)
class WrapperPayload:
    """One recipient's copy of a wrapper.

    `body_mask` is the subset sent in full; everything else in the core's
    `tx_mask` travels as a hash because the recipient is believed to have it
    already. Two peers therefore receive different-sized copies of one wrapper.
    """

    core: WrapperCore
    body_mask: int
    size: SizeBreakdown
    # The proof travels as its 32-byte hash rather than its bytes, because the
    # sender believes this recipient already holds it. The receiver must be
    # able to tell: a reference it cannot resolve is not evidence.
    proof_by_ref: bool = False

    kind = "wrapper"

    def __post_init__(self):
        if self.body_mask & ~self.core.tx_mask:
            raise ValueError("body_mask must be a subset of the wrapper's txs")

@dataclass(frozen=True, slots=True)
class BlockPayload:
    """A block announcement, for metered chain notification."""

    block_id: int
    slot: int
    included: int
    # Everything the chain has settled as of this notice, inclusions and
    # expiries alike. Expiry produces no block of its own, so a notice carrying
    # only `included` could never convey one.
    settled: int
    size: SizeBreakdown

    kind = "block"


@dataclass(frozen=True, slots=True)
class WireMessage:
    """A payload in flight between two nodes."""

    sender: int
    receiver: int
    payload: object

    def kind(self):
        return self.payload.kind

    def size_bytes(self):
        return self.payload.size.total()

#!/usr/bin/env python3
"""
Chain facts, and how nodes come to know them.

Inclusion is global truth: a transaction either is or is not in a block. What a
*node* knows about that is a different thing, and keeping the two apart is what
stops node code from becoming accidentally omniscient -- reaching the chain's
truth takes a visible call through a notifier, not an attribute read.

`ProposerSchedule` decides which node builds a given slot. It lives here rather
than on the node because building is not a property a node has; it is an
assignment a slot makes. Every node is an ordinary mempool node.
"""

from starksim.derive import stable_index


class Block:
    """What one slot produced.

    Per-transaction delay needs the inclusion mask so each leaf can be found;
    proposer attribution needs the builder identity; and the trace needs the
    time. A count alone cannot reconstruct any of them.
    """

    __slots__ = ("slot", "proposer", "time", "attempted", "included",
                 "object_ids", "delays")

    def __init__(self, slot, proposer, time, attempted, included, object_ids,
                 delays):
        self.slot = slot
        self.proposer = proposer
        self.time = time
        # What the proposer selected, and what of it was actually new. They
        # differ when a proposer works from a stale view and picks coverage that
        # has already been included or expired -- wasted block capacity, which
        # is only visible if both are recorded.
        self.attempted = attempted
        self.included = included
        self.object_ids = object_ids    # the objects folded in, in pick order
        self.delays = delays            # per-tx inclusion delay, in slots

    def wasted(self):
        """Selected coverage that turned out to be already dead."""
        return self.attempted & ~self.included

    def count(self):
        return self.included.bit_count()


class ChainState:
    """The canonical block sequence and the set of no-longer-includable txs.

    `discarded` is a bitmask over leaf indices. It is the union of everything
    that has been included and everything pruned for another reason (expiry
    later). Nodes never read it directly -- they go through a notifier.
    """

    __slots__ = ("included", "expired", "blocks", "revision")

    def __init__(self):
        # Kept apart because they mean different things and are counted
        # differently: a transaction that expired is not pending and is not
        # included either, and a single union cannot express that.
        self.included = 0
        self.expired = 0
        self.blocks = []
        # Bumped whenever the discard set gains bits, by inclusion or expiry.
        # This is what a per-node usefulness cache must be keyed on: block count
        # alone misses expiry, leaving such a cache stale.
        self.revision = 0

    def discarded_mask(self):
        return self.included | self.expired

    def included_count(self):
        return self.included.bit_count()

    def expired_count(self):
        return self.expired.bit_count()

    def include(self, coverage, slot, proposer=None, time=0.0,
                object_ids=(), delays=()):
        """Record a block including everything new in `coverage`.

        Returns the `Block`. Only transactions not already dead count as
        included by this block, so nothing is counted twice -- and the block
        keeps what was *attempted* as well, so a stale proposer's wasted
        capacity is measurable.
        """
        newly = coverage & ~self.discarded_mask()
        # Delays are optional -- a caller measuring nothing passes none -- but
        # if given there must be exactly one per newly included transaction.
        if delays and len(delays) != newly.bit_count():
            raise ValueError(
                f"{len(delays)} delays for {newly.bit_count()} newly included "
                "transactions: one delay per transaction, or none")
        self.included |= newly
        if newly:
            self.revision += 1
        block = Block(slot, proposer, time, coverage, newly, tuple(object_ids),
                      tuple(delays))
        self.blocks.append(block)
        return block

    def expire(self, mask):
        """Prune transactions for a reason other than inclusion."""
        newly = mask & ~self.discarded_mask()
        self.expired |= newly
        if newly:
            self.revision += 1
        return newly


class InstantNotifier:
    """Every node learns of a chain fact the moment it becomes true.

    This is a deliberate simplification: every node's discard mask is identical
    at every instant, so the
    per-node machinery is exercised but never diverges. The consequence to stay
    aware of is that any metric measuring the *cost* of learning late -- stale
    aggregation waste, above all -- is structurally zero here and must be
    reported as unavailable rather than as zero.
    """

    __slots__ = ("chain",)

    kind = "instant"
    diverges = False

    def __init__(self, chain):
        self.chain = chain

    def known_discarded(self, node_id):
        return self.chain.discarded_mask()

    def generation(self, node_id):
        """One global generation: under instant notification nobody diverges.

        The chain's revision, not its block count: expiry changes what is
        useful without producing a block, and a cache keyed on block count
        would not notice.
        """
        return self.chain.revision


class OverlayNotifier:
    """Nodes learn chain facts when they are told, not when they become true.

    The counterpart to `InstantNotifier`, and the reason to have the interface
    at all. Under instant notification every node's discard mask is identical at
    every moment, so the per-node machinery is exercised without ever diverging:
    one global generation, and a cache keyed on (node, generation) never meets
    the case it exists for. Keeping this implementation alive from the start --
    even while `instant` is the default for measurements -- is what stops that
    from being discovered late.

    `generation` counts how many times a node's knowledge has changed. It is the
    key a per-node usefulness cache has to be invalidated against, so it is
    exposed now rather than retrofitted.
    """

    __slots__ = ("chain", "known", "generations")

    kind = "overlay"
    diverges = True

    def __init__(self, chain, node_count):
        self.chain = chain
        self.known = [0] * node_count
        self.generations = [0] * node_count

    def known_discarded(self, node_id):
        return self.known[node_id]

    def generation(self, node_id):
        return self.generations[node_id]

    def notify(self, node_id, mask):
        """Tell one node about transactions it did not know were discarded.

        Clamped to what is actually true: a notifier is a channel for chain
        facts, not a way to invent them, and letting a caller pass arbitrary
        bits would make a node's belief unfalsifiable.
        """
        mask &= self.chain.discarded_mask()
        fresh = mask & ~self.known[node_id]
        if not fresh:
            return 0
        self.known[node_id] |= fresh
        self.generations[node_id] += 1
        return fresh

    def notify_all(self, mask):
        for node_id in range(len(self.known)):
            self.notify(node_id, mask)


class ProposerSchedule:
    """Which node builds slot k.

    Two independent effects live here, and conflating them makes results
    unreadable: the proposer's *degree*, which sets how much it sees, and
    whether its *identity* is stable, which sets who is disadvantaged. A fixed
    proposer under partial diffusion penalises the same distant transactions
    every slot; rotation moves the blind spot at the cost of block-to-block
    consistency.

    A third question turns out to belong here rather than beside it: whether the
    proposer is a mempool node at all. `dedicated` is a fixed proposer that
    never aggregates -- v1's separate builder -- and it is a *role* rather than
    a modifier, because the rotating equivalent would be "every node stops
    folding during its own slot", which is a different mechanism and would
    deserve its own name. Keeping it as a fourth value leaves no unreachable
    combination to police.
    """

    MODES = ("fixed", "rotating", "random", "dedicated")

    __slots__ = ("mode", "node_count", "fixed_id", "seed")

    def __init__(self, node_count, mode="fixed", fixed_id=0, seed=0):
        if mode not in self.MODES:
            raise ValueError(f"unknown proposer mode {mode!r}")
        if not 0 <= fixed_id < node_count:
            raise ValueError(f"fixed proposer {fixed_id} outside 0..{node_count - 1}")
        self.mode = mode
        self.node_count = node_count
        self.fixed_id = fixed_id
        self.seed = seed

    def proposer_for(self, slot):
        """Which node builds `slot`. A pure function of the slot.

        This has to be idempotent: the builder is asked once to build, and again
        by metrics and the trace. Drawing from a generator per call would answer
        differently each time and attribute one block to several proposers, so
        the random mode derives from `(seed, slot)` instead of drawing.
        """
        if self.mode in ("fixed", "dedicated"):
            return self.fixed_id
        if self.mode == "rotating":
            # Deterministic round robin from the configured node, so a rotating
            # run is reproducible and every node proposes equally often.
            return (self.fixed_id + slot) % self.node_count
        return stable_index(self.node_count, self.seed, "proposer", slot)

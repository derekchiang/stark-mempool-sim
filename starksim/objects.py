#!/usr/bin/env python3
"""
Pool objects: anything that flows through the EIP-8288 mempool.

current networking: every object records when and where it was born. It is visible to its creator immediately and to
every other actor l ms later.
"""

# Creator sentinel: aggregators are 0..N-1, user broadcasts (tx arrivals)
# come from outside -- no actor sees them before their hop completes.
USER = -1


class PoolObject:
    __slots__ = ("id", "depth", "pending", "total", "born", "creator",
                 "parents", "consumed", "covering",
                 "builder_seen", "builder_counted", "traced_size")

    def __init__(self, oid, depth, pending, total, born, creator):
        self.id = oid
        self.depth = depth      # fold depth, for the viewer
        self.pending = pending  # covered txs, pruned of included ones lazily
        self.total = total      # total txs covered (object "size")
        self.born = born        # creation time: broadcast / fold completion
        self.creator = creator  # aggregator id, or USER for tx arrivals
        self.parents = []       # folds that consumed this object
        self.consumed = 0       # times taken as a fold input (waste metric)
        self.covering = False   # counted as covering pending txs (builder)
        self.builder_seen = False     # the builder's copy has landed
        self.builder_counted = False  # in builder_pool_size right now
        self.traced_size = len(pending)  # useful count last written to trace

    def useful_count(self, exclude_ids=frozenset()):
        # covered txs not yet included (and not in exclude_ids)
        count = 0
        for tx in self.pending:
            if tx.included:
                continue
            if tx.id in exclude_ids:
                continue
            count += 1
        return count

    def mark_included(self):
        # a tx can be covered by several pool objects at once: aggregators
        # are blind to each other's in-flight folds, so two folds can
        # consume the same inputs and produce overlapping aggregates. The
        # tx is included (and its delay counted) once, at its first
        # covering pick.
        # rebind, never clear: an in-flight fold reads this object later
        txs = [tx for tx in self.pending if not tx.included]
        for tx in txs:
            tx.included = True
        self.pending = []
        self.traced_size = 0     # the "inc" event already zeroes it
        return txs


class PrivacyTx(PoolObject):
    """A privacy-app tx carrying its own raw STARK; is its own pool object."""

    __slots__ = ("included",)
    kind = "p"

    def __init__(self, oid, t):
        super().__init__(oid, 0, [self], 1, t, USER)
        self.included = False


class PQSig(PoolObject):
    """A PQ tx carrying only a bare, unaggregated signature."""

    __slots__ = ("included",)
    kind = "q"

    def __init__(self, oid, t):
        super().__init__(oid, 0, [self], 1, t, USER)
        self.included = False


class AggregatedSTARK(PoolObject):
    """The 2-to-1 fold of two pool objects."""

    __slots__ = ("children",)
    kind = "a"

    def __init__(self, oid, a, b, born, creator):
        big, small = (a, b) if len(a.pending) >= len(b.pending) else (b, a)
        # Build a fresh pending list -- never alias an input's list: the
        # builder, or an overlapping aggregate (one a racing fold built
        # from the same input), may still read it. Overlapping aggregates
        # cover some of the same txs, so the union keeps each tx once
        # (bigger side's order first) and drops already-included txs.
        seen = set()
        pending = []
        for tx in big.pending + small.pending:
            if tx.included or tx.id in seen:
                continue
            seen.add(tx.id)
            pending.append(tx)
        # `total` stays additive on purpose: it measures embodied proving
        # work (proof size), and a raced fold really did re-prove the
        # overlap. Distinct coverage is what `pending` tracks.
        super().__init__(oid, max(a.depth, b.depth) + 1, pending,
                         a.total + b.total, born, creator)
        self.children = (a, b)
        a.parents.append(self)
        b.parents.append(self)

#!/usr/bin/env python3
"""
The object arena: global truth about pool objects, and nothing else.

The arena contains no per-viewer state. An object records what happened to it,
a node records what it believes, and chain facts live in `chain.py`.

Coverage
--------

An object covers a set of transactions. Representing that set as a list of
references does not scale with the object count: every node emits an aggregate
every tick, so a run produces on the order of

    N x slots x slot_length / AGGREGATION_INTERVAL

aggregates, each covering hundreds of transactions. Instead:

- **cold** objects store only their immutable tuple of children;
- **coverage is a bitmask** over dense leaf indices, computed on demand by
  walking the children and memoised per object. Coverage is immutable -- it is
  fixed by which objects were folded, not by what has happened since -- so this
  memo never needs invalidating.

That is the whole reason the union of two coverages is `a | b` and counting is
one `int.bit_count()`. What *does* change over time is how much of a coverage is
still *useful*, and that is per-node knowledge, never a property of the object:

    useful_count(coverage, known_discarded)

Provenance
----------

Objects record their `children`, because that is what they were built from. They
do **not** record parents. Two reasons: a parent link is a back-reference that
keeps the whole DAG alive, and -- more importantly -- "some fold consumed this"
is not something a node can observe. A mode-1 wrapper exposes a dependency list
and a proof, not the identity of its recursive inputs, so a node deciding what to
fold must never consult parent links or it silently becomes omniscient.
"""

from collections import OrderedDict

# Object kind letters are part of the trace format and must remain stable.
KIND_PRIVACY = "p"      # a privacy-app tx carrying its own STARK
KIND_PQ = "q"           # a bare post-quantum signature
KIND_AGGREGATE = "a"    # the fold of one or more objects

# Creator sentinel: an object broadcast by a user rather than built by a node.
USER = -1


def useful_count(coverage, known_discarded):
    """How many covered txs are still worth proving, from one node's view.

    `known_discarded` is that node's mask of transactions it knows are no longer
    includable -- included on chain, expired, or otherwise pruned. Bits set in
    coverage and clear in the discard mask are the useful ones.
    """
    return (coverage & ~known_discarded).bit_count()


def marginal_count(coverage, known_discarded, chosen):
    """Useful txs this object would add on top of `chosen`.

    Used when assembling a block or a wrapper: the second object in a selection
    is worth only what it covers that the first one did not.
    """
    return (coverage & ~known_discarded & ~chosen).bit_count()


class PoolObject:
    """One object in the arena. Global truth only.

    `leaf_index` is set for leaves and None for aggregates: coverage bits are
    indexed by leaf, and aggregates own no bit of their own.
    """

    __slots__ = ("id", "kind", "born", "creator", "children",
                 "leaf_index", "total", "depth", "consumed", "_coverage",
                 "fee", "expiry", "claimed")

    def __init__(self, oid, kind, born, creator, children, leaf_index,
                 fee=0.0, expiry=None):
        self.id = oid
        self.kind = kind
        self.born = born
        self.creator = creator
        self.children = children      # tuple, possibly empty for a leaf
        self.leaf_index = leaf_index
        # Both inert by default, and both exist so that two tiers of the EIP's
        # selection order are expressible at all: without a fee the last tier
        # ("users' transactions in descending order of priority fee") cannot be
        # implemented, and without an expiry nothing can become unincludable
        # except by being included, which makes the removals tier vacuous.
        self.fee = fee
        self.expiry = expiry
        # What this proof *asserts*, as distinct from what it physically folds.
        # Frozen when the proof starts and never recomputed: the EIP's circuit
        # takes its discards as an input, so the claim is decided by what the
        # prover knew when it began, not by what anyone knows later. Recomputing
        # at completion could restore a dependency the prover explicitly
        # dropped. None for leaves, which claim themselves.
        self.claimed = None
        # `total` is the additive leaf count of the fold tree: embodied proving
        # work, not a proof size. It double-counts overlap on purpose, because a
        # fold that re-proved an overlap really did spend that work.
        if leaf_index is not None:
            self.total = 1
            self.depth = 0
            # Deliberately not stored. A leaf's mask is `1 << leaf_index`, and
            # a Python integer's size grows with its highest bit, so keeping one
            # per leaf makes the collection **quadratic** in transaction count --
            # about 625 MB of raw bit payload at a hundred thousand leaves. It
            # is a single shift to rebuild, so it is rebuilt.
            self._coverage = None
        else:
            self.total = sum(child.total for child in children)
            self.depth = max(child.depth for child in children) + 1
            self._coverage = None     # computed on first request, then kept
        self.consumed = 0             # times taken as a fold input

    def is_leaf(self):
        return self.leaf_index is not None


class Arena:
    """Owns every object and hands out identities.

    Node views hold references into the arena; only the arena may drop an
    object. Structural sharing means an ancestor stays reachable long after
    every view has moved past it, so "no view holds it" and "collectable" are
    different questions.
    """

    __slots__ = ("objects", "leaves", "_next_id", "_next_leaf_index",
                 "coverage_memos", "cache_limit", "_cache", "cache_evictions")

    def __init__(self, coverage_cache_limit=None):
        self.objects = {}
        # Leaf index -> object, so a coverage mask can be turned back into the
        # transactions it covers. Needed the moment inclusion delay is measured:
        # a mask says which bits, and a delay needs each one's birth time.
        self.leaves = []
        self._next_id = 0
        self._next_leaf_index = 0
        # [D15]'s bounded hot cache (docs/history/networking-plan.md). Coverage
        # is immutable, so a memoised mask
        # never needs invalidating -- but it does need evicting, because asking
        # for one root memoises every aggregate beneath it and a per-tick scan
        # over every candidate would make "everything traversed stays hot
        # forever" a leak rather than a cache. None means unbounded.
        self.coverage_memos = 0
        self.cache_limit = coverage_cache_limit
        self._cache = OrderedDict()
        self.cache_evictions = 0

    def new_leaf(self, kind, born, creator=USER, fee=0.0, expiry=None):
        """Create a transaction: its own object, covering exactly itself."""
        obj = PoolObject(self._next_id, kind, born, creator,
                         (), self._next_leaf_index, fee, expiry)
        self._next_id += 1
        self._next_leaf_index += 1
        self.objects[obj.id] = obj
        self.leaves.append(obj)
        return obj

    def new_aggregate(self, children, born, creator, claimed=None):
        """Fold one or more objects into an aggregate.

        Arity is free: the EIP's circuit takes a flat list of dependencies plus
        inner recursive proofs, so a node's per-tick aggregate is naturally one
        wide fold rather than a chain of pairs. A single child is allowed and
        means "re-proved as-is".
        """
        if not children:
            raise ValueError("an aggregate needs at least one child")
        obj = PoolObject(self._next_id, KIND_AGGREGATE, born, creator,
                         tuple(children), None)
        obj.claimed = claimed
        self._next_id += 1
        self.objects[obj.id] = obj
        return obj

    def note_consumption(self, children):
        """Book proving work against the objects it was spent on.

        Deliberately *not* done by `new_aggregate`. Once proving takes time, the
        aggregate is created when the proof finishes, while the work was
        committed when the job started -- and a job that is cancelled, or still
        in flight when the run ends, spent effort that produced no aggregate at
        all. Booking on creation would miss all three cases, so the caller that
        starts the work is the one that records it.
        """
        for child in children:
            child.consumed += 1

    def claim(self, obj):
        """What this object asserts, which is what another node may rely on.

        For a leaf, itself. For an aggregate, the mask frozen when its proof
        began -- not its coverage, which may be wider because a prover drops
        dependencies it knows are settled. Everything that decides usefulness,
        selection or inclusion must ask this rather than `coverage()`; coverage
        is an internal fact about folding.

        Falls back to coverage for an aggregate built without a claim, which is
        the case in unit tests that construct folds directly.
        """
        if obj.leaf_index is not None:
            return 1 << obj.leaf_index
        if obj.claimed is None:
            return self.coverage(obj)
        return obj.claimed

    def coverage(self, obj):
        """The object's coverage bitmask, computing and memoising as needed.

        A leaf's mask is rebuilt from its index every time; an aggregate's is
        memoised behind the LRU. Cheap and idempotent once cached; the walk runs
        only for aggregates whose mask has never been built or has been evicted.

        Walks the DAG iteratively rather than recursively: fold chains can get
        deep, and a recursive walk would put run-dependent pressure on the
        interpreter's stack.
        """
        if obj.leaf_index is not None:
            return 1 << obj.leaf_index
        if obj._coverage is not None:
            self._touch(obj)
            return obj._coverage
        # Post-order traversal: push a node, then its unresolved children; when
        # a node comes back around with all children resolved, OR them together.
        stack = [obj]
        while stack:
            current = stack[-1]
            if current.leaf_index is not None or current._coverage is not None:
                stack.pop()
                continue
            pending_children = []
            for child in current.children:
                if child.leaf_index is None and child._coverage is None:
                    pending_children.append(child)
            if pending_children:
                stack.extend(pending_children)
                continue
            mask = 0
            for child in current.children:
                if child.leaf_index is not None:
                    mask |= 1 << child.leaf_index
                else:
                    mask |= child._coverage
            current._coverage = mask
            self.coverage_memos += 1
            self._cache[current.id] = None
            stack.pop()
        self._touch(obj)
        self._trim_cache(keep=obj.id)
        return obj._coverage

    def _touch(self, obj):
        """Mark an aggregate as recently used, so it is evicted last."""
        if obj.leaf_index is not None:
            return
        if obj.id in self._cache:
            self._cache.move_to_end(obj.id)

    def _trim_cache(self, keep=None):
        """Evict least-recently-used masks. `keep` is never evicted.

        A limit of zero would otherwise evict the very mask being returned, and
        `coverage()` would hand back None -- a bound that corrupts the answer
        rather than only the memory.
        """
        if self.cache_limit is None:
            return
        while len(self._cache) > self.cache_limit:
            if len(self._cache) == 1 and keep is not None and keep in self._cache:
                return
            object_id, _ = self._cache.popitem(last=False)
            obj = self.objects.get(object_id)
            if obj is not None and obj.leaf_index is None:
                obj._coverage = None
                self.coverage_memos -= 1
                self.cache_evictions += 1

    def coverage_cache_bytes(self):
        """Roughly how much memory the memoised masks hold.

        Counted in bytes rather than in entries: a mask's width grows with the
        highest transaction id it covers, so late aggregates are far larger than
        early ones and a count of entries says little about the cost.
        """
        total = 0
        for object_id in self._cache:
            obj = self.objects.get(object_id)
            if obj is not None and obj._coverage is not None:
                total += (obj._coverage.bit_length() + 7) // 8
        return total

    def drop_coverage_memo(self, obj):
        """Release a memoised mask. Only aggregates have one to release."""
        if obj.leaf_index is None and obj._coverage is not None:
            obj._coverage = None
            self.coverage_memos -= 1
            self._cache.pop(obj.id, None)

    def leaf_count(self):
        return self._next_leaf_index

    def schemes_in(self, mask):
        """The *scheme* of each dependency in `mask`, lowest index first.

        Deliberately narrower than `leaves_in`. A node holding a claim holds its
        dependency triples, whose first field is the scheme
        (`eip-8288.md:67`), so reading kinds off a claim is reading what the
        wire carried -- while reading the leaf objects themselves would be
        reading global truth. Anything that needs only kinds asks for only
        kinds, so the two cannot be confused at a call site.
        """
        return [leaf.kind for leaf in self.leaves_in(mask)]

    def leaves_in(self, mask):
        """The leaf objects whose bits are set in `mask`, lowest index first."""
        found = []
        remaining = mask
        while remaining:
            lowest_bit = remaining & -remaining
            index = lowest_bit.bit_length() - 1
            found.append(self.leaves[index])
            remaining ^= lowest_bit
        return found

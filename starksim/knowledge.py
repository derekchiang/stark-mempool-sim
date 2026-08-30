#!/usr/bin/env python3
"""Cost-bounded tracing of per-node transaction knowledge."""

from starksim.node import (KNOWLEDGE_BODY_ONLY, KNOWLEDGE_HELD_AND_BODY,
                           KNOWLEDGE_NONE, KNOWLEDGE_SETTLED,
                           knowledge_evidence)

# The classes in which the node has the transaction's body. A transition into
# one of them from outside is caused by whatever delivered that body.
WITH_BODY = (KNOWLEDGE_HELD_AND_BODY, KNOWLEDGE_BODY_ONLY)


class KnowledgeTracer:
    """Emits one net knowledge transition per (node, transaction) per event.

    A single calendar event performs several mutations. Wrapper completion pops
    the in-flight proof, installs bodies, admits the aggregate and may then evict
    candidates, so a per-mutation report would manufacture intermediate states
    no observer could have seen. Knowledge is therefore classified once after
    each event and only the net change is emitted. The event kind is the stable
    attribution for that transition.

    Only nodes marked dirty are recomputed. Classifying every node against every
    followed transaction after every event would make observation quadratic in
    run length.
    """

    __slots__ = ("nodes", "arena", "clock", "tracer", "followed", "classes",
                 "_dirty", "_cause", "_all_dirty", "_all_dirty_leaves",
                 "classifications")

    def __init__(self, nodes, arena, clock, tracer):
        self.nodes = nodes
        self.arena = arena
        self.clock = clock
        self.tracer = tracer
        self.followed = set()
        self.classes = {}
        self._dirty = {}
        self._cause = {}
        self._all_dirty = False
        self._all_dirty_leaves = None
        # Counted so a performance claim can be asserted without timing
        # anything: wall clock on a shared machine is not signal, and this is.
        self.classifications = 0

    def active(self):
        return bool(self.followed) and self.tracer.enabled()

    def follow(self, leaf_index):
        """Start following without dirtying nodes whose state remains `none`.

        Dirtying every node here would make every birth reclassify every
        followed leaf and turn observation quadratic in run length.
        """
        self.followed.add(leaf_index)

    def mark(self, node_id, leaves=None, peer=None, obj=None, bodies=0):
        """Mark that this node's knowledge may have changed for `leaves`.

        `None` means anything and is correct but takes the slow path.

        `peer`, `obj` and `bodies` say what this event delivered to the node:
        the sender, the object id (or `"block"` for a block notice), and the
        mask of transaction bodies that came with it. They attribute a
        transition to its cause only where the cause is the evidence or the
        body -- see `flush` -- so a delivery that evicts something else is not
        blamed for the eviction. Every delivery path must pass them; a path
        that forgets produces no error, only a spread tree with an edge
        missing.
        """
        if peer is not None:
            self._cause[node_id] = (peer, obj, bodies)
        if leaves is None:
            self._dirty[node_id] = None
            return
        current = self._dirty.get(node_id, 0)
        if current is None:
            return
        self._dirty[node_id] = current | leaves

    def mark_all(self, leaves=None):
        """Mark that every node's knowledge may have changed for `leaves`.

        Over-marking costs time but not correctness; under-marking is caught by
        the full-recount tests.
        """
        if leaves is None:
            self._all_dirty = True
            self._all_dirty_leaves = None
            return
        if self._all_dirty and self._all_dirty_leaves is None:
            return
        self._all_dirty = True
        if self._all_dirty_leaves is None:
            self._all_dirty_leaves = leaves
        else:
            self._all_dirty_leaves |= leaves

    def flush(self, kind):
        if not self.active():
            self._dirty.clear()
            self._cause.clear()
            self._all_dirty = False
            return
        if self._all_dirty:
            shared = self._all_dirty_leaves
            work = {}
            for node_id in range(len(self.nodes)):
                own = self._dirty.get(node_id, 0)
                if shared is None or own is None:
                    work[node_id] = None
                else:
                    work[node_id] = own | shared
        else:
            work = self._dirty
        causes = self._cause
        self._dirty = {}
        self._cause = {}
        self._all_dirty = False
        self._all_dirty_leaves = None
        now = round(self.clock(), 3)
        for node_id, mask in work.items():
            if not self.tracer.covers(node_id):
                continue
            node = self.nodes[node_id]
            cause = causes.get(node_id)
            if mask is None:
                candidates = self.followed
            elif mask.bit_count() < len(self.followed):
                # Walking the narrower set keeps per-event work proportional to
                # changed knowledge rather than all followed transactions.
                candidates = []
                remaining = mask
                while remaining:
                    lowest = remaining & -remaining
                    leaf_index = lowest.bit_length() - 1
                    if leaf_index in self.followed:
                        candidates.append(leaf_index)
                    remaining ^= lowest
            else:
                # When the changed mask is wider, scanning the followed set is
                # the cheaper direction.
                candidates = [leaf for leaf in self.followed
                              if mask >> leaf & 1]
            for leaf_index in candidates:
                self.classifications += 1
                current, evidence = knowledge_evidence(node, leaf_index,
                                                       self.arena)
                key = (node_id, leaf_index)
                previous = self.classes.get(key, KNOWLEDGE_NONE)
                if current == previous:
                    continue
                self.classes[key] = current
                extra = {}
                if evidence is not None:
                    extra["o"] = evidence
                # The peer is attributed only where what it delivered is the
                # evidence the new class rests on, the body the new class has
                # and the old one lacked, or the notice that settled the
                # transaction. One event can change several transactions at
                # a node -- admitting one object evicts another -- and the
                # delivery caused only the transitions its cargo appears in.
                # When a body from a second peer was merged into a
                # verification the first peer's copy started, the transition
                # lands when that verification completes and carries the
                # first peer: the evidence arm is checked first, and the
                # merge is not a transition of its own. Recorded as the rule;
                # crediting the body's deliverer would need the merge to keep
                # its sender.
                if cause is not None:
                    peer, delivered, bodies = cause
                    if ((evidence is not None and delivered == evidence) or
                            (current == KNOWLEDGE_SETTLED and
                             delivered == "block") or
                            (current in WITH_BODY and
                             previous not in WITH_BODY and
                             bodies >> leaf_index & 1)):
                        extra["peer"] = peer
                self.tracer.emit("knw", node_id, t=now, tx=leaf_index,
                                 **{"from": previous}, to=current, by=kind,
                                 **extra)

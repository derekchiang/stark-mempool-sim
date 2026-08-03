#!/usr/bin/env python3
"""Actors on the pool (Aggregator, Builder) and the Block record."""

import heapq


class Aggregator:
    """One folding worker. The aggregation strategy:

        loop forever:
            publish(fold(random object, random object))
    """

    __slots__ = ("id", "pair")

    def __init__(self, agg_id):
        self.id = agg_id
        self.pair = None        # (a, b) while folding, else None

    def begin(self, a, b):
        self.pair = (a, b)

    def finish(self):
        a, b = self.pair
        self.pair = None
        return a, b

class Builder:
    """The one block builder. At each slot boundary:

        repeat up to B+1 times:
            pick the object covering the most pending txs that
            this slot's earlier picks do not already cover
        fold the picks 2-to-1 into the block's single STARK

    The builder keeps its own view of the store: the sim reports every
    delivery with sees() and every removal with forget().
    """

    __slots__ = ("budget", "includable", "effective_objects", "pool_size",
                 "visible_pending")

    def __init__(self, budget):
        self.budget = int(budget)
        self.includable = []         # lazy max-heap (-pending, id),
                                     # objects the builder holds
        self.effective_objects = 0   # builder-held objects still
                                     # covering pending txs
        self.pool_size = 0           # builder-held objects not yet
                                     # seen folded away
        self.visible_pending = 0     # pending txs the builder holds

    def sees(self, obj):
        # what's the point of "builder_seen" and "builder_coutned"?
        # The builder's copy has landed: the object becomes includable.
        # `covering` tracks membership in effective_objects; a twin whose
        # coverage got included through its double stays counted until its
        # own consumption becomes visible (transient, race-window sized).
        obj.builder_seen = True
        if not any(p.builder_seen for p in obj.parents):
            obj.builder_counted = True
            self.pool_size += 1
            count = obj.useful_count()
            if count:
                obj.covering = True
                self.effective_objects += 1
                heapq.heappush(self.includable, (-count, obj.id))
        if obj.kind != "a" and not obj.included:
            self.visible_pending += 1

    def forget(self, obj):
        # The object left the store; drop it from the builder's counts.
        if obj.builder_counted:
            obj.builder_counted = False
            self.pool_size -= 1
        if obj.covering:
            obj.covering = False
            self.effective_objects -= 1

    def build(self, pool, slot_length, t, k):
        blk = Block(k)
        blk.folds_8288 = max(0, self.effective_objects - 1)
        blk.folds_baseline = max(0, self.visible_pending - 1)
        blk.pool_objects = self.pool_size

        # fold the up-to-(B+1) pool objects covering the most pending txs
        selection = self._get_best_objects_for_block(pool, self.budget + 1)

        # Make sure the builder also SNARKs up any chosen PQ transactions
        if len(selection) > 1:
            blk.folds_used = len(selection) - 1
        elif len(selection) == 1 and selection[0].kind == "q":
            blk.folds_used = 1
        else:
            blk.folds_used = 0
        for obj in selection:
            for tx in self._include(obj):
                blk.count(tx, (t - tx.born) / slot_length)
        blk.object_ids = [o.id for o in selection]
        return blk

    def _get_best_objects_for_block(self, pool, k):
        selection = []
        chosen = set()
        while self.includable and len(selection) < k:
            neg_count, oid = heapq.heappop(self.includable)
            obj = pool.get(oid)
            if obj is None or not obj.covering:
                continue
            marginal = obj.useful_count(chosen)
            if marginal == 0:
                continue
            if marginal < -neg_count:
                heapq.heappush(self.includable, (-marginal, oid))
                continue
            selection.append(obj)
            for tx in obj.pending:
                if not tx.included:
                    chosen.add(tx.id)
        return selection

    def _include(self, obj):
        txs = obj.mark_included()
        if obj.covering:
            obj.covering = False
            self.effective_objects -= 1
        self.visible_pending -= sum(1 for tx in txs if tx.builder_seen)
        return txs

class Block:
    __slots__ = ("slot", "n_priv", "n_pq", "folds_used", "folds_8288",
                 "folds_baseline", "pool_objects", "object_ids", "delays")

    def __init__(self, slot):
        self.slot = slot
        self.n_priv = 0
        self.n_pq = 0
        self.folds_used = 0      # builder folds actually spent (<= B)
        self.folds_8288 = 0      # builder folds to include everything
        self.folds_baseline = 0  # builder folds, no-8288 baseline
        self.pool_objects = 0    # pool size in the builder's view
        self.object_ids = []     # included pool objects, selection order
        self.delays = []         # per-tx inclusion delay in slots, in order

    def count(self, tx, delay_slots):
        """Count transactions in this block"""
        self.delays.append(delay_slots)
        if tx.kind == "q":
            self.n_pq += 1
        else:
            self.n_priv += 1



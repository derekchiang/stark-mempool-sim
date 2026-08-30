#!/usr/bin/env python3
"""
What a node decides to prove each tick, and how.

Two pure stages, deliberately separate:

- **Selection** turns a node's view into an immutable `Selection`: which objects
  to fold, what they cover, and what it is estimated to cost. It carries the
  chain revision it was made against, so work that has gone stale can be
  recognised as stale rather than silently proved.
- **Planning** turns a selection into an `AggregationPlan`: a tree of proving
  jobs with one root. Separating them is what lets a queued plan be abandoned
  and re-decided instead of being a closure already committed to its inputs.

Neither stage touches the engine. Nothing here schedules anything.
"""

from dataclasses import dataclass

from starksim.arena import KIND_PQ, marginal_count
from starksim.derive import stable_index
from starksim.costs import (PROVE_COST_DEPS, PROVE_COST_INPUTS,
                            PROVE_RECURSIVE, CircuitCost)
from starksim.strategies import register


@dataclass(frozen=True, slots=True)
class Selection:
    """What a node chose to fold this tick, and what it claims.

    `claimed` is `claim_union` minus what this node knows is discarded. That is
    the EIP's `discards` input doing its job (`eip-8288.md:143`): a new proof
    over an old aggregate does not have to keep re-proving transactions that
    have since been included. Dropping them is what its "removing anything that
    needs to be removed" priority means when coverage itself is immutable.
    """

    inputs: tuple            # object ids, in priority order
    input_claims: tuple      # each input's claim, parallel to `inputs`
    claim_union: int         # union of the inputs' claims
    claimed: int             # claim_union minus locally known discards
    tick: int
    revision: int            # the chain revision this was decided against
    estimated_cost: float

    def dep_count(self):
        return self.claimed.bit_count()

    def is_empty(self):
        return not self.inputs


# What a selection must take before the budget may stop it. The budget bounds a
# *fold*, and the cheapest fold has two inputs -- so stopping at one produces a
# node that never stalls and never aggregates either, proving single objects
# for ever while the configuration appears to be pricing aggregation. The
# first-input rule was written against a livelock and stopped one input short
# of the thing it was protecting. A node holding fewer than two objects still
# selects what it has; this is a floor on what the budget may refuse, not on
# what must exist.
MINIMUM_FOLD_INPUTS = 2


def input_composition(arena, view, object_ids):
    """What a circuit must verify to fold these objects, counted by kind.

    One place, because selection and execution must agree: an estimate in a

    unit execution does not charge is a budget ceiling wearing a cost's

    clothing, and that has already produced one withdrawn result in this

    project.

    An aggregate costs one nested verification when its recursive proof is

    held. Where the evidence arrived as mode 0 witnesses there is no proof to

    nest, so the circuit verifies the dependencies themselves. A leaf is its

    own kind. An input that has left the arena contributes nothing, the same

    way `_resolve` drops it when the job finally runs.


    """
    signatures = 0
    leanstarks = 0
    recursive = 0
    for object_id in object_ids:
        obj = arena.objects.get(object_id)
        if obj is None:
            continue
        if not obj.is_leaf():
            if obj.id not in view.witness_backed:
                recursive += 1
                continue
            for leaf in arena.leaves_in(arena.claim(obj)):
                if leaf.kind == KIND_PQ:
                    signatures += 1
                else:
                    leanstarks += 1
        elif obj.kind == KIND_PQ:
            signatures += 1
        else:
            leanstarks += 1
    return signatures, leanstarks, recursive


def estimate_cost(costs, dep_count, arena=None, view=None, object_ids=()):
    """What the selected inputs are expected to cost to prove.

    Under a scalar price the dependency count is the size argument, which is

    what execution charges under `prove_cost_basis="deps"`.

    A `CircuitCost` is not a scalar in the dependency count: it prices what

    the circuit verifies, where one nested proof is worth about twenty

    signatures. Passing a bare count would make the callable read it as that

    many nested proofs -- roughly twenty times the truth for signatures -- and

    a binding budget would then refuse selections for a cost nothing will pay.

    The composition of the chosen inputs is used instead, which is the same

    function execution prices the root job by.

    Under `inputs` the size is how many objects the fold takes, which is what

    execution counts there. A dependency count differs from it exactly when

    the inputs are aggregates -- many dependencies, one input -- and always in

    the direction that narrows the claim.

    It remains a *lower bound* under multi-level plans, whose internal roots

    add nested verifications a selection cannot know about before planning.

    That error is deliberate in this direction: underestimating lets a node

    overrun a budget, which the CPU queue and `on_tick_overrun` make visible,

    while overestimating silently narrows the claim and looks like a protocol

    result.


    """
    prover = costs.costs.get(PROVE_RECURSIVE)
    usable = arena is not None and view is not None
    if isinstance(prover, CircuitCost) and usable:
        return prover.duration(*input_composition(arena, view, object_ids))
    basis = getattr(costs, "prove_cost_basis", PROVE_COST_DEPS)
    if basis == PROVE_COST_INPUTS:
        return costs.base(PROVE_RECURSIVE, max(1, len(object_ids)))
    return costs.base(PROVE_RECURSIVE, max(1, dep_count))


@register("selection", "eip_priority")
def eip_priority(node, arena, tick, costs, budget, previous_aggregate=None):
    """The EIP's stated order (`eip-8288.md:258`).

    1. this node's own aggregate from the previous round; 2. removals -- not a

    selection step here, because coverage is immutable and usefulness is per

    node, so dead dependencies are dropped from what the wrapper *claims*

    rather than removed from an object; 3. other nodes' aggregates, by how

    many new transactions each adds; 4. loose transactions, by descending fee.

    Selection stops when the estimated proving cost reaches the budget, which

    is why the estimate is part of the loop rather than computed afterwards --

    with one exception. The budget may not refuse a node its first *fold*, so

    the first `MINIMUM_FOLD_INPUTS` inputs are taken whatever they are

    estimated to cost. A node whose cheapest possible fold does not fit its

    interval would otherwise select one object and prove it alone, for ever:

    never stalling, never aggregating, and reporting a proving cost while

    measuring a budget ceiling. Taking two and overrunning is what a real node

    does, and the overrun is visible in the CPU queue.


    """
    known_discarded = node.known_discarded()
    chosen = []
    claims = []
    coverage = 0

    def would_add(obj):
        return marginal_count(arena.claim(obj), known_discarded, coverage)

    def take(obj):
        nonlocal coverage
        chosen.append(obj.id)
        claims.append(arena.claim(obj))
        coverage |= arena.claim(obj)

    # Tier 1: keep building on what this node proved last time.
    if previous_aggregate is not None and would_add(previous_aggregate):
        take(previous_aggregate)

    held_aggregates = []
    held_leaves = []
    for object_id in node.view.held:
        obj = arena.objects.get(object_id)
        if obj is None or obj is previous_aggregate:
            continue
        if obj.is_leaf():
            held_leaves.append(obj)
        else:
            held_aggregates.append(obj)

    # Tier 3: peers' aggregates, most new transactions first. Recomputed each
    # round rather than sorted once, because taking one changes what the next
    # would add.
    while held_aggregates:
        best = None
        best_gain = 0
        for obj in held_aggregates:
            gain = would_add(obj)
            if gain > best_gain:
                best_gain = gain
                best = obj
        if best is None:
            break
        merged = (coverage | arena.claim(best)).bit_count()
        if _over_budget(costs, chosen + [best.id], merged, arena, node.view,
                        budget):
            break
        take(best)
        held_aggregates.remove(best)

    # Tier 4: loose transactions by fee, highest first, id breaking ties so the
    # order is reproducible.
    held_leaves.sort(key=lambda obj: (-obj.fee, obj.id))
    for obj in held_leaves:
        if not would_add(obj):
            continue
        merged = (coverage | arena.claim(obj)).bit_count()
        if _over_budget(costs, chosen + [obj.id], merged, arena, node.view,
                        budget):
            break
        take(obj)

    claimed = coverage & ~known_discarded
    return Selection(tuple(chosen), tuple(claims), coverage, claimed, tick,
                     node.notifier.generation(node.id),
                     estimate_cost(costs, claimed.bit_count(), arena,
                                   node.view, chosen))


def _over_budget(costs, object_ids, dep_count, arena, view, budget):
    """Whether taking these inputs would exceed the budget, floor respected.

    The floor is `MINIMUM_FOLD_INPUTS`: a budget may refuse a node a wider
    fold, never its first one.
    """
    if len(object_ids) <= MINIMUM_FOLD_INPUTS:
        return False
    return estimate_cost(costs, dep_count, arena, view, object_ids) > budget


@register("selection", "random_pair")
def random_pair(node, arena, tick, costs, budget, previous_aggregate=None):
    """Two objects, drawn uniformly and kind-blind: v1's rule.

    The baseline the other strategies are worth measuring against. It has no

    size heuristic, no pairing rule and no fee order -- whether a fold absorbs

    a fresh transaction, merges two deep aggregates or pairs two bare

    signatures falls out of the draw. That is the point: a no-coordination

    floor.

    It ignores the budget, which is not an oversight. A pair is the cheapest

    fold there is, and `MINIMUM_FOLD_INPUTS` already puts it beyond the

    budget's reach; a budget could only refuse a third input, and this policy

    never asks for one. It ignores `previous_aggregate` too, because a node's

    own output is already among the objects it holds, which is exactly v1's

    treatment of it.

    The draw is derived from `(node, tick)` rather than taken from a

    generator, so it is reproducible and independent of how many draws came

    before. The limit worth knowing: the selection signature carries no seed,

    so those positions repeat across seeds -- what differs is the candidate

    list they index into, which depends on topology and arrival order and

    therefore on the seed. For a sweep over seeds that is enough; a policy

    needing a genuinely per-seed stream would need the signature to carry one.


    """
    known_discarded = node.known_discarded()
    candidates = []
    # Sorted, so the list a draw indexes into does not depend on set iteration
    # order -- which is stable within a process and not something to rely on.
    for object_id in sorted(node.view.held):
        obj = arena.objects.get(object_id)
        if obj is None:
            continue
        if arena.claim(obj) & ~known_discarded:
            candidates.append(obj)
    chosen = []
    if candidates:
        first = candidates.pop(stable_index(len(candidates), node.id,
                                            "pair", tick, 0))
        chosen.append(first)
    if candidates:
        # Drawn from the remainder, so nothing is ever folded with itself.
        chosen.append(candidates[stable_index(len(candidates), node.id,
                                              "pair", tick, 1)])

    claims = [arena.claim(obj) for obj in chosen]
    coverage = 0
    for claim in claims:
        coverage |= claim
    claimed = coverage & ~known_discarded
    inputs = tuple(obj.id for obj in chosen)
    return Selection(inputs, tuple(claims), coverage, claimed, tick,
                     node.notifier.generation(node.id),
                     estimate_cost(costs, claimed.bit_count(), arena,
                                   node.view, inputs))


@register("selection", "coverage_greedy")
def coverage_greedy(node, arena, tick, costs, budget, previous_aggregate=None):
    """Marginal coverage only, ignoring fee and provenance.

    The no-priority baseline: whatever adds the most transactions goes next.

    Kept so the EIP's ordering can be measured against something rather than

    assumed to help.


    """
    known_discarded = node.known_discarded()
    chosen = []
    claims = []
    coverage = 0
    candidates = []
    for object_id in node.view.held:
        obj = arena.objects.get(object_id)
        if obj is not None:
            candidates.append(obj)
    if previous_aggregate is not None:
        candidates.append(previous_aggregate)
    while candidates:
        best = None
        best_gain = 0
        for obj in candidates:
            gain = marginal_count(arena.claim(obj), known_discarded, coverage)
            if gain > best_gain:
                best_gain = gain
                best = obj
        if best is None:
            break
        merged = coverage | arena.claim(best)
        if _over_budget(costs, chosen + [best.id], merged.bit_count(), arena,
                        node.view, budget):
            break
        chosen.append(best.id)
        claims.append(arena.claim(best))
        coverage = merged
        candidates.remove(best)
    claimed = coverage & ~known_discarded
    return Selection(tuple(chosen), tuple(claims), coverage, claimed, tick,
                     node.notifier.generation(node.id),
                     estimate_cost(costs, claimed.bit_count(), arena,
                                   node.view, chosen))


def live_fee(arena, mask):
    """Total fee of the transactions in `mask`. Caller pre-masks liveness."""
    total = 0.0
    for leaf in arena.leaves_in(mask):
        total += leaf.fee
    return total


@register("selection", "fee_density")
def fee_density(node, arena, tick, costs, budget, previous_aggregate=None):
    """Greedy by marginal fee per marginal proving cost.

    `eip_priority` ranks peers' aggregates by transaction count and loose
    transactions by raw fee -- neither sees that a pick's *value* and its
    *cost* are different quantities, so under a binding budget it can spend
    its proving time on many cheap claims while a few expensive ones carry
    the fees. Here every held object competes on one number: the live fee it
    adds divided by the proving cost it adds. Tier 1 is kept -- the node's own
    previous aggregate goes first, because re-asserting everything already
    proven is the continuity the duty cycle is built on.

    The first pick after tier 1 is taken even over budget, for the same
    livelock reason as `eip_priority`: a node whose cheapest proof does not
    fit its interval must overrun visibly, not select nothing for ever.
    """
    known_discarded = node.known_discarded()
    chosen = []
    claims = []
    coverage = 0

    def take(obj):
        nonlocal coverage
        chosen.append(obj.id)
        claims.append(arena.claim(obj))
        coverage |= arena.claim(obj)

    if previous_aggregate is not None:
        added = (arena.claim(previous_aggregate)
                 & ~known_discarded & ~coverage)
        if added:
            take(previous_aggregate)

    candidates = []
    for object_id in node.view.held:
        obj = arena.objects.get(object_id)
        if obj is not None and obj is not previous_aggregate:
            candidates.append(obj)

    while candidates:
        best = None
        best_key = None
        best_merged = 0
        for obj in candidates:
            added = arena.claim(obj) & ~known_discarded & ~coverage
            if not added:
                continue
            merged = coverage | arena.claim(obj)
            added_cost = (estimate_cost(costs, merged.bit_count(), arena,
                                        node.view, chosen + [obj.id])
                          - estimate_cost(costs, coverage.bit_count(), arena,
                                          node.view, chosen))
            added_fee = live_fee(arena, added)
            if added_cost > 0:
                density = added_fee / added_cost
            else:
                # A pick the cost model prices as free: value at any cost
                # ratio, so it sorts above every priced pick.
                density = float("inf")
            # Ties broken by absolute fee, then lowest id, so the order is
            # reproducible whatever the dict order of the view.
            key = (density, added_fee, -obj.id)
            if best_key is None or key > best_key:
                best_key = key
                best = obj
                best_merged = merged.bit_count()
        if best is None:
            break
        if _over_budget(costs, chosen + [best.id], best_merged, arena,
                        node.view, budget):
            break
        take(best)
        candidates.remove(best)

    claimed = coverage & ~known_discarded
    return Selection(tuple(chosen), tuple(claims), coverage, claimed, tick,
                     node.notifier.generation(node.id),
                     estimate_cost(costs, claimed.bit_count(), arena,
                                   node.view, chosen))


@register("selection", "deadline")
def deadline(node, arena, tick, costs, budget, previous_aggregate=None):
    """Rescue what is about to expire first.

    The placement result showed the real penalty of a constrained system is
    *exclusion*, not lateness -- and `eip_priority` gives a transaction one
    slot from expiry no priority whatsoever. Here a pick's urgency is the
    earliest expiry among the live transactions it adds; transactions with no
    deadline sort last, and ties fall to the added fee. Tier 1 continuity and
    the first-pick livelock exception are kept, as in every selection.
    """
    known_discarded = node.known_discarded()
    chosen = []
    claims = []
    coverage = 0

    def take(obj):
        nonlocal coverage
        chosen.append(obj.id)
        claims.append(arena.claim(obj))
        coverage |= arena.claim(obj)

    if previous_aggregate is not None:
        added = (arena.claim(previous_aggregate)
                 & ~known_discarded & ~coverage)
        if added:
            take(previous_aggregate)

    candidates = []
    for object_id in node.view.held:
        obj = arena.objects.get(object_id)
        if obj is not None and obj is not previous_aggregate:
            candidates.append(obj)

    while candidates:
        best = None
        best_key = None
        best_merged = 0
        for obj in candidates:
            added = arena.claim(obj) & ~known_discarded & ~coverage
            if not added:
                continue
            urgency = float("inf")
            for leaf in arena.leaves_in(added):
                if leaf.expiry is not None and leaf.expiry < urgency:
                    urgency = leaf.expiry
            merged = coverage | arena.claim(obj)
            # Lower urgency is better, so it is negated into a max-key; fee
            # and id break ties reproducibly.
            key = (-urgency, live_fee(arena, added), -obj.id)
            if best_key is None or key > best_key:
                best_key = key
                best = obj
                best_merged = merged.bit_count()
        if best is None:
            break
        if _over_budget(costs, chosen + [best.id], best_merged, arena,
                        node.view, budget):
            break
        take(best)
        candidates.remove(best)

    claimed = coverage & ~known_discarded
    return Selection(tuple(chosen), tuple(claims), coverage, claimed, tick,
                     node.notifier.generation(node.id),
                     estimate_cost(costs, claimed.bit_count(), arena,
                                   node.view, chosen))


@dataclass(frozen=True, slots=True)
class PlanJob:
    """One proving job. `inputs` are object ids; `depends_on` are job indices."""

    index: int
    inputs: tuple
    depends_on: tuple
    # What this job's output will assert, and how many dependencies that is.
    # Per job rather than per plan, because pricing a two-input fold as though
    # it proved the whole selection makes every plan cost the same and the
    # comparison between plan shapes meaningless.
    output_mask: int = 0
    dep_count: int = 0


@dataclass(frozen=True, slots=True)
class AggregationPlan:
    """A tree of proving jobs with exactly one root.

    A plan is not a job. "Prove all of this" and "prove it as a chain of pairs"
    are different amounts of work producing the same coverage, which is the
    comparison `[D8]` (`docs/history/networking-plan.md`) exists to make -- so
    arity is a plan shape, not a constructor argument.
    """

    jobs: tuple
    root: int
    selection: Selection

    def job_count(self):
        return len(self.jobs)


def asserted_count(mask, selection):
    """How many dependencies a job with this coverage will actually assert.

    A plan's `output_mask` is structural -- the union of what its inputs cover
    -- while the claim frozen when the job starts drops whatever the node knows
    is settled. Counting the structural mask would price a job for dependencies
    its proof never asserts, and would disagree with the estimate selection was
    stopped by, which counts the live claim. Same fault as pricing an estimate
    in the wrong unit, one level down.
    """
    return (mask & selection.claimed).bit_count()


@register("fold_plan", "wide")
def wide_plan(selection):
    """One n-ary fold over everything selected: the EIP's own shape."""
    mask = 0
    for claim in selection.input_claims:
        mask |= claim
    job = PlanJob(0, selection.inputs, (), mask,
                  asserted_count(mask, selection))
    return AggregationPlan((job,), 0, selection)


@register("fold_plan", "binary")
def binary_plan(selection):
    """A balanced tree of 2-to-1 folds.

    Same coverage as one wide fold, but `k-1` proofs instead of one, and the

    root is not available until the last of them finishes. That difference is

    the point of keeping this option.


    """
    return _capped_plan(selection, 2)


@register("fold_plan", "capped8")
def capped8_plan(selection):
    """At most eight children per fold: a middle ground between the two."""
    return _capped_plan(selection, 8)


def _capped_plan(selection, fan_in):
    if fan_in < 2:
        raise ValueError("a capped plan needs a fan-in of at least 2")
    if not selection.inputs:
        return AggregationPlan((), -1, selection)
    claim_of = dict(zip(selection.inputs, selection.input_claims))
    if len(selection.inputs) == 1:
        mask = selection.input_claims[0]
        job = PlanJob(0, selection.inputs, (), mask,
                      asserted_count(mask, selection))
        return AggregationPlan((job,), 0, selection)
    jobs = []
    # Level by level: object inputs at the bottom, job outputs above. Every
    # non-carry batch consumes at least two pending items and replaces them
    # with one job, so the pending count strictly decreases. A lone leftover
    # is carried into the next level, where another item must join it; the two
    # empty/one-root breaks therefore terminate the only non-consuming paths.
    pending_objects = list(selection.inputs)
    pending_jobs = []
    while len(pending_objects) + len(pending_jobs) > 1 or not jobs:
        next_jobs = []
        while pending_objects or pending_jobs:
            batch_objects = pending_objects[:fan_in]
            pending_objects = pending_objects[fan_in:]
            room = fan_in - len(batch_objects)
            batch_jobs = pending_jobs[:room]
            pending_jobs = pending_jobs[room:]
            if not batch_objects and not batch_jobs:
                break
            if len(batch_objects) + len(batch_jobs) == 1 and (
                    pending_objects or pending_jobs or next_jobs):
                # A lone leftover waits for the next level rather than being
                # re-proved on its own, which would cost a proof for nothing.
                if batch_objects:
                    pending_objects = batch_objects + pending_objects
                else:
                    pending_jobs = batch_jobs + pending_jobs
                break
            index = len(jobs)
            mask = 0
            for object_id in batch_objects:
                mask |= claim_of.get(object_id, 0)
            for produced in batch_jobs:
                mask |= jobs[produced].output_mask
            jobs.append(PlanJob(index, tuple(batch_objects),
                                tuple(batch_jobs), mask,
                                asserted_count(mask, selection)))
            next_jobs.append(index)
        if not next_jobs:
            break
        pending_jobs = next_jobs + pending_jobs
        if len(pending_jobs) == 1 and not pending_objects:
            break
    return AggregationPlan(tuple(jobs), len(jobs) - 1, selection)

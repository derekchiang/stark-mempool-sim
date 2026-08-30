#!/usr/bin/env python3
"""
Nodes and what they believe.

A node is an ordinary mempool participant: it receives objects, admits them to
its own view, and relays what is new to its other peers. Building a block is not
a property a node has -- it is an assignment a slot makes (`chain.ProposerSchedule`).

Everything here is *belief*. The object arena holds what happened; the chain
holds what is true; a node holds what it has been told. Deliberately absent:

- no access to an object's parents, because "somebody folded this" is not
  observable from a wrapper (see `arena`);
- no reading of chain state except through a notifier, so learning late is
  expressible rather than accidentally impossible.

This module knows nothing about the calendar. A node decides *what* should
happen; `netsim` decides *when*, because only the wiring layer should hold the
engine.
"""

from starksim.arena import marginal_count, useful_count
from starksim.strategies import register


class SeenWindow:
    """Identities this node has already handled, in bounded memory.

    Duplicate suppression needs to remember what has been seen, and remembering
    everything for ever is the larger of the two per-node memory problems --
    bounding the candidate set does nothing for it, because a node sees far more
    than it keeps.

    Two generations, rotated: when the newer one fills, it becomes the older one
    and a fresh set starts. Membership checks both, so recent history is exact
    and older history is dropped wholesale rather than decaying unpredictably.
    Forgetting means a very old identity can be accepted twice, so that is
    counted rather than hidden. `limit=None` remembers everything.
    """

    __slots__ = ("limit", "current", "previous", "forgotten")

    def __init__(self, limit=None):
        self.limit = limit
        self.current = set()
        self.previous = set()
        self.forgotten = 0

    def __contains__(self, identity):
        return identity in self.current or identity in self.previous

    def __len__(self):
        return len(self.current) + len(self.previous)

    def add(self, identity):
        self.current.add(identity)
        if self.limit is not None and len(self.current) > self.limit:
            self.forgotten += len(self.previous)
            self.previous = self.current
            self.current = set()

    def discard(self, identity):
        self.current.discard(identity)
        self.previous.discard(identity)


class PeerBodyBeliefs:
    """What this node believes each of its peers already holds.

    The EIP lets a sender replace a transaction body with a 32-byte hash when
    the peer already has it (`eip-8288.md:223`), so a sender needs a belief
    about each peer, per transaction. Note what this is *not*: "I have sent this
    to you". A belief updated when a message is queued would let a later
    hash-only wrapper overtake or outlive the body it refers to, so it is
    updated only on delivery.

    Masks are indexed by the node's stable peer order, so a peer's slot does not
    move. Python integers give ideal bitwise operations, but their size grows
    with the highest transaction id for every peer, so the byte cost is
    instrumented -- chunked masks that can drop old chunks are the fallback.
    """

    __slots__ = ("peer_slot", "known_masks")

    def __init__(self, peers):
        self.peer_slot = {peer: index for index, peer in enumerate(peers)}
        self.known_masks = [0] * len(peers)

    def believes_held(self, peer, tx_mask):
        """The part of `tx_mask` this peer is believed to already hold."""
        return tx_mask & self.known_masks[self.peer_slot[peer]]

    def missing_for(self, peer, tx_mask):
        """The part of `tx_mask` that must travel to this peer in full."""
        return tx_mask & ~self.known_masks[self.peer_slot[peer]]

    def confirm_delivery(self, peer, body_mask):
        """Record bodies that have *arrived* at a peer. Returns what was new."""
        slot = self.peer_slot[peer]
        fresh = body_mask & ~self.known_masks[slot]
        self.known_masks[slot] |= fresh
        return fresh

    def mask_bytes(self):
        """Rough memory held by these masks, for the scale question."""
        total = 0
        for mask in self.known_masks:
            total += (mask.bit_length() + 7) // 8
        return total


class PeerProofBeliefs:
    """Which proofs this node knows each peer already holds.

    The wrapper-economy levers need this: re-sending a 256 kB recursive proof
    to a peer that verifiably has it is the single largest avoidable cost in
    the protocol, but "verifiably" is the load-bearing word. A belief forms in
    exactly two ways -- this node *delivered* the proof to that peer, or that
    peer *sent* it to this node -- and never at enqueue, for the same reason as
    `PeerBodyBeliefs`: a queued message can still be dropped, and a belief
    formed early would suppress the retransmission that repairs the loss.

    Unbounded: one entry per (peer, proof) pair, so it grows with run duration
    like `verified_proofs` does.
    """

    __slots__ = ("peer_slot", "known")

    def __init__(self, peers):
        self.peer_slot = {peer: index for index, peer in enumerate(peers)}
        self.known = [set() for _ in peers]

    def believes_held(self, peer, proof_id):
        return proof_id in self.known[self.peer_slot[peer]]

    def note(self, peer, proof_id):
        self.known[self.peer_slot[peer]].add(proof_id)


# -- eviction policies ------------------------------------------------------
#
# What "least worth keeping" means when the candidate limit forces a choice.
# The default rule ranks by usefulness -- live claimed transaction count --
# which values an aggregate over the many transactions it claims and never
# sees a fee: under memory pressure a high-fee loose transaction (usefulness
# one) dies before a mediocre aggregate (usefulness twenty). Whether that is
# right is a genuine design question -- the aggregate embodies paid-for
# proving work, the transaction carries the fee -- so it is a selectable
# policy, not an accident of the rule. Each policy scores one held object;
# the *lowest* scores are evicted first, oldest admission breaking ties.


@register("eviction", "usefulness")
def evict_by_usefulness(arena, known_discarded, obj):
    """Live claimed transactions: an aggregate's breadth is its worth."""
    if obj is None:
        return 0
    return useful_count(arena.claim(obj), known_discarded)


@register("eviction", "fee_value")
def evict_by_fee_value(arena, known_discarded, obj):
    """Live claimed fee: what the object is worth in the fee market's terms."""
    if obj is None:
        return 0.0
    total = 0.0
    for leaf in arena.leaves_in(arena.claim(obj) & ~known_discarded):
        total += leaf.fee
    return total


# -- knowledge classification ---------------------------------------------
#
# What a node knows about one transaction is not a single flag. It is four
# independent predicates over different identity domains -- settlement, evidence,
# body, retrieval -- and several objects covering the same transaction can
# satisfy several at once. A stacked area, or any count that must sum to the node
# count, needs a *partition*, so the projection from those predicates to one
# label is written down here once and used everywhere.
#
# Precedence, highest first. The order answers "what may this node still act on?"
# so current capability beats work in progress.

KNOWLEDGE_SETTLED = "settled"
KNOWLEDGE_HELD_AND_BODY = "held_and_body"
KNOWLEDGE_HELD_NO_BODY = "held_no_body"
KNOWLEDGE_BODY_ONLY = "body_only"
KNOWLEDGE_VERIFYING = "verifying"
KNOWLEDGE_REQUESTED = "requested"
KNOWLEDGE_NONE = "none"

# In precedence order, which is also the order a stacked area should draw them.
KNOWLEDGE_CLASSES = (
    KNOWLEDGE_SETTLED,
    KNOWLEDGE_HELD_AND_BODY,
    KNOWLEDGE_HELD_NO_BODY,
    KNOWLEDGE_BODY_ONLY,
    KNOWLEDGE_VERIFYING,
    KNOWLEDGE_REQUESTED,
    KNOWLEDGE_NONE,
)


def _matching(object_ids, leaf_index, arena, predicate):
    """The ids among `object_ids` whose object exists and covers the leaf
    under `predicate` -- claim for held evidence, claim or coverage for
    evidence in flight or requested."""
    bit = 1 << leaf_index
    return [object_id for object_id in object_ids
            if (obj := arena.objects.get(object_id)) is not None
            and predicate(obj) & bit]


def _pick(candidates, leaf_index, arena):
    """The one object a class rests on, out of the candidates that match.

    The transaction's own object when it is among them, else the lowest id.
    Deterministic whatever order the candidates arrive in, and the choice is
    the *form* the trace reports: own object against wrapper. A candidate
    missing from the arena cannot be the own object and ranks by id.
    """
    chosen = None
    for object_id in candidates:
        obj = arena.objects.get(object_id)
        if obj is not None and obj.leaf_index == leaf_index:
            return object_id
        if chosen is None or object_id < chosen:
            chosen = object_id
    return chosen


def knowledge_evidence(node, leaf_index, arena):
    """Which single class describes `node`'s knowledge of one transaction.

    Pure: reads `node` and `arena` and mutates nothing.

    Four precedence constraints are load-bearing:

    `settled` takes precedence over everything, including a held proof and a
    held body. Notification updates what a node knows is discarded immediately,
    while the held objects covering it are dropped only at the next aggregation
    tick -- so a node routinely knows a transaction is settled while still
    storing evidence and a body for it. This classifies capability, not storage:
    a settled transaction is one the node may no longer act on. Residual storage
    is a separate question, answered by the residency and stale-work metrics.

    `body_only` is reachable and is not a bug. `view.bodies` is monotonic -- only
    ever or-ed into -- while held objects leave by supersession, pruning and
    eviction, so a node keeps the body after losing every proof over it.

    A full object in flight is asked about with `coverage`, not `claim`: a full
    transfer delivers the bodies its structure covers. Evidence in flight is a
    `claim` question, as evidence always is. Both land in `verifying`, so both
    are checked.

    `none` means no *active* evidence, body or request -- never "never heard of
    it". The identity may still sit in `seen` and cause a re-delivery to be
    rejected as a duplicate, so claiming ignorance here would be false.

    Returns `(class, object_id)`: the object whose evidence produced the
    class, chosen by `_pick` over everything that matches under the branch's
    own predicate, and `None` for `settled`, `body_only` and `none`, which no
    single object evidences.
    """
    bit = 1 << leaf_index
    if node.known_discarded() & bit:
        return KNOWLEDGE_SETTLED, None

    has_body = bool(node.view.bodies & bit)
    held = _pick(_matching(node.view.held, leaf_index, arena, arena.claim),
                 leaf_index, arena)
    if held is not None:
        if has_body:
            return KNOWLEDGE_HELD_AND_BODY, held
        return KNOWLEDGE_HELD_NO_BODY, held
    if has_body:
        return KNOWLEDGE_BODY_ONLY, None

    # Evidence pending is a claim question; the bodies a full transfer will
    # deliver are a coverage question. Either makes this `verifying`. So does
    # a body riding on a wrapper still being verified, whether or not that
    # wrapper's proof is in the arena: the body is on its way regardless.
    in_flight = _matching(node.view.verifying, leaf_index, arena,
                          lambda obj: arena.claim(obj) | arena.coverage(obj))
    in_flight += [proof_id for proof_id, pending_bodies
                  in node.view.verifying_proofs.items()
                  if pending_bodies & bit]
    in_flight += _matching(
        [proof_id for proof_id, pending_bodies
         in node.view.verifying_proofs.items() if not pending_bodies & bit],
        leaf_index, arena, arena.claim)
    proof = _pick(in_flight, leaf_index, arena)
    if proof is not None:
        return KNOWLEDGE_VERIFYING, proof

    # Claim *or* coverage, for the same reason as the in-flight branch above:
    # a full-object response delivers the bodies its structure covers, so a
    # request for an aggregate is also a request for a leaf it covers but
    # does not claim. `claim` alone would classify an arriving body as
    # `none`.
    requested = _pick(
        _matching(node.view.requested, leaf_index, arena,
                  lambda obj: arena.claim(obj) | arena.coverage(obj)),
        leaf_index, arena)
    if requested is not None:
        return KNOWLEDGE_REQUESTED, requested

    return KNOWLEDGE_NONE, None


def knowledge_class(node, leaf_index, arena):
    """The class alone; see `knowledge_evidence` for the constraints."""
    return knowledge_evidence(node, leaf_index, arena)[0]


# -- supersession by provenance --------------------------------------------
#
# The design (docs/history/networking-plan.md, Part I) says a newer wrapper
# from the same peer supersedes the older one: a peer folds its whole view
# every tick, so its latest aggregate should subsume its earlier ones. Whether
# that is safe is not obvious -- a prover drops dependencies it knows are
# settled, so a newer claim can be *narrower* than the older one it replaces,
# and superseding unconditionally can throw away the only evidence a node
# holds for a transaction. So the rule is a selectable policy rather than a
# fixed behaviour, and comparing the options is the point.
#
# Each policy answers one question: given the aggregate a peer's newest wrapper
# just delivered, should this older aggregate from the same peer be dropped?
# `known_discarded` is the *node's* belief about settlement, not the chain's
# truth: a node can only drop based on what it has been told.


@register("supersede", "keep_all")
def supersede_keep_all(arena, known_discarded, old_object, new_object):
    """Never: retention is bounded by eviction and pruning alone."""
    return False


@register("supersede", "newest_per_peer")
def supersede_newest_per_peer(arena, known_discarded, old_object, new_object):
    """Always, as Part I wrote it (docs/history/networking-plan.md): the
    peer's newest wrapper wins outright.
    """
    return True


@register("supersede", "newest_if_covered")
def supersede_newest_if_covered(arena, known_discarded, old_object,
                                new_object):
    """Only when nothing still useful would be lost.

    The old aggregate may claim transactions the new one dropped. If every such
    transaction is one this node already knows is settled, the old claim's
    surplus is dead weight and the new aggregate subsumes it; if even one is
    still live, dropping the old aggregate would lose evidence, so it stays.
    Claims, not coverage: what another node may rely on is the claim.
    """
    old_useful = arena.claim(old_object) & ~known_discarded
    return (old_useful & ~arena.claim(new_object)) == 0


class NodeView:
    """One node's knowledge: what it has seen, and what it currently holds.

    `seen` is every object id ever delivered here, kept for duplicate
    suppression -- gossip delivers the same object from several peers and the
    duplicates are pure bandwidth waste, so they are counted rather than
    silently dropped.

    `held` maps object id to the time it was admitted. That timestamp is when
    this node learned the object, not a propagation estimate from its birth.

    `candidate_limit=None` leaves the candidate set unbounded. A finite limit
    makes eviction an explicit, counted loss rather than silently truncating
    state.
    """

    __slots__ = ("node_id", "seen", "verifying", "held", "requested",
                 "duplicates", "relayed", "bodies", "verified_proofs",
                 "peer_bodies", "peer_proofs", "seen_blocks",
                 "verifying_proofs", "witness_backed", "wrapper_provenance")

    def __init__(self, node_id, peers=(), seen_limit=None):
        self.node_id = node_id
        # Transaction bodies physically received, and proof identities already
        # verified. Kept apart because a wrapper can bring one without the
        # other: the same proof may arrive twice carrying different body
        # subsets, and the second copy is not a duplicate if it adds bodies.
        self.bodies = 0
        self.verified_proofs = set()
        self.peer_bodies = PeerBodyBeliefs(peers)
        self.peer_proofs = PeerProofBeliefs(peers)
        # Blocks this node has been told about. Separate from `seen`, which is a
        # forgetful window: a block is small, permanent news, and forgetting one
        # would let its notice circulate again for ever.
        self.seen_blocks = set()
        # Proofs being verified right now, with the bodies each in-flight copy
        # brought. Two copies of one proof carrying different body subsets must
        # merge while verification is in flight rather than the second being
        # discarded as a duplicate.
        self.verifying_proofs = {}
        # Aggregates whose evidence here is the EIP's mode 0: the raw witnesses
        # of the claim's dependencies, never the recursive proof. The claim is
        # just as well evidenced -- the witnesses were verified on arrival -- but
        # there are no proof bytes, so nothing may reference them by hash and a
        # later fold must verify signatures rather than a nested proof. Disjoint
        # from `verified_proofs` by construction; an id can move from here to
        # there when the proof itself later arrives, never the other way.
        self.witness_backed = set()
        # Which held aggregates each peer's wrappers delivered, so a newer
        # wrapper from that peer can supersede them. Provenance is who
        # *delivered* the aggregate, not who created it: the design's rule is
        # about a peer's stream of wrappers, and the creator of the proof
        # inside is a different fact. Bounded by the view -- entries whose
        # object has left `held` by any route are dropped on the next wrapper
        # from that peer.
        self.wrapper_provenance = {}
        self.seen = SeenWindow(seen_limit)
        # Received but not yet admitted. A node must not relay, or answer a
        # request for, something it has only started receiving.
        self.verifying = set()
        self.held = {}
        # Objects this node has asked a peer for but not yet received. Only the
        # announce-then-fetch policy uses it: without it a node would request
        # the same body from every peer that announced it, which is the waste
        # the policy exists to avoid.
        self.requested = set()
        self.duplicates = 0
        self.relayed = 0

    def holds(self, obj):
        return obj.id in self.held

    def admitted_at(self, obj):
        return self.held.get(obj.id)

class Node:
    """A mempool node: peers, a view, and the rules for what to do on receipt."""

    __slots__ = ("id", "peers", "view", "notifier", "candidate_limit",
                 "eviction", "evicted", "superseded", "pruned")

    def __init__(self, node_id, peers, notifier, seen_limit=None,
                 candidate_limit=None, eviction=None):
        self.id = node_id
        self.peers = tuple(peers)
        self.view = NodeView(node_id, self.peers, seen_limit)
        self.notifier = notifier
        self.candidate_limit = candidate_limit
        # How this node scores a held object when the limit forces eviction.
        self.eviction = eviction if eviction is not None else evict_by_usefulness
        # Two ways an object leaves a view without being included, kept apart
        # because they mean different things: superseded is the model working as
        # intended, evicted is the model admitting it dropped something it should
        # have kept.
        self.evicted = 0
        self.superseded = 0
        self.pruned = 0

    # -- receiving ---------------------------------------------------------

    def begin_receive(self, obj):
        """Start taking an object in. False if it is already seen.

        Processing may take time between these calls. During that window the
        node has the bytes but must not relay them or serve them to a peer. A
        duplicate is counted rather than ignored, since gossip delivering the
        same object from several peers is waste worth measuring.

        All three of `held`, `verifying` and `seen` are consulted, not just
        `seen`. Once `seen` is a bounded window it can forget an identity the
        node is still holding, and treating it as the only guard would let that
        object be accepted and processed a second time while already in the view.
        """
        if (obj.id in self.view.held or obj.id in self.view.verifying
                or obj.id in self.view.seen):
            self.view.duplicates += 1
            return False
        self.view.seen.add(obj.id)
        self.view.verifying.add(obj.id)
        return True

    def finish_receive(self, obj, now, body_mask=0):
        """Admit an object whose processing is done.

        `body_mask` is which transaction bodies arrived with it. A full object
        transfer brings every body it covers; a wrapper brings only the subset
        the sender believed this node was missing. So holding an object does not
        imply holding its bodies, and the two are tracked apart.
        """
        self.view.verifying.discard(obj.id)
        self.view.held[obj.id] = now
        self.view.requested.discard(obj.id)
        self.view.bodies |= body_mask

    def abandon_receive(self, obj):
        """Give up on an object whose coverage died while it was being handled.

        It stays in `seen`, so it is not fetched or verified again, but it never
        enters the view and is never relayed: forwarding it would spread work the
        network already knows is dead.
        """
        self.view.verifying.discard(obj.id)
        self.view.requested.discard(obj.id)

    def admit(self, obj, now):
        """Receive and admit synchronously for focused node tests."""
        if not self.begin_receive(obj):
            return False
        self.finish_receive(obj, now)
        return True

    def relay_targets(self, exclude_peer=None):
        """Peers to forward to, never back to where it came from.

        Excluding the sender is what makes flooding terminate; on top of that,
        sending an object back to the peer that just sent it is pure waste.
        """
        targets = []
        for peer in self.peers:
            if peer != exclude_peer:
                targets.append(peer)
        self.view.relayed += len(targets)
        return targets

    def originate(self, obj, now, body_mask=0, relay=True):
        """Admit an object this node created or was handed by a user.

        The creator knows its own object at once -- no hop -- and forwards it
        to every peer. Under a policy that never forwards raw objects the
        caller passes `relay=False`, and no targets are gathered at all --
        gathering and discarding them would still count them as relayed.
        """
        self.view.seen.add(obj.id)
        self.view.held[obj.id] = now
        self.view.bodies |= body_mask
        if not relay:
            return []
        return self.relay_targets(None)

    # -- announce-then-fetch -----------------------------------------------

    def wants(self, obj):
        """Whether an announcement should turn into a request.

        No if it is held or being handled, no if it was seen, and no if a
        request is already outstanding -- one body per node, not one per
        announcing peer. `held` is checked directly because `seen` is a
        forgetful window and may have dropped something still in the view.
        """
        if obj.id in self.view.held or obj.id in self.view.verifying:
            return False
        if obj.id in self.view.seen:
            return False
        return obj.id not in self.view.requested

    def mark_requested(self, obj):
        self.view.requested.add(obj.id)

    def note_duplicate_announcement(self):
        self.view.duplicates += 1

    # -- wrappers ----------------------------------------------------------

    def begin_wrapper(self, proof_id, body_mask, witness_backed=False):
        """Start taking in a wrapper.

        Returns (worth_verifying, fresh_bodies, upgraded): whether this copy
        must be verified, which bodies it alone brought, and whether it raised
        the evidence class of a claim already being checked.

        Deduplicating on identity alone is wrong once a wrapper carries a proof
        *and* a subset of bodies: two deliveries can share a proof yet differ in
        what they supply, so a second copy is only a duplicate if it brings
        neither new evidence nor new bodies. In-flight verification counts as
        evidence pending, so a second copy of a proof already being checked
        merges its bodies rather than paying to verify the same proof twice.

        `witness_backed` says this copy is a mode 0 wrapper: raw witnesses, no
        proof bytes. The two evidence classes are not interchangeable, so a
        recursive proof for a claim already witness-backed is *new* evidence and
        is worth verifying. The reverse is not: witnesses for a claim whose proof
        is held add nothing. When the proof arrives while witnesses for the same
        id are still in flight, the marker is cleared without charging a second
        verification -- the bytes did arrive, and the claim is already being
        checked, so paying twice would price the same work twice. That copy is
        not a duplicate, though it verifies nothing and may carry no fresh
        body: it turns pending witnesses into a pending proof, which changes
        what the claim can later be resolved by reference against. `upgraded`
        is how the caller tells that copy from one that taught nothing.
        """
        settled_witnesses = proof_id in self.view.witness_backed
        known = proof_id in self.view.verified_proofs
        if settled_witnesses and not witness_backed:
            known = False
        in_flight = proof_id in self.view.verifying_proofs
        upgraded = in_flight and settled_witnesses and not witness_backed
        if in_flight and not witness_backed:
            self.view.witness_backed.discard(proof_id)
        elif settled_witnesses and witness_backed:
            known = True
        # Fresh means not held *and* not already on its way in: a body another
        # in-flight wrapper is bringing is not something this one adds.
        pending = 0
        for bodies in self.view.verifying_proofs.values():
            pending |= bodies
        fresh_bodies = body_mask & ~(self.view.bodies | pending)
        if known or in_flight:
            if in_flight:
                self.view.verifying_proofs[proof_id] |= fresh_bodies
            elif fresh_bodies:
                # The proof is already verified, so no evidence is queued, but
                # fresh bodies still arrived and must be retained. Otherwise the
                # receiver would pay for bytes and immediately forget content
                # that the sender now believes it holds. No CPU is charged
                # because `verify_recv` prices proof verification, which this
                # delivery does not repeat.
                self.view.bodies |= fresh_bodies
            if not fresh_bodies and not upgraded:
                self.view.duplicates += 1
            return False, fresh_bodies, upgraded
        self.view.verifying_proofs[proof_id] = fresh_bodies
        if witness_backed:
            self.view.witness_backed.add(proof_id)
        else:
            self.view.witness_backed.discard(proof_id)
        return True, fresh_bodies, False

    def finish_wrapper(self, proof_id):
        """Verification done: install the evidence and the bodies it brought.

        Which evidence was installed depends on what was being verified: a mode 0
        wrapper's witnesses leave the claim witness-backed, and only a recursive
        proof reaches `verified_proofs`. Recording mode 0 as a verified proof
        would let this node be sent that proof's 32-byte hash in place of bytes
        it never received, and would price a later fold over it as verifying one
        nested proof instead of every signature behind it.
        """
        bodies = self.view.verifying_proofs.pop(proof_id, 0)
        if proof_id not in self.view.witness_backed:
            self.view.verified_proofs.add(proof_id)
        self.view.bodies |= bodies
        return bodies

    def abandon_wrapper(self, proof_id):
        """Give up on a wrapper's evidence -- never on its contents.

        The bodies an in-flight copy brought physically arrived, and the
        sender's per-peer body belief formed at delivery, so they will not be
        sent here in full again. Dropping them with the proof would starve this
        node of content the network believes it has.
        """
        bodies = self.view.verifying_proofs.pop(proof_id, 0)
        self.view.witness_backed.discard(proof_id)
        self.view.bodies |= bodies

    def bodies_to_send(self, peer, tx_mask):
        """Which of `tx_mask` must go to `peer` in full rather than as a hash."""
        return self.view.peer_bodies.missing_for(peer, tx_mask)

    def confirm_bodies_delivered(self, peer, body_mask):
        return self.view.peer_bodies.confirm_delivery(peer, body_mask)

    def note_proof_at_peer(self, peer, proof_id):
        """Record that `peer` holds `proof_id` -- delivered to it, or sent by it."""
        self.view.peer_proofs.note(peer, proof_id)

    def peer_has_proof(self, peer, proof_id):
        return self.view.peer_proofs.believes_held(peer, proof_id)

    def can_resolve_proof(self, proof_id):
        """Whether a proof sent by reference names bytes this node has.

        Verified, or still being verified: either way the bytes arrived here
        once in full. Anything else -- including a proof this node abandoned
        under CPU overflow after the sender's belief had already formed, or a
        claim evidenced only by mode 0 witnesses -- is a reference to nothing,
        and treating it as evidence would let a node act on bytes it never held.
        """
        if proof_id in self.view.witness_backed:
            return False
        return (proof_id in self.view.verified_proofs
                or proof_id in self.view.verifying_proofs)

    def receive_bodies(self, body_mask):
        """Install bodies that arrived without usable evidence around them.

        The sender's per-peer body belief formed when the wrapper was
        *delivered*, so these bodies will never be sent here again in full --
        discarding them would starve this node of content the network believes
        it has.
        """
        self.view.bodies |= body_mask

    # -- chain news --------------------------------------------------------

    def hears_about_block(self, block_id):
        """First time this node learns of a block. False if it already knew."""
        if block_id in self.view.seen_blocks:
            return False
        self.view.seen_blocks.add(block_id)
        return True

    # -- lifecycle ---------------------------------------------------------

    def supersede_from_peer(self, sender, new_object, arena, policy):
        """Apply a supersession policy on admitting a wrapper's aggregate.

        Called once per wrapper admission, after `new_object` has entered the
        view. Every aggregate still retained from `sender`'s earlier wrappers
        is put to the policy; the ones it supersedes are dropped and returned.
        The survivors stay tracked, so a policy that keeps an uncovered old
        aggregate gets to reconsider it against the next wrapper rather than
        holding it for ever on one decision.
        """
        known = self.known_discarded()
        surviving = []
        dropping = []
        for object_id in self.view.wrapper_provenance.get(sender, []):
            # Already pruned, evicted or folded away: nothing left to
            # supersede, so simply stop tracking it.
            if object_id not in self.view.held:
                continue
            old_object = arena.objects.get(object_id)
            if old_object is None:
                continue
            if policy(arena, known, old_object, new_object):
                dropping.append(object_id)
            else:
                surviving.append(object_id)
        surviving.append(new_object.id)
        self.view.wrapper_provenance[sender] = surviving
        return self.supersede(dropping)

    def supersede(self, object_ids):
        """Drop objects this node has folded into something newer, returning them.

        Per-node, and never inferred from an object's parents: whether some fold
        consumed a thing is not observable from a wrapper, so a node can only
        drop what it knows it replaced itself.
        """
        dropped = []
        for object_id in object_ids:
            if self.view.held.pop(object_id, None) is not None:
                self.superseded += 1
                dropped.append(object_id)
        return dropped

    def prune_discarded(self, arena):
        """Drop held objects that cover nothing this node still wants."""
        known = self.known_discarded()
        dead = []
        for object_id in self.view.held:
            obj = arena.objects.get(object_id)
            if obj is None or useful_count(arena.claim(obj), known) == 0:
                dead.append(object_id)
        for object_id in dead:
            del self.view.held[object_id]
        self.pruned += len(dead)
        return dead

    def enforce_candidate_limit(self, arena, protected=None):
        """Evict the least useful candidates until the view fits its bound.

        Eviction is loss: the node drops something a real one would have kept, so
        aggregation looks worse than it is. That is why it is counted, and why
        the bound defaults to unbounded until measurement justifies a number --
        too tight a bound biases against the design being evaluated.

        `protected` is an object id the ranking may not drop -- the node's own
        current aggregate, when the caller pins it. The bound then holds
        everything else, so a pinned view is effectively one larger, which is
        the honest cost of the choice.
        """
        limit = self.candidate_limit
        if limit is None or len(self.view.held) <= limit:
            return []
        known = self.known_discarded()
        ranked = []
        for object_id, admitted_at in self.view.held.items():
            if object_id == protected:
                continue
            obj = arena.objects.get(object_id)
            worth = self.eviction(arena, known, obj)
            # Least worth keeping first, and among equals the older admission
            # goes: a stale candidate covering little is the cheapest thing to
            # lose. Ascending sort, so the *smaller* admitted_at must sort
            # first -- negating it put the newest at the front and evicted
            # exactly the wrong end.
            ranked.append((worth, admitted_at, object_id))
        ranked.sort()
        dropped = []
        while len(self.view.held) > limit and ranked:
            _usefulness, _age, object_id = ranked.pop(0)
            if self.view.held.pop(object_id, None) is not None:
                dropped.append(object_id)
        self.evicted += len(dropped)
        return dropped

    # -- selection ---------------------------------------------------------

    def select_by_coverage(self, arena, limit, known_discarded,
                           body_mask=None):
        """Greedily pick up to `limit` held objects covering the most txs.

        Marginal coverage, not total: the second pick is worth only what it adds
        to the first. Selection stops early when nothing left adds anything.

        `body_mask`, when given, is which transactions this node holds the
        body of, and a claim outside it is worth nothing: a block must carry
        each included transaction in its execution payload (`eip-8288.md`,
        builder step 7), so a proof over a body this node lacks cannot be
        turned into an inclusion however wide its claim. `None` means bodies
        are not required under `inclusion_policy="claim_only"`.

        The returned mask is the union of the picked objects' *effective
        claims* -- claims, body-masked when bodies are required -- never their
        structural coverage. Coverage can be wider than claim when a prover
        dropped settled dependencies. Accumulating structural coverage would
        let a block include a transaction no selected proof asserted and expose
        recursive inputs that a wrapper recipient cannot observe.

        This is the proposer's block-assembly rule, separate from aggregation
        selection strategies.
        """
        chosen_mask = 0
        picked = []
        candidates = list(self.view.held)
        while len(picked) < limit:
            best_object = None
            best_claim = 0
            best_gain = 0
            for object_id in candidates:
                obj = arena.objects.get(object_id)
                if obj is None:
                    continue
                claim = arena.claim(obj)
                if body_mask is not None:
                    claim &= body_mask
                gain = marginal_count(claim, known_discarded, chosen_mask)
                if gain > best_gain:
                    best_gain = gain
                    best_object = obj
                    best_claim = claim
            if best_object is None:
                break
            picked.append(best_object)
            chosen_mask |= best_claim
            candidates.remove(best_object.id)
        return picked, chosen_mask

    # -- belief about usefulness ------------------------------------------

    def known_discarded(self):
        """This node's mask of transactions it knows are no longer includable."""
        return self.notifier.known_discarded(self.id)

    # -- invariants --------------------------------------------------------

    def assert_invariants(self, now):
        # A node cannot hold what has not been created yet, and cannot hold
        # anything it has not seen.
        for object_id, admitted_at in self.view.held.items():
            assert admitted_at <= now
        # No relationship is asserted between held and seen: seen is a bounded
        # window that may have forgotten something still held, which is exactly
        # why held is consulted directly when deciding what is a duplicate.
        # Nothing is both held and still awaited.
        for object_id in self.view.requested:
            assert object_id not in self.view.held
        # Nothing is both admitted and still being processed, and anything in
        # flight through processing has at least been seen.
        for object_id in self.view.verifying:
            assert object_id not in self.view.held
        # A proof is either being verified or verified, never both.
        for proof_id in self.view.verifying_proofs:
            assert proof_id not in self.view.verified_proofs
        # Evidence is one class or the other. Witnesses and a recursive proof for
        # the same claim would make `can_resolve_proof` answer both ways.
        for proof_id in self.view.witness_backed:
            assert proof_id not in self.view.verified_proofs

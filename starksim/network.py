#!/usr/bin/env python3
"""
Overlay, link latency and messages.

The overlay is a graph over node ids. Links always supply latency and may have
an optional capacity. The primary bandwidth constraint is each node's egress,
so fan-out shares one sender-side queue.

Graph construction deliberately does *not* use the configuration model with
restart-on-collision: at the degrees we care about the retry rate becomes
impractical and connectivity is never guaranteed anyway. Instead a simple
regular graph is randomised with degree-preserving double-edge swaps, which is
always simple by construction and mixes toward a uniform random regular graph.
"""

from collections import deque

from starksim.derive import stable_fraction
from starksim.payload import (AnnouncePayload, BlockPayload, ObjectPayload,
                              SizeBreakdown, WantPayload, WrapperPayload)


def validate_degree(node_count, degree):
    """Check (N, d) is in the supported domain, with a reason if it is not."""
    if node_count < 1:
        raise ValueError(f"node_count must be >= 1, got {node_count}")
    if degree < 0:
        raise ValueError(f"degree must be >= 0, got {degree}")
    if degree > node_count - 1:
        raise ValueError(
            f"degree {degree} needs at least {degree + 1} nodes, got {node_count}")
    # Handshake counting: every edge contributes one to two nodes' degrees, so
    # the sum of degrees N*d must be even for a d-regular graph to exist.
    if (node_count * degree) % 2 != 0:
        raise ValueError(
            f"no {degree}-regular graph on {node_count} nodes: "
            f"N*d = {node_count * degree} is odd")


def can_be_connected(node_count, degree):
    """Whether a d-regular graph on N nodes can be connected at all.

    Degree 0 is N isolated nodes (connected only when N == 1); degree 1 is a
    perfect matching (connected only when N == 2); degree >= 2 admits a cycle,
    which is connected for any N >= 3.
    """
    if degree == 0:
        return node_count == 1
    if degree == 1:
        return node_count == 2
    return True


def _circulant(node_count, degree):
    """A simple d-regular starting graph: each node joined to its neighbours.

    Offsets 1..d//2 in both directions give an even degree; an odd degree needs
    one more edge per node, which only closes up if every node can be paired
    with the node directly opposite it, hence the even-N requirement that
    `validate_degree` has already checked via N*d being even.
    """
    adjacency = [set() for _ in range(node_count)]
    for offset in range(1, degree // 2 + 1):
        for node in range(node_count):
            other = (node + offset) % node_count
            adjacency[node].add(other)
            adjacency[other].add(node)
    if degree % 2 == 1:
        half = node_count // 2
        for node in range(half):
            other = node + half
            adjacency[node].add(other)
            adjacency[other].add(node)
    return adjacency


def _edge_list(adjacency):
    edges = []
    for node, neighbours in enumerate(adjacency):
        for other in neighbours:
            if node < other:
                edges.append((node, other))
    return edges


def _swap_edges(adjacency, edges, rng, swaps):
    """Degree-preserving double-edge swaps.

    Replacing edges (a,b) and (c,d) with (a,c) and (b,d) leaves every degree
    untouched, so the graph stays regular. A swap is rejected if it would create
    a self-loop or a duplicate edge, which keeps the graph simple throughout --
    the property the configuration model cannot promise.
    """
    if len(edges) < 2:
        return 0
    accepted = 0
    for _ in range(swaps):
        first = rng.randrange(len(edges))
        second = rng.randrange(len(edges))
        if first == second:
            continue
        a, b = edges[first]
        c, d = edges[second]
        if rng.random() < 0.5:
            c, d = d, c
        if a == c or b == d:
            continue
        if c in adjacency[a] or d in adjacency[b]:
            continue
        adjacency[a].discard(b)
        adjacency[b].discard(a)
        adjacency[c].discard(d)
        adjacency[d].discard(c)
        adjacency[a].add(c)
        adjacency[c].add(a)
        adjacency[b].add(d)
        adjacency[d].add(b)
        edges[first] = (min(a, c), max(a, c))
        edges[second] = (min(b, d), max(b, d))
        accepted += 1
    return accepted


def is_connected(adjacency):
    if not adjacency:
        return True
    seen = {0}
    frontier = deque([0])
    while frontier:
        node = frontier.popleft()
        for other in adjacency[node]:
            if other not in seen:
                seen.add(other)
                frontier.append(other)
    return len(seen) == len(adjacency)


def _random_cycle(node_count, rng):
    """A single cycle through a random permutation of the nodes.

    Needed because degree 2 cannot be built by swapping. A 2-regular graph is a
    disjoint union of cycles, and a double-edge swap on one cycle *splits* it
    into two -- so the swap loop drifts away from connectivity rather than
    toward it, and retrying the whole construction only re-rolls the same dice.
    A permutation cycle is connected by construction and uniform over cycles.
    """
    order = list(range(node_count))
    rng.shuffle(order)
    adjacency = [set() for _ in range(node_count)]
    for index, node in enumerate(order):
        following = order[(index + 1) % node_count]
        adjacency[node].add(following)
        adjacency[following].add(node)
    return adjacency


def build_random_regular(node_count, degree, rng, swap_factor=10,
                         require_connected=True, max_attempts=20):
    """Random d-regular adjacency: circulant, then randomised by edge swaps."""
    validate_degree(node_count, degree)
    if require_connected and not can_be_connected(node_count, degree):
        raise ValueError(
            f"a {degree}-regular graph on {node_count} nodes cannot be "
            "connected; pass require_connected=False if that is intended")
    if degree == 2 and node_count >= 3 and require_connected:
        adjacency = _random_cycle(node_count, rng)
        return [tuple(sorted(neighbours)) for neighbours in adjacency]
    for _ in range(max_attempts):
        adjacency = _circulant(node_count, degree)
        edges = _edge_list(adjacency)
        _swap_edges(adjacency, edges, rng, swap_factor * max(1, len(edges)))
        if not require_connected or is_connected(adjacency):
            return [tuple(sorted(neighbours)) for neighbours in adjacency]
    raise RuntimeError(
        f"could not build a connected {degree}-regular graph on {node_count} "
        f"nodes in {max_attempts} attempts")


class UniformLatency:
    """One base latency per link plus a small fixed per-edge jitter.

    The jitter is fixed **per undirected edge**, not per message. Physically,
    propagation delay over a path does not resample itself for every packet;
    operationally, per-message jitter would let two messages to the same peer
    arrive out of order, which breaks the EIP's substitution of a hash for a
    body already sent -- the hash could overtake the body it names.

    It is derived from `(seed, u, v)` by hashing rather than drawn from a shared
    generator, so that **an edge's latency does not depend on the topology it
    sits in**. Drawing from one stream in adjacency order means adding a node,
    or changing the swap count, reshuffles every other edge's latency, and
    parameter sweeps stop being comparable. Hashing also makes the value
    independent of when it is first asked for, so it can be computed lazily.

    Latency is symmetric: distance is the same in both directions. Asymmetry in
    this model comes from congestion on a node's egress, not from geography.
    """

    __slots__ = ("base", "jitter_frac", "seed", "_by_edge")

    def __init__(self, base, jitter_frac=0.0, seed=0):
        if base < 0:
            raise ValueError("base latency must be >= 0")
        if jitter_frac < 0:
            raise ValueError("jitter_frac must be >= 0")
        self.base = float(base)
        self.jitter_frac = float(jitter_frac)
        self.seed = seed
        self._by_edge = {}

    def latency(self, source, target):
        if self.jitter_frac == 0.0:
            return self.base
        if source <= target:
            key = (source, target)
        else:
            key = (target, source)
        value = self._by_edge.get(key)
        if value is None:
            fraction = stable_fraction(self.seed, key[0], key[1])
            value = self.base + self.jitter_frac * self.base * fraction
            self._by_edge[key] = value
        return value


class Overlay:
    """A graph over node ids, plus the latency of each link."""

    __slots__ = ("adjacency", "latency_model", "node_count")

    def __init__(self, adjacency, latency_model):
        self.adjacency = [tuple(sorted(peers)) for peers in adjacency]
        self.latency_model = latency_model
        self.node_count = len(self.adjacency)

    @staticmethod
    def random_regular(node_count, degree, rng, base_latency,
                       jitter_frac=0.0, latency_seed=0, **kwargs):
        adjacency = build_random_regular(node_count, degree, rng, **kwargs)
        latency_model = UniformLatency(base_latency, jitter_frac, latency_seed)
        return Overlay(adjacency, latency_model)

    @staticmethod
    def from_adjacency(adjacency, base_latency, jitter_frac=0.0,
                       latency_seed=0):
        """Build from an explicit graph. Used for line, star and clique tests."""
        latency_model = UniformLatency(base_latency, jitter_frac, latency_seed)
        return Overlay(adjacency, latency_model)

    def peers(self, node_id):
        return self.adjacency[node_id]

    def edges(self):
        """Canonical undirected edges: each pair once, as [min, max], sorted.

        One encoding, fixed here, so a reader never has to guess whether a pair
        appears twice or in which order. Sorted so two runs of the same graph
        produce byte-identical output.
        """
        pairs = []
        for node, neighbours in enumerate(self.adjacency):
            for other in neighbours:
                if node < other:
                    pairs.append([node, other])
        pairs.sort()
        return pairs

    def latency(self, source, target):
        return self.latency_model.latency(source, target)

    def degrees(self):
        return [len(peers) for peers in self.adjacency]

    def is_connected(self):
        return is_connected([set(peers) for peers in self.adjacency])


# -- message kinds ---------------------------------------------------------
MSG_ANNOUNCE = "announce"  # "I have object X"
MSG_WANT = "want"          # "send me object X"


def bytes_per_ms(megabits_per_second):
    """Convert a link rate to bytes per millisecond.

    With milliseconds as the time unit, 1 Mbit/s is 10^6/8 bytes per second,
    which is 125 bytes per millisecond.
    """
    return megabits_per_second * 125.0


class SizeModel:
    """How many bytes each thing costs on the wire.

    Values come from EIP-8288 rather than from taste, and live here so they can
    be swept: a dependency triple is 96 bytes (`:57`), a hash reference 32
    (`:223`), a leanSPHINCS witness a few kB and a leanSTARK witness 128-512 kB
    (Motivation), and a recursive STARK about 256 kB (`:394`).

    Full-object helpers price one dependency and its witness. Wrapper sizing is
    recipient-specific: one proof covers many transactions, and bodies known to
    the peer are replaced by hashes.
    """

    __slots__ = ("triple", "hash_ref", "sig_witness", "stark_witness",
                 "recursive_proof", "tx_body", "announce_overhead")

    def __init__(self, triple=96, hash_ref=32, sig_witness=2560,
                 stark_witness=262144, recursive_proof=262144,
                 tx_body=512, announce_overhead=0):
        self.triple = triple
        self.hash_ref = hash_ref
        self.sig_witness = sig_witness
        self.stark_witness = stark_witness
        self.recursive_proof = recursive_proof
        self.tx_body = tx_body
        self.announce_overhead = announce_overhead

    def object_size(self, obj, dep_count=None):
        """Bytes needed to send `obj` in full.

        This numeric helper remains for transport tests; production sends the
        payload returned by `object_payload`.

        `dep_count` is the number of **distinct** dependencies an aggregate
        claims, i.e. the population count of its coverage. It is required rather
        than defaulted because the tempting alternative -- `obj.total` -- is the
        additive leaf count, which double-counts overlap on purpose. Two racing
        nodes folding the same pair produce aggregates whose merge has total 4
        and a dependency list of 2; charging for 4 triples would invent bytes
        that no wrapper contains.
        """
        if obj.kind == "q":
            # A bare post-quantum signature: the tx, its dependency triple and
            # the raw signature that proves it.
            return self.tx_body + self.triple + self.sig_witness
        if obj.kind == "p":
            # A privacy tx carries its own STARK, which dominates everything.
            return self.tx_body + self.triple + self.stark_witness
        if dep_count is None:
            raise ValueError(
                "an aggregate's size needs its distinct dependency count "
                "(coverage bit count), not its additive total")
        # One recursive proof, plus a triple per transaction it claims so the
        # receiver knows what the proof is about.
        return self.recursive_proof + self.triple * dep_count

    def message_size(self, kind, obj, dep_count=None):
        """Size query retained for focused transport tests."""
        if kind == MSG_ANNOUNCE:
            return self.hash_ref + self.announce_overhead
        if kind == MSG_WANT:
            return self.hash_ref + self.announce_overhead
        return self.object_size(obj, dep_count)

    # -- payload construction ---------------------------------------------
    #
    # Size is decided here, once, and travels with the payload. The transport
    # layer reads one number; nothing downstream re-derives a size from content.

    def announce(self, content_id):
        control = self.hash_ref + self.announce_overhead
        return AnnouncePayload(content_id, SizeBreakdown(control=control))

    def want(self, content_id):
        control = self.hash_ref + self.announce_overhead
        return WantPayload(content_id, SizeBreakdown(control=control))

    def object_payload(self, obj, dep_count=None, solicited=False):
        """A single object sent in full by flood or fetch relay."""
        if obj.kind == "q":
            breakdown = SizeBreakdown(bodies=self.tx_body,
                                      dependencies=self.triple,
                                      proof=self.sig_witness)
        elif obj.kind == "p":
            breakdown = SizeBreakdown(bodies=self.tx_body,
                                      dependencies=self.triple,
                                      proof=self.stark_witness)
        else:
            if dep_count is None:
                raise ValueError(
                    "an aggregate's size needs its distinct dependency count")
            breakdown = SizeBreakdown(dependencies=self.triple * dep_count,
                                      proof=self.recursive_proof)
        return ObjectPayload(obj.id, breakdown, solicited)

    def witness_bytes(self, schemes):
        """What mode 0 costs for these dependencies: their actual witnesses.

        Per scheme, not per count. A leanSPHINCS witness is a few kB and a
        leanSTARK witness is 256 kB, so a mode-0 wrapper over privacy
        transactions costs a hundred times one over signatures, and pricing both
        at the signature size would make mode 0 look universally cheap.

        Schemes rather than objects, because that is all this needs and all a
        node is entitled to: the claim's dependency triples carry the scheme
        (`eip-8288.md:67`), while the witnesses themselves travel only inside
        raw objects.
        """
        total = 0
        for scheme in schemes:
            if scheme == "p":
                total += self.stark_witness
            else:
                total += self.sig_witness
        return total

    def wrapper(self, core, body_mask, proof_by_ref=False, witness_bytes=None):
        """One recipient's copy: bodies for what it lacks, hashes for the rest.

        This is where the EIP's substitution is priced (`eip-8288.md:223`). Mode
        0 carries a witness per dependency; mode 1 carries one recursive proof
        whatever it covers, which is the whole bandwidth argument.

        `proof_by_ref` extends the EIP's body substitution to the proof itself:
        a recipient known to hold this proof gets its 32-byte hash instead of
        its 256 kB. The EIP does not specify this; it is one of the economy
        levers the simulator exists to price.
        """
        body_count = body_mask.bit_count()
        hash_count = (core.tx_mask & ~body_mask).bit_count()
        if proof_by_ref:
            # A reference outranks the mode, including mode 0's witnesses: it
            # names the recursive proof this recipient is known to hold, from an
            # earlier emission that actually carried it, and 32 bytes beat every
            # other encoding. The recipient learns no new evidence from such a
            # copy -- only its bodies -- so it is not witness-backed by it.
            proof = self.hash_ref
        elif core.mode == 1:
            proof = self.recursive_proof
        elif witness_bytes is not None:
            # Mode 0 carries the dependencies' own witnesses, whose size depends
            # on what they are; the caller resolves the kinds and totals them.
            proof = witness_bytes
        else:
            raise ValueError(
                "a mode 0 wrapper needs witness_bytes: its size is the sum of "
                "its dependencies' witnesses, which the size model cannot "
                "infer from a count")
        breakdown = SizeBreakdown(
            bodies=self.tx_body * body_count,
            hashes=self.hash_ref * hash_count,
            dependencies=self.triple * core.dep_count,
            proof=proof)
        return WrapperPayload(core, body_mask, breakdown,
                              proof_by_ref=proof_by_ref)

    def block(self, block_id, slot, included, settled=None):
        """A block notice: the header plus a hash per included transaction."""
        if settled is None:
            settled = included
        breakdown = SizeBreakdown(
            proof=self.recursive_proof,
            hashes=self.hash_ref * included.bit_count())
        return BlockPayload(block_id, slot, included, settled, breakdown)

#!/usr/bin/env python3
"""
Wiring: the layer that owns the engine and turns node decisions into events.

`NetworkSim` connects the domain objects to the event calendar: births,
store-and-forward relay, per-node egress and CPU queues, aggregation ticks,
wrapper delivery, expiry, block building and chain notification. It also owns
measurement and tracing because those observe transitions across the complete
wiring rather than one domain object.
"""

import heapq
import inspect
import math
import random

from starksim import aggregation as _aggregation  # register built-in strategies
from starksim.aggregation import input_composition
from starksim.arena import KIND_PQ, KIND_PRIVACY, Arena, useful_count
from starksim.chain import (ChainState, InstantNotifier, OverlayNotifier,
                            ProposerSchedule)
from starksim.costs import (BUILD_BLOCK, PROVE_COST_BASES, PROVE_COST_CIRCUIT,
                            PROVE_COST_DEPS, PROVE_COST_INPUTS,
                            PROVE_RECURSIVE, VERIFY_RECV,
                            CircuitCost, CostTable)
from starksim.derive import stable_fraction
from starksim.engine import Engine, Job
from starksim.knowledge import KnowledgeTracer
from starksim.network import SizeModel
from starksim.node import Node
from starksim.payload import WireMessage, WrapperCore
from starksim.reporting import (CSV_COLUMNS, _pearson, csv_header, csv_row,
                                admission_delays as _admission_delays,
                                coverage_fraction_within as
                                _coverage_fraction_within,
                                delay_distance_correlation as
                                _delay_distance_correlation,
                                egress_utilisation as _egress_utilisation,
                                hop_distance_from as _hop_distance_from,
                                inclusion_by_distance as _inclusion_by_distance,
                                max_egress_depth as _max_egress_depth,
                                metrics as _metrics,
                                residency_fraction as _residency_fraction,
                                time_to_coverage as _time_to_coverage,
                                variant as _variant)
from starksim.strategies import get as get_strategy
from starksim.trace import TOPOLOGY_EDGE_LIMIT, Tracer

EV_DELIVER = "deliver"
EV_BIRTH = "birth"
EV_SLOT = "slot"
EV_TICK = "tick"
EV_EXPIRE = "expire"
EV_REQUEST_TIMEOUT = "request_timeout"

RELAY_FLOOD = "flood"
RELAY_ANNOUNCE_FETCH = "announce_fetch"
# Raw objects never travel: the witness stays where the transaction was born,
# and everything else -- claims, then bodies -- spreads through wrappers by
# re-aggregation, one tick per hop. The design the wrapper-economy discussion
# asked for: total bandwidth genuinely constant in transaction count, paid for
# in propagation latency quantised by the tick.
RELAY_WRAPPER_ONLY = "wrapper_only"
RELAY_POLICIES = (RELAY_FLOOD, RELAY_ANNOUNCE_FETCH, RELAY_WRAPPER_ONLY)

# How much a node spends on wrappers it strictly need not send. `full` is the
# EIP's stated cadence: one full wrapper per peer per tick, always. The others
# are economies the EIP does not specify, here to be priced: `skip_unchanged`
# stays silent toward a peer known to hold this exact proof when there are no
# new bodies for it; `proof_by_ref` sends such a peer the proof's 32-byte hash
# instead of its 256 kB; `lean` does both. All three rest on beliefs formed at
# *delivery*, so a dropped wrapper self-heals at the next tick.
ECONOMY_FULL = "full"
ECONOMY_SKIP_UNCHANGED = "skip_unchanged"
ECONOMY_PROOF_BY_REF = "proof_by_ref"
ECONOMY_LEAN = "lean"
WRAPPER_ECONOMIES = (ECONOMY_FULL, ECONOMY_SKIP_UNCHANGED,
                     ECONOMY_PROOF_BY_REF, ECONOMY_LEAN)

# What a proposer needs before it may include a transaction. The EIP's builder
# puts every included transaction in the execution payload, which takes its
# body -- so `body_required` is the faithful reading and the default.
# `claim_only` isolates how much apparent throughput depends on treating proof
# possession as sufficient despite missing transaction bodies.
INCLUSION_BODY_REQUIRED = "body_required"
INCLUSION_CLAIM_ONLY = "claim_only"
INCLUSION_POLICIES = (INCLUSION_BODY_REQUIRED, INCLUSION_CLAIM_ONLY)

# How a node's one CPU orders its work ([D20] in the plan:
# docs/history/networking-plan.md). The system's delay
# objective is weighted -- a verification delayed before your slot can cost
# block coverage, a fold delayed the same costs almost nothing -- so `fifo` is
# not delay-optimal; it is the default, while `priority` exposes the effect of
# serving verification and block building ahead of queued proving.
# `priority` serves verify > build > prove, non-preemptively: the choice a
# real implementation would make, without interrupting work in progress.
CPU_FIFO = "fifo"
CPU_PRIORITY = "priority"
CPU_DISCIPLINES = (CPU_FIFO, CPU_PRIORITY)

# `deps` passes the output claim's dependency count to the proving-cost
# callable; `inputs` passes the number of objects folded by that job. See
# `docs/model.md` under "Computation and fold cost".
#
# Selection estimates in whichever unit execution will charge: the wiring
# stamps the basis onto the cost table it hands the strategy. Before that they
# were always per-dependency, and under `inputs` a binding budget then refused
# any selection claiming more than budget/c dependencies -- one, at the default
# budget and a 750 ms per-input cost -- which switched aggregation off while
# appearing to price it.
# `circuit` prices a job by what its inputs actually make the circuit verify:
# signatures, leanSTARK witnesses and nested recursive proofs, each at its own
# weight, with the 2**24 trace length as a hard ceiling on one proof. It needs
# a `CircuitCost` as the proving cost; see `starksim.costs`.
# Which wrapper mode a node emits. `recursive` is the model's original and the
# only one the EIP's steady state describes: always mode 1, one recursive proof
# whatever it covers. `cheapest` implements the EIP's own mode 0 provision
# (`eip-8288.md:244`) as a per-emission decision -- carry the dependencies' raw
# witnesses when they are smaller than a recursive proof, which for signatures
# is true below about a hundred dependencies and for privacy transactions below
# two. In the protocol, choosing mode 0 also means the node need not prove at
# all -- but not here: the mode is decided at *emission*, and an aggregate only
# exists once it has been proved, so this model spends the proving either way
# and substitutes witnesses for the proof's bytes. It is a bandwidth lever
# only, and the compute half is unmodelled. Making it a compute lever means
# deciding before planning, which needs a wrapper that references a claim
# rather than a proof object; see roadmap.md.
# Whether a node folds at all when folding is not worth its bytes. `always` is
# the EIP's cadence: prove and emit every interval regardless. `crossover`
# declines while the claim's own witnesses are smaller than the recursive proof
# that would replace them -- below about a hundred signatures, or two privacy
# transactions -- on the grounds that a 256 kB proof asserting 2 656 B of
# evidence is worse than the evidence. It saves the proof and the emission both,
# and it only makes sense where something else carries the transactions: under
# `wrapper_only` nothing does, so deferring there stalls propagation.
# What makes a node aggregate. Three strategies, and one question: a clock
# shared by the network, a clock each, or no clock.
#
# `aligned` and `spread` are the EIP's cadence (`:242`) with every node's tick
# at the same instant or at a derived offset within the interval -- aligned puts
# every uplink and every CPU to work at once and idle between, which is a
# thundering herd rather than a network, so spread is the default. `when_ready`
# has no clock: a node folds as soon as it is idle and holds enough to fold,
# which is what v1 of this simulator did before the EIP's cadence arrived, and
# what proposal S5 asks for. The trade is predictability -- a fixed interval is
# what makes the standing bandwidth cost calculable as
# `degree x recursive_proof / interval`, and without one the emission rate is a
# consequence of load rather than a constant.
#
# One field rather than a trigger and a phase: a phase is only meaningful under
# a clock, so splitting them would invent a combination to refuse.
TRIGGER_ALIGNED = "aligned"
TRIGGER_SPREAD = "spread"
TRIGGER_WHEN_READY = "when_ready"
AGGREGATION_TRIGGERS = (TRIGGER_ALIGNED, TRIGGER_SPREAD, TRIGGER_WHEN_READY)
CLOCKED_TRIGGERS = (TRIGGER_ALIGNED, TRIGGER_SPREAD)

# What a node must hold before folding is worth waking for. Two, because that is
# the cheapest fold; below it a tick can only re-wrap or prove a lone object,
# which no arrival makes newly worthwhile.
FOLDABLE_TO_WAKE = 2

FOLD_ALWAYS = "always"
FOLD_AT_CROSSOVER = "crossover"
FOLD_THRESHOLDS = (FOLD_ALWAYS, FOLD_AT_CROSSOVER)

WRAPPER_MODE_RECURSIVE = "recursive"
WRAPPER_MODE_CHEAPEST = "cheapest"
WRAPPER_MODES = (WRAPPER_MODE_RECURSIVE, WRAPPER_MODE_CHEAPEST)

_CPU_RANK = {VERIFY_RECV: 0, BUILD_BLOCK: 1, PROVE_RECURSIVE: 2}

# Settings that cannot affect a run, given the rest of it. Choosing one of these
# and getting a result is the shape of every measurement fault this project has
# recorded: the configuration looks like it is asking a question and is not.
# So a *chosen* value that cannot matter is refused at construction, loudly and
# before any simulated time passes, rather than being quietly ignored.
#
# Chosen, not merely present: the rule compares against the signature default,
# so leaving a field alone is never an error and passing its default explicitly
# is indistinguishable from that. It refuses "cannot affect", never "might
# surprise" -- setting `candidate_limit` without expecting eviction is a
# legitimate experiment, while `request_timeout` under `flood` cannot do
# anything at all.
INERT_SETTINGS = (
    ("fold_amortize",
     lambda sim: sim.fold_threshold != FOLD_AT_CROSSOVER,
     "it scales the crossover comparison, and no other threshold compares"),
    ("eviction_policy",
     lambda sim: sim.nodes and sim.nodes[0].candidate_limit is None,
     "it decides what a full candidate set drops, and without "
     "candidate_limit no set is ever full"),
    ("request_timeout",
     lambda sim: sim.relay_policy != RELAY_ANNOUNCE_FETCH,
     "it retries an unanswered want, and only announce_fetch sends wants"),
    ("entry_subset",
     lambda sim: sim.entry_subset is not None
     and sim.entry_subset >= sim.overlay.node_count,
     "a subset as large as the network is every node, which is what None "
     "already means"),
    ("aggregation_interval",
     lambda sim: sim.aggregation_trigger not in CLOCKED_TRIGGERS,
     "it spaces a clock, and 'when_ready' has none"),
    ("trace_follow_rate",
     lambda sim: sim.trace_follow is not None,
     "an explicit trace_follow list decides what is followed, and a rate is "
     "only consulted when there is no list"),
    # `trace_follow_seed` is inert the same way when no rate is drawn, but it
    # is resolved to `seed` during construction, so the rule -- which compares
    # against the signature default -- cannot tell a chosen value from a
    # defaulted one. Left unguarded rather than guarded wrongly.
    ("pin_own_aggregate",
     lambda sim: sim.nodes and sim.nodes[0].candidate_limit is None,
     "it protects one object from eviction, and without candidate_limit "
     "nothing is ever evicted"),
    ("on_tick_overrun",
     lambda sim: sim.aggregation_trigger not in CLOCKED_TRIGGERS,
     "it decides what happens to work still waiting when the next tick "
     "arrives, and 'when_ready' only folds a node that is already idle"),
)


def _cpu_priority_rank(job):
    # Unknown kinds rank last: new work classes should be scheduled
    # deliberately, not accidentally ahead of proving.
    return _CPU_RANK.get(job.kind, 3)


class _PlanRun:
    """One tick's plan while it is being proved.

    Mutable on purpose, and deliberately not part of the plan: the plan is an
    immutable decision, this is the state of carrying it out.
    """

    __slots__ = ("plan", "node_id", "tick", "produced", "resolved", "claims")

    def __init__(self, plan, node_id, tick):
        self.plan = plan
        self.node_id = node_id
        self.tick = tick
        self.produced = {}
        self.resolved = {}
        # Each job's frozen claim, decided at its start.
        self.claims = {}


def _message_bytes(job):
    """A queued send's weight: the bytes of the message it carries."""
    return job.tag.size_bytes()


class NetworkSim:
    """A network of mempool nodes over an overlay, driven by one engine."""

    __slots__ = (
        # Wiring.
        "engine", "overlay", "arena", "chain", "notifier", "nodes",
        "proposers", "seed", "rng_entry", "size_model", "egress",
        "link_servers", "rotation", "costs", "verify_cost",
        "_selection_costs", "cpu",
        "_request_sources",
        "knowledge", "_departed", "_trace_finished", "_expiry_queue",
        "own_aggregate", "entry_nodes", "entry_of", "tx_records", "tracer",
        "_next_tick", "_next_slot",
        # Policy.
        "capacity", "link_capacity", "relay_policy", "egress_queue_bytes",
        "cpu_queue_jobs", "request_timeout", "trace_follow",
        "trace_follow_rate", "trace_follow_seed", "slot_length",
        "block_budget", "aggregation_interval",
        "aggregation_trigger", "_next_generation",
        "selection_strategy", "fold_plan", "supersede_policy",
        "_supersede_strategy", "eviction_policy", "wrapper_economy",
        "inclusion_policy", "cpu_discipline", "prove_cost_basis",
        "wrapper_mode", "mode_zero_wrappers",
        "fold_threshold", "fold_amortize", "folds_deferred",
        "on_tick_overrun", "proving_budget", "entry_subset",
        "_dedicated_builder", "_material", "_woke_at",
        "pin_own_aggregate",
        # Accounting.
        "messages_offered", "messages_delivered", "duplicates",
        "offered_bytes", "sent_bytes", "offered_bytes_by_kind",
        "offered_messages_by_kind", "offered_kind", "sent_kind",
        "ingress_kind", "duplicate_bytes", "ingress_bytes",
        "ingress_messages", "ingress_duplicate_bytes", "unserved_wants",
        "dropped_messages", "dropped_bytes", "dropped_by_kind",
        "dropped_by_node", "dropped_bytes_by_node", "requests_timed_out",
        "requests_retried", "arrived_txs", "delay_sum", "delays",
        "slots_run", "discarded_after_work", "bodies_pushed",
        "bodies_requested", "wrapper_sends_suppressed",
        "wrapper_refs_unresolved", "build_jobs_refused", "_next_wrapper_id",
        "ticks_run", "wrappers_emitted", "plans_abandoned",
        "proofs_completed", "stale_proofs", "stale_deps_proved",
        "block_notices", "stale_by_node", "proof_jobs_cancelled",
        "proof_jobs_over_capacity",
    )

    def __init__(self, overlay, seed=42, size_model=None, capacity=None,
                 link_capacity=None, relay_policy=RELAY_FLOOD,
                 proposer_mode="fixed", proposer_id=0,
                 pin_own_aggregate=True,
                 engine=None, chain=None,
                 chain_notify="instant", costs=None, verify_cost=None,
                 slot_length=4000.0,
                 block_budget=5, seen_limit=None, candidate_limit=None,
                 coverage_cache_limit=None, aggregation_interval=1000.0,
                 aggregation_trigger=TRIGGER_SPREAD,
                 selection_strategy="eip_priority",
                 fold_plan="wide", supersede_policy="keep_all",
                 eviction_policy="usefulness",
                 wrapper_economy=ECONOMY_FULL,
                 inclusion_policy=INCLUSION_BODY_REQUIRED,
                 cpu_discipline=CPU_FIFO,
                 prove_cost_basis=PROVE_COST_DEPS,
                 wrapper_mode=WRAPPER_MODE_RECURSIVE,
                 fold_threshold=FOLD_ALWAYS, fold_amortize=1.0,
                 on_tick_overrun="fifo",
                 proving_budget=None, entry_subset=None,
                 trace=None, trace_focus=None,
                 trace_sample=1, trace_counter_interval=1000.0,
                 trace_follow=None, trace_follow_rate=0.0,
                 trace_follow_seed=None,
                 egress_queue_bytes=None, cpu_queue_jobs=None,
                 request_timeout=None):
        if relay_policy not in RELAY_POLICIES:
            raise ValueError(f"unknown relay policy {relay_policy!r}")
        if wrapper_economy not in WRAPPER_ECONOMIES:
            raise ValueError(f"unknown wrapper economy {wrapper_economy!r}")
        if inclusion_policy not in INCLUSION_POLICIES:
            raise ValueError(f"unknown inclusion policy {inclusion_policy!r}")
        if cpu_discipline not in CPU_DISCIPLINES:
            raise ValueError(f"unknown cpu discipline {cpu_discipline!r}")
        if prove_cost_basis not in PROVE_COST_BASES:
            raise ValueError(f"unknown prove cost basis {prove_cost_basis!r}")
        if wrapper_mode not in WRAPPER_MODES:
            raise ValueError(f"unknown wrapper mode {wrapper_mode!r}")
        if fold_threshold not in FOLD_THRESHOLDS:
            raise ValueError(f"unknown fold threshold {fold_threshold!r}")
        fold_amortize = float(fold_amortize)
        if fold_amortize < 1.0:
            raise ValueError("fold_amortize must be >= 1.0: below one it "
                             "would discount the witnesses a fold replaces, "
                             "which nothing downstream ever does")
        if prove_cost_basis == PROVE_COST_CIRCUIT:
            table = costs if costs is not None else CostTable(seed=seed)
            if not isinstance(table.costs[PROVE_RECURSIVE], CircuitCost):
                raise ValueError(
                    "prove_cost_basis='circuit' needs a CircuitCost as the "
                    "prove_recursive cost: a scalar cannot say what a job "
                    "makes the circuit verify")
        if capacity is not None and capacity <= 0:
            raise ValueError("capacity must be > 0, or None for unmetered")
        if link_capacity is not None and link_capacity <= 0:
            raise ValueError("link_capacity must be > 0, or None for no cap")
        self.overlay = overlay
        self.engine = engine if engine is not None else Engine()
        self.arena = Arena(coverage_cache_limit=coverage_cache_limit)
        self.chain = chain if chain is not None else ChainState()
        if chain_notify == "instant":
            self.notifier = InstantNotifier(self.chain)
        elif chain_notify == "overlay":
            self.notifier = OverlayNotifier(self.chain, overlay.node_count)
        else:
            raise ValueError(f"unknown chain_notify {chain_notify!r}")
        self.size_model = size_model if size_model is not None else SizeModel()
        # Node-egress bytes per unit of simulated time. None means unmetered:
        # transmission takes no time and only propagation latency remains.
        self.capacity = capacity
        self.link_capacity = link_capacity
        self.relay_policy = relay_policy
        self.seed = seed
        self.rng_entry = random.Random(f"{seed}:entry")
        self.nodes = []
        self.egress = []
        # Fetched once, so an unknown name fails at construction; every node
        # scores eviction the same way, which is what makes the policy a
        # variant axis rather than per-node noise.
        self.eviction_policy = eviction_policy
        # Every other policy name is checked here, so these are too: resolved
        # and discarded, because a tick looks them up by name. Left lazy, a
        # misspelled strategy constructed cleanly and raised KeyError at the
        # first tick with work, which points at the duty cycle rather than at
        # the caller that made the typo.
        get_strategy("selection", selection_strategy)
        get_strategy("fold_plan", fold_plan)
        eviction = get_strategy("eviction", eviction_policy)
        for node_id in range(overlay.node_count):
            self.nodes.append(Node(node_id, overlay.peers(node_id),
                                   self.notifier, seen_limit=seen_limit,
                                   candidate_limit=candidate_limit,
                                   eviction=eviction))
            # Bounded in *bytes*: a send buffer holds bytes, not messages, and
            # a bound in messages would let one 256 kB proof and one 32-byte
            # announcement occupy the same space.
            self.egress.append(self.engine.new_server(
                f"egress:{node_id}", egress_queue_bytes,
                None if egress_queue_bytes is None else _message_bytes))
        self.link_servers = {}
        self.costs = costs if costs is not None else CostTable(seed=seed)
        self.verify_cost = verify_cost
        # One CPU per node, serialising everything it computes: processing a
        # received message, proving an aggregate, building a block. The whole
        # point of a single server is that these compete.
        self.cpu = []
        if cpu_discipline == CPU_PRIORITY:
            cpu_rank = _cpu_priority_rank
        else:
            cpu_rank = None
        for node_id in range(overlay.node_count):
            # Bounded in *jobs*: a work queue holds units of work, and the
            # cost table already prices how long each takes.
            self.cpu.append(self.engine.new_server(
                f"cpu:{node_id}", cpu_queue_jobs, rank=cpu_rank))
        self.slot_length = float(slot_length)
        # Folds the proposer may spend, so up to budget+1 objects per block: a
        # binary tree over k leaves costs k-1 folds.
        self.block_budget = int(block_budget)
        # Per-node fan-out rotation. One FIFO egress plus a fixed peer order
        # would give low-id peers a permanent head start on every fan-out, so
        # the starting peer advances each time. Deterministic, so runs stay
        # reproducible while no peer is structurally favoured.
        self.rotation = [0] * overlay.node_count
        self.proposers = ProposerSchedule(overlay.node_count, proposer_mode,
                                          proposer_id, seed)
        # Byte accounting keeps *offered* separate from *sent*, because they
        # differ by whatever is still queued. Counting only at submit time
        # reports bytes that never crossed a wire -- badly enough, at a finite
        # horizon on a saturated uplink, to exceed the physical capacity of the
        # link and so be obviously impossible.
        self.messages_offered = 0
        self.messages_delivered = 0
        self.duplicates = 0
        self.offered_bytes = [0] * overlay.node_count
        self.sent_bytes = [0] * overlay.node_count
        self.offered_bytes_by_kind = {}
        # Per node and per payload kind, cumulative. `snd` cannot answer this:
        # it records the size offered at enqueue, so it establishes neither the
        # completion of egress nor arrival -- and it is the one event kind
        # sampling thins, so scaling it would be a systematic sample dressed up
        # as a total. These are exact and never sampled.
        self.offered_kind = [dict() for _ in range(overlay.node_count)]
        self.sent_kind = [dict() for _ in range(overlay.node_count)]
        self.ingress_kind = [dict() for _ in range(overlay.node_count)]
        self.offered_messages_by_kind = {}
        self.duplicate_bytes = 0
        # Ingress is measured but not metered: no capacity, no queue, so no
        # meaningful queue depth -- but the volume arriving is worth knowing
        # before deciding whether a receive-side bottleneck needs modelling.
        self.ingress_bytes = [0] * overlay.node_count
        self.ingress_messages = [0] * overlay.node_count
        self.ingress_duplicate_bytes = [0] * overlay.node_count
        self.unserved_wants = 0
        self.arrived_txs = 0
        self.delay_sum = 0.0
        self.delays = []
        self.slots_run = 0
        self.discarded_after_work = 0
        # A body is the same bytes whether pushed or asked for; which it was is
        # the signature of the relay policy.
        self.bodies_pushed = 0
        self.bodies_requested = 0
        # Leaves ordered by when they stop being includable. A heap rather than
        # a scan: sweeping every transaction at every boundary is the kind of
        # per-slot cost that stops a ten-thousand-node run from finishing.
        self._expiry_queue = []
        self.aggregation_interval = float(aggregation_interval)
        if aggregation_trigger not in AGGREGATION_TRIGGERS:
            raise ValueError(
                f"unknown aggregation_trigger {aggregation_trigger!r}")
        self.aggregation_trigger = aggregation_trigger
        # Generation numbers replace tick numbers when there is no clock. They
        # only have to increase: cancellation compares them, and nothing reads
        # them as a time.
        self._next_generation = [0] * overlay.node_count
        # `when_ready` folds when a node is idle *and* something changed. The
        # counter is what changed: an admission, a birth, or a completed proof.
        # Without it an idle CPU re-triggers the same decision for ever -- a
        # node whose plan cannot complete abandons and retries as fast as the
        # CPU allows, which is a livelock rather than a busy node.
        self._material = [0] * overlay.node_count
        self._woke_at = [None] * overlay.node_count

        # Real nodes do not tick together. Spreading the phase avoids a
        # thundering herd that would make every uplink busy at the same instant
        # and idle between; aligned is kept because it is the clearer picture
        # when debugging.
        self.selection_strategy = selection_strategy
        self.fold_plan = fold_plan
        self.supersede_policy = supersede_policy
        # Fetched once here rather than per admission, so an unknown name
        # fails at construction instead of minutes into a run.
        self._supersede_strategy = get_strategy("supersede", supersede_policy)
        self.wrapper_economy = wrapper_economy
        self.wrapper_sends_suppressed = 0
        self.wrapper_refs_unresolved = 0
        self.build_jobs_refused = 0
        self.inclusion_policy = inclusion_policy
        self.cpu_discipline = cpu_discipline
        self.prove_cost_basis = prove_cost_basis
        # Selection is handed a table that knows how execution will size a job,
        # so an estimate cannot be taken in a unit nothing charges.
        self._selection_costs = self.costs.for_basis(prove_cost_basis)
        self.wrapper_mode = wrapper_mode
        self.mode_zero_wrappers = 0
        self.fold_threshold = fold_threshold
        self.fold_amortize = fold_amortize
        self.folds_deferred = 0
        if on_tick_overrun not in ("fifo", "abandon"):
            raise ValueError(f"unknown on_tick_overrun {on_tick_overrun!r}")
        self.on_tick_overrun = on_tick_overrun
        # Proving must fit in a tick, or a node falls permanently behind. None
        # means the interval itself.
        if proving_budget is None:
            if self.aggregation_trigger not in CLOCKED_TRIGGERS:
                raise ValueError(
                    "aggregation_trigger='when_ready' has no interval for "
                    "proving_budget to default to. Pass one explicitly -- a "
                    "large number for an unbounded budget -- because a budget "
                    "invented here would decide how wide every claim gets.")
            self.proving_budget = self.aggregation_interval
        else:
            self.proving_budget = float(proving_budget)
        self._next_wrapper_id = 0
        # The aggregate each node proved last tick: tier 1 of the EIP's order.
        self.own_aggregate = [None] * overlay.node_count
        self.ticks_run = 0
        self.wrappers_emitted = 0
        self.plans_abandoned = 0
        self.proof_jobs_cancelled = 0
        self.proof_jobs_over_capacity = 0
        self._next_tick = 1
        self._next_slot = 1
        self.proofs_completed = 0
        self.stale_proofs = 0
        # Dependencies that were already included when a proof over them began.
        # This is the cost of learning late: a node that has not yet heard about
        # a block keeps proving what that block already settled. Structurally
        # zero under instant notification, which is why it must be reported as
        # unavailable there rather than as a measurement of zero.
        self.stale_deps_proved = 0
        self.block_notices = 0
        # Per node, because the aggregate hides the effect: a latency sweep
        # changes both how late the news arrives *and* how much a node has to
        # prove, so the total is not monotonic in latency even though the
        # mechanism is. Distance from the proposer isolates lateness.
        self.stale_by_node = [0] * overlay.node_count
        # Where transactions enter the overlay. `None` is every node, which is
        # the neutral default; a count concentrates entry, which is what makes
        # diffusion partial enough for proposer placement to matter at all. One
        # field rather than a policy name and a size: a size without the policy
        # does nothing and the policy without a size is the default, so the two
        # only ever said one thing -- which is what the variant has always
        # reported, as a resolved `entry_nodes` count.
        #
        # The subset is derived rather than the first k ids, so it does not sit
        # next to whichever node happens to be proposer zero.
        if entry_subset is not None and entry_subset < 1:
            raise ValueError(
                f"entry_subset must be at least 1, got {entry_subset!r}; "
                "pass None for every node")
        self.entry_subset = entry_subset
        # Whether a bounded candidate set may drop the node's own current
        # aggregate. The EIP's first priority is "its own aggregate from the
        # previous round", which presumes the node still has it, and a node
        # would not discard the proof it is about to emit. But a hard bound is
        # hard: exempting an object means the set really holds one more. Both
        # are defensible node behaviours and the difference is measurable, so
        # it is a choice rather than a fixed rule -- and what continuity is
        # worth has never been measured.
        self.pin_own_aggregate = pin_own_aggregate
        # v1 had N aggregators and, separately, one builder that only built.
        # The EIP has no such role -- a proposer is a mempool node like any
        # other -- so `dedicated` exists to isolate the difference rather than
        # to recommend it, and every other mode leaves the proposer folding.
        if proposer_mode == "dedicated":
            self._dedicated_builder = proposer_id
        else:
            self._dedicated_builder = None
        if entry_subset is None:
            self.entry_nodes = tuple(range(overlay.node_count))
        else:
            ranked = sorted(range(overlay.node_count),
                            key=lambda node: stable_fraction(seed, "entry",
                                                             node))
            self.entry_nodes = tuple(sorted(ranked[:entry_subset]))
        # Which node each transaction entered at, so an inclusion delay can be
        # attributed to a position in the graph and not only to a time.
        self.entry_of = {}
        self.tx_records = []
        edges = None
        if trace is not None:
            degrees = overlay.degrees()
            directed = sum(degrees)
            if directed <= TOPOLOGY_EDGE_LIMIT:
                edges = overlay.edges()
        if trace_follow is not None:
            follow = {"mode": "explicit", "indices": sorted(trace_follow)}
        elif trace_follow_rate > 0.0:
            follow = {"mode": "bernoulli", "rate": trace_follow_rate,
                      "seed": str(seed if trace_follow_seed is None
                                  else trace_follow_seed)}
        else:
            follow = {"mode": "none"}
        self.tracer = Tracer(trace, trace_focus, trace_sample,
                             trace_counter_interval, edges, follow)
        # Registered whether or not a trace stream exists. The observer advances
        # the clock to each boundary as it passes, so registering it only when
        # tracing would give a traced run a different clock trajectory from an
        # untraced one. Instrumentation must not change the clock trajectory;
        # `_trace_counters` returns immediately when tracing is disabled, so the
        # inactive cost is one call per boundary.
        self.engine.observe_every(trace_counter_interval, self._trace_counters)
        # Two selection modes and no third. An explicit list is exact and makes
        # no claim to being representative; a rate is an independent Bernoulli
        # trial per transaction, which is defensible but yields `p*T` rather
        # than a fixed count. A fixed uniform K cannot be drawn online without
        # biasing toward whichever transactions were born first.
        if trace_follow is None:
            self.trace_follow = None
        else:
            self.trace_follow = frozenset(trace_follow)
        self.trace_follow_rate = trace_follow_rate
        if trace_follow_seed is None:
            trace_follow_seed = seed
        self.trace_follow_seed = trace_follow_seed
        self.egress_queue_bytes = egress_queue_bytes
        self.cpu_queue_jobs = cpu_queue_jobs
        self.request_timeout = request_timeout
        self.dropped_messages = 0
        self.dropped_bytes = 0
        self.dropped_by_kind = {}
        # Per node as well as in total: "the network dropped 1580 messages" and
        # "four nodes dropped everything while the rest dropped nothing" are
        # different situations, and only the second one names a hotspot.
        self.dropped_by_node = [0] * overlay.node_count
        self.dropped_bytes_by_node = [0] * overlay.node_count
        self.requests_timed_out = 0
        self.requests_retried = 0
        # Who else announced an object a node is already waiting for. Without
        # this a retry has nowhere to go: the peer that was asked is the one
        # whose answer went missing, and asking it again is the least promising
        # move available. Cleared when the request resolves, so it is bounded by
        # outstanding requests rather than by run length.
        self._request_sources = {}
        self._departed = set()
        self._trace_finished = False
        self.knowledge = KnowledgeTracer(
            self.nodes, self.arena,
            lambda engine=self.engine: engine.now, self.tracer)
        self.engine.after_event(self.knowledge.flush)
        self.engine.on(EV_DELIVER, self._on_deliver)
        self.engine.on(EV_BIRTH, self._on_birth)
        self.engine.on(EV_SLOT, self._on_slot)
        self.engine.on(EV_TICK, self._on_tick)
        self.engine.on(EV_EXPIRE, self._on_expire)
        self.engine.on(EV_REQUEST_TIMEOUT, self._on_request_timeout)
        # A node folds when it is idle and has something to fold, so "this CPU
        # ran out of work" is the event that matters -- not any particular kind
        # of job finishing. Hanging the wake off individual completions left
        # verification, block assembly and abandoned plans as ways to miss it,
        # twice over.
        for node_id, server in enumerate(self.cpu):
            server.on_idle = (
                lambda _server, node_id=node_id: self._wake_if_ready(node_id))
        self._refuse_inert_settings()
        self.tracer.header(self.variant())

    def _refuse_inert_settings(self):
        """Refuse a chosen setting that this configuration cannot act on."""
        for field, is_inert, why in INERT_SETTINGS:
            chosen = getattr(self, field)
            if chosen == _field_default(field):
                continue
            if is_inert(self):
                raise ValueError(
                    f"{field}={chosen!r} cannot affect this run: {why}. "
                    "Remove it, or configure the run so that it can.")

    # -- injecting load ----------------------------------------------------

    def schedule_birth(self, t, kind, node_id=None, fee=0.0, lifetime=None):
        """Schedule a transaction to be broadcast at `t` into `node_id`.

        With no node given the entry node is drawn uniformly. Concentrating
        entry points instead is what makes diffusion partial enough for proposer
        placement to matter, so it is a policy rather than a constant -- but
        uniform is the neutral default.
        """
        self._require_unfinished("schedule_birth")
        self.engine.schedule_at(t, EV_BIRTH, (kind, node_id, fee, lifetime))

    def _on_birth(self, engine, payload):
        kind, node_id, fee, lifetime = payload
        self._birth(kind, node_id, fee, lifetime)

    def _birth(self, kind, node_id, fee=0.0, lifetime=None):
        if node_id is None:
            node_id = self.entry_nodes[
                self.rng_entry.randrange(len(self.entry_nodes))]
        now = self.engine.now
        if lifetime is None:
            expiry = None
        else:
            expiry = now + lifetime
        obj = self.arena.new_leaf(kind, now, fee=fee, expiry=expiry)
        # `tx` as well as `o`: coverage masks, settlement masks and followed
        # selection are all keyed on the leaf index, and the two namespaces
        # diverge as soon as anything folds -- a leaf advances both counters, an
        # aggregate only the object id. Without this a transaction-level record
        # cannot be joined back to its birth.
        self.tracer.emit("arr", node_id, t=round(now, 3), o=obj.id,
                         tx=obj.leaf_index, k=kind, fee=fee)
        if expiry is not None:
            heapq.heappush(self._expiry_queue, (expiry, obj.leaf_index))
            # Its own event, so a deadline is honoured when it falls rather than
            # whenever a slot next happens to fire. Without scheduled slots a
            # transaction stayed provable indefinitely past its expiry.
            self.engine.schedule_at(expiry, EV_EXPIRE, None)
        body_mask = self.arena.coverage(obj)
        if self._is_followed(obj.leaf_index):
            self.knowledge.follow(obj.leaf_index)
        self.entry_of[obj.leaf_index] = node_id
        self.arrived_txs += 1
        node = self.nodes[node_id]
        # The entry node is the only one whose knowledge a birth changes, so it
        # is the only one marked. `follow` above deliberately dirties nothing.
        self.knowledge.mark(node_id, 1 << obj.leaf_index)
        targets = node.originate(obj, now, body_mask,
                                 relay=self.relay_policy != RELAY_WRAPPER_ONLY)
        self._trace_drops(node.id, node.enforce_candidate_limit(
                              self.arena, self._protected_candidate(node.id)),
                          "evicted")
        self._advertise(node_id, targets, obj)
        self._material[node_id] += 1
        # After `originate`, which is where the entry node actually takes the
        # object. A locally born transaction is new material like any other,
        # and missing this stranded a node whose objects all arrived by birth
        # rather than by delivery: nothing else would ever wake it.
        self._wake_if_ready(node_id)
        return obj

    def _delivered_leaves(self, message):
        """Which leaves a message could change knowledge of, as a mask.

        Coverage rather than claim: a delivery brings the bodies its structure
        covers, and this is about what the receiver may now know rather than
        what a proof asserts.

        `None` where the payload names nothing resolvable -- a block notice, say,
        whose effect is on settlement rather than on any one object. `None` means
        "could be anything", which is the correct-but-slow answer and never the
        wrong one.
        """
        payload = message.payload
        kind = payload.kind
        if kind in ("announce", "want", "object"):
            object_id = payload.content_id
        elif kind == "wrapper":
            object_id = payload.core.proof_object_id
        else:
            return None
        obj = self.arena.objects.get(object_id)
        if obj is None:
            return None
        return self.arena.coverage(obj)

    def _delivered_object(self, message):
        """What a message delivered, for attributing a knowledge transition.

        The object id for an object, wrapper, announce or want; `"block"` for a
        block notice, whose effect is settlement rather than any one object.
        """
        payload = message.payload
        kind = payload.kind
        if kind in ("announce", "want", "object"):
            return payload.content_id
        if kind == "wrapper":
            return payload.core.proof_object_id
        if kind == "block":
            return "block"
        return None

    def _delivered_bodies(self, message):
        """The transaction bodies a message carried, as a mask.

        A full object brings every body its structure covers; a wrapper brings
        exactly its `body_mask`, whether or not its proof is kept.
        """
        payload = message.payload
        if payload.kind == "object":
            return self._body_mask_of(payload.content_id)
        if payload.kind == "wrapper":
            return payload.body_mask
        return 0

    def _trace_duplicate(self, message, object_id, claim=None):
        """Record a delivery that taught the receiver nothing.

        Only when the object covers a followed transaction, and naming those
        transactions: a duplicate is the cost of spreading, and the trace's
        spread record is per followed transaction, so this is bounded the way
        `knw` is rather than the way `snd` is. Block notices are not here --
        a block covers everything it settled, not any one transaction.

        `claim` is what the delivery itself named, when that is narrower than
        the object's structure: a wrapper carries its own `tx_mask`, and a
        duplicate copy must not be charged to a transaction the wrapper never
        claimed just because the aggregate behind it structurally covers one.
        """
        if not self.knowledge.active():
            return
        obj = self.arena.objects.get(object_id)
        if obj is None:
            return
        coverage = self.arena.coverage(obj)
        if claim is not None:
            coverage &= claim
        followed = sorted(leaf for leaf in self.knowledge.followed
                          if coverage >> leaf & 1)
        if not followed:
            return
        self.tracer.emit("dup", message.receiver, t=round(self.engine.now, 3),
                         peer=message.sender, o=object_id,
                         k=message.payload.kind, b=message.size_bytes(),
                         txs=followed)

    def _is_followed(self, leaf_index):
        """Whether this transaction's knowledge transitions are traced.

        Keyed on the leaf index, never the object id: a leaf advances both
        counters and an aggregate only the object id, so the namespaces diverge
        as soon as anything folds.
        """
        if self.trace_follow is not None:
            return leaf_index in self.trace_follow
        if self.trace_follow_rate <= 0.0:
            return False
        drawn = stable_fraction(self.trace_follow_seed, "trace-follow",
                                leaf_index)
        return drawn < self.trace_follow_rate

    # -- relay policy ------------------------------------------------------

    def _advertise(self, sender_id, targets, obj):
        """Tell peers about an object, per the relay policy."""
        # wrapper_only never advertises; its callers pass no targets. Kept as a
        # guard so a stray target list cannot leak an object onto the wire.
        if self.relay_policy == RELAY_WRAPPER_ONLY:
            return
        for target in self._rotated(sender_id, targets):
            if self.relay_policy == RELAY_FLOOD:
                payload = self.size_model.object_payload(
                    obj, self._dep_count(obj))
            else:
                payload = self.size_model.announce(obj.id)
            self._send(sender_id, target, payload)

    def _rotated(self, sender_id, targets):
        """Fan-out order, advancing around the node's stable peer ring.

        The rotation is over `peers`, not over the filtered target list. Rotating
        the filtered list means the offset walks a sequence whose length changes
        with whichever peer is excluded, so it is not a rotation of anything
        stable and some eligible peer can stay disadvantaged. Rotating the ring
        first and filtering after gives every peer the lead equally often
        whatever the exclusions are.
        """
        if len(targets) < 2:
            return list(targets)
        peers = self.nodes[sender_id].peers
        offset = self.rotation[sender_id] % len(peers)
        self.rotation[sender_id] += 1
        ring = list(peers[offset:]) + list(peers[:offset])
        wanted = set(targets)
        return [peer for peer in ring if peer in wanted]

    # -- transmission ------------------------------------------------------

    def _object_of(self, message):
        """The object a payload refers to, looked up rather than handed over.

        A payload carries an identity; resolving it is an explicit step through
        the shared arena. Carrying the reference instead is how a node ends up
        able to act on something it was never told.
        """
        return self.arena.objects[message.payload.content_id]

    def _body_mask_of(self, object_id):
        """The transaction bodies a full object transfer delivers.

        For a leaf that is its own single bit. An aggregate carries a proof over
        many transactions; in the pre-wrapper model it also carries them all.
        """
        obj = self.arena.objects[object_id]
        return self.arena.coverage(obj)

    def _dep_count(self, obj):
        """Distinct dependencies an object claims, for sizing.

        A leaf is one. An aggregate is what it *asserts*, which is what a
        wrapper carries triples for -- not `obj.total`, the additive leaf count
        that double-counts overlap by design.
        """
        if obj.is_leaf():
            return 1
        return self.arena.claim(obj).bit_count()

    def _verify_duration(self, signatures, starks, fallback_count):
        """Receive verification for one delivery carrying this evidence.

        With no `verify_cost` configured, the flat table is charged
        `fallback_count` exactly as before -- and keeps the table's jitter,
        which the composition path does not have.
        """
        if self.verify_cost is None:
            return self.costs.duration(VERIFY_RECV, fallback_count)
        return self.verify_cost.message(signatures, starks)

    def _send(self, sender_id, receiver_id, payload):
        """Queue a payload on the sender's egress server.

        Transmission time is size/capacity, charged on one server per node, so
        sending to `d` peers takes `d` times as long as sending to one -- the
        fan-out cost made explicit. The size comes from the payload; nothing
        here inspects what it contains.
        """
        size = payload.size.total()
        message = WireMessage(sender_id, receiver_id, payload)
        if self.capacity is None:
            duration = 0.0
        else:
            duration = size / self.capacity
        kind = payload.kind
        accepted = self.egress[sender_id].submit(
            Job(duration, on_done=self._egress_done, tag=message))
        if not accepted:
            # The send buffer was full. Nothing crossed and nothing was offered,
            # so this is counted as a loss rather than as traffic -- charging it
            # as offered bytes would inflate the very figure that is meant to
            # bound what the links can carry.
            self.dropped_messages += 1
            self.dropped_bytes += size
            self.dropped_by_kind[kind] = self.dropped_by_kind.get(kind, 0) + 1
            self.dropped_by_node[sender_id] += 1
            self.dropped_bytes_by_node[sender_id] += size
            self.tracer.emit("drp", sender_id, t=round(self.engine.now, 3),
                             to=receiver_id, k=kind, b=size, why="egress_full")
            return False
        self.tracer.emit("snd", sender_id, t=round(self.engine.now, 3),
                         to=receiver_id, k=kind, b=size)
        if kind == "object" and payload.solicited:
            self.bodies_requested += 1
        elif kind == "object":
            self.bodies_pushed += 1
        self.messages_offered += 1
        self.offered_bytes[sender_id] += size
        self.offered_bytes_by_kind[kind] = (
            self.offered_bytes_by_kind.get(kind, 0) + size)
        table = self.offered_kind[sender_id]
        table[kind] = table.get(kind, 0) + size
        self.offered_messages_by_kind[kind] = (
            self.offered_messages_by_kind.get(kind, 0) + 1)
        return True

    def _cpu_overflow(self, message, what):
        """A verification job the CPU queue refused.

        The bytes crossed the wire and were counted as ingress -- they really did
        arrive -- but the work to make sense of them never ran, so nothing is
        admitted. That asymmetry is the point of modelling a bounded CPU queue:
        under overload a node pays for reception and gets nothing for it.
        """
        self.dropped_messages += 1
        self.dropped_by_kind[what] = self.dropped_by_kind.get(what, 0) + 1
        self.dropped_by_node[message.receiver] += 1
        # Global and per-node accounting must move together or dropped-byte
        # conservation fails for CPU-refused messages.
        self.dropped_bytes += message.size_bytes()
        self.dropped_bytes_by_node[message.receiver] += message.size_bytes()
        self.tracer.emit("drp", message.receiver,
                         t=round(self.engine.now, 3), k=message.payload.kind,
                         b=message.size_bytes(), why=what)

    def _egress_done(self, job):
        """The last byte has left the sender. Now it travels."""
        message = job.tag
        self.sent_bytes[message.sender] += message.size_bytes()
        table = self.sent_kind[message.sender]
        kind = message.payload.kind
        table[kind] = table.get(kind, 0) + message.size_bytes()
        if self.link_capacity is None:
            latency = self.overlay.latency(message.sender, message.receiver)
            self.engine.schedule(latency, EV_DELIVER, message)
            return
        # Optional per-link cap: a second serialising stage in tandem with
        # egress, with its own queue and its own completion.
        self._link_server(message.sender, message.receiver).submit(
            Job(message.size_bytes() / self.link_capacity,
                on_done=self._link_done, tag=message))

    def _link_server(self, sender_id, receiver_id):
        key = (sender_id, receiver_id)
        server = self.link_servers.get(key)
        if server is None:
            server = self.engine.new_server(f"link:{sender_id}->{receiver_id}")
            self.link_servers[key] = server
        return server

    def _link_done(self, job):
        message = job.tag
        latency = self.overlay.latency(message.sender, message.receiver)
        self.engine.schedule(latency, EV_DELIVER, message)

    # -- delivery ----------------------------------------------------------

    def _on_deliver(self, engine, message):
        self.knowledge.mark(message.receiver, self._delivered_leaves(message),
                            peer=message.sender,
                            obj=self._delivered_object(message),
                            bodies=self._delivered_bodies(message))
        self.messages_delivered += 1
        self.ingress_messages[message.receiver] += 1
        self.ingress_bytes[message.receiver] += message.size_bytes()
        table = self.ingress_kind[message.receiver]
        kind = message.kind()
        table[kind] = table.get(kind, 0) + message.size_bytes()
        # A sender learns a peer holds a body when it *arrives*, not when it was
        # queued: a belief formed at enqueue would let a later hash-only wrapper
        # overtake the body it refers to.
        payload = message.payload
        if kind == "block":
            pass
        elif kind == "object":
            leaf_mask = self._body_mask_of(payload.content_id)
            if leaf_mask:
                self.nodes[message.sender].confirm_bodies_delivered(
                    message.receiver, leaf_mask)
        elif kind == "wrapper":
            self.nodes[message.sender].confirm_bodies_delivered(
                message.receiver, payload.body_mask)
            # Only the receipt direction is safe here: the sender demonstrably
            # holds what it sent. The sender's belief about the receiver waits
            # for the retention decision in `_deliver_wrapper`, because the
            # receiver may still refuse the proof at its CPU queue. Body belief
            # is safe at delivery because arrived bodies are kept regardless of
            # what happens to the proof.
            #
            # Mode 0 sent witnesses instead, so it demonstrates possession of
            # those and of nothing else. Concluding the sender holds the proof
            # would let this node answer with that proof's hash, and the peer
            # cannot resolve bytes it chose not to send.
            if payload.core.mode == 1:
                self.nodes[message.receiver].note_proof_at_peer(
                    message.sender, payload.core.proof_object_id)
        if kind == "announce":
            self._deliver_announce(message)
        elif kind == "want":
            self._deliver_want(message)
        elif kind == "wrapper":
            self._deliver_wrapper(message)
        elif kind == "block":
            self._deliver_block(message)
        else:
            self._deliver_body(message)

    def _deliver_body(self, message):
        """A full object arrived, pushed or requested.

        Arrival and admission are separate because verification may queue
        between them. Until admission, the node cannot relay or serve it.
        """
        node = self.nodes[message.receiver]
        if not node.begin_receive(self._object_of(message)):
            self.duplicates += 1
            self.duplicate_bytes += message.size_bytes()
            self.ingress_duplicate_bytes[message.receiver] += message.size_bytes()
            self._trace_duplicate(message, self._object_of(message).id)
            return
        obj = self._object_of(message)
        # A body delivers one thing to verify: the leaf's own evidence (a
        # signature or a STARK witness by scheme), or an aggregate's
        # recursive proof -- its dependency triples are data, not evidence.
        if obj.kind == KIND_PQ:
            signatures, starks = 1, 0
        else:
            signatures, starks = 0, 1
        duration = self._verify_duration(signatures, starks,
                                         self._dep_count(obj))
        if duration == 0.0:
            # Nothing to queue: admit and relay in the same instant. The
            # computed duration is authoritative, so a callable returning zero
            # behaves like a literal zero instead of waiting behind the CPU.
            self._admit_and_relay(message)
            return
        if not self.cpu[message.receiver].submit(
                Job(duration, on_done=self._verify_done, tag=message,
                    kind=VERIFY_RECV)):
            self._cpu_overflow(message, "verify_recv")
            self.nodes[message.receiver].abandon_receive(
                self._object_of(message))

    def _verify_done(self, job):
        self.knowledge.mark(job.tag.receiver,
                            self._delivered_leaves(job.tag),
                            peer=job.tag.sender,
                            obj=self._delivered_object(job.tag),
                            bodies=self._delivered_bodies(job.tag))
        message = job.tag
        node = self.nodes[message.receiver]
        # Everything this object covered may have died while it sat in the
        # queue. Relaying it then spreads work the network already knows is
        # dead, so check before admitting rather than after.
        coverage = self.arena.coverage(self._object_of(message))
        if useful_count(coverage, node.known_discarded()) == 0:
            node.abandon_receive(self._object_of(message))
            self.discarded_after_work += 1
            return
        self._admit_and_relay(message)

    def _admit_and_relay(self, message):
        node = self.nodes[message.receiver]
        obj = self._object_of(message)
        # A full object transfer brings every body it covers.
        node.finish_receive(obj, self.engine.now, self.arena.coverage(obj))
        self._material[node.id] += 1
        self.tracer.emit("adm", node.id, t=round(self.engine.now, 3),
                         o=obj.id, born=round(obj.born, 3))
        self._wake_if_ready(node.id)
        self._trace_drops(node.id, node.enforce_candidate_limit(
                              self.arena, self._protected_candidate(node.id)),
                          "evicted")
        # Under wrapper_only nothing ever puts a raw object on the wire, so a
        # delivery to relay cannot happen; the guard is here so a future policy
        # that mixes modes does not quietly start flooding.
        if self.relay_policy == RELAY_WRAPPER_ONLY:
            return
        targets = node.relay_targets(message.sender)
        self._advertise(message.receiver, targets, self._object_of(message))

    def _deliver_announce(self, message):
        node = self.nodes[message.receiver]
        if not node.wants(self._object_of(message)):
            # Either already held or already asked for elsewhere. Counted: a
            # redundant announcement is small but not free.
            node.note_duplicate_announcement()
            self.duplicates += 1
            self.duplicate_bytes += message.size_bytes()
            self.ingress_duplicate_bytes[message.receiver] += message.size_bytes()
            self._trace_duplicate(message, message.payload.content_id)
            # Still redundant *now*, but remembered as an alternative source in
            # case the outstanding request never comes back.
            key = (message.receiver, message.payload.content_id)
            if key in self._request_sources:
                self._request_sources[key].append(message.sender)
            return
        node.mark_requested(self._object_of(message))
        self._request(message.receiver, message.payload.content_id,
                      message.sender)

    def _request(self, node_id, object_id, peer_id):
        """Ask one peer for a body, and arm a timeout if one is configured.

        Without the timeout a lost request is permanent: `wants()` refuses while
        a request is outstanding, so every later announcement of that object is
        discarded as redundant and the node waits for an answer that is never
        coming. Lossless queues cannot expose this state, but bounded queues can.
        """
        key = (node_id, object_id)
        if key not in self._request_sources:
            self._request_sources[key] = []
        payload = self.size_model.want(object_id)
        self._send(node_id, peer_id, payload)
        if self.request_timeout is not None:
            self.engine.schedule(self.request_timeout, EV_REQUEST_TIMEOUT, key)

    def _on_request_timeout(self, engine, key):
        node_id, object_id = key
        node = self.nodes[node_id]
        if object_id not in node.view.requested:
            # It arrived, or was abandoned for another reason. Nothing owed.
            self._request_sources.pop(key, None)
            return
        obj = self.arena.objects.get(object_id)
        if obj is None:
            self._request_sources.pop(key, None)
            return
        self.requests_timed_out += 1
        # Knowledge changes here: without another source the class falls from
        # `requested` to `none`, and a tracer that missed it would then miss
        # the fresh announcement that brings it back, too.
        self.knowledge.mark(node_id, self.arena.coverage(obj))
        node.view.requested.discard(object_id)
        others = self._request_sources.get(key) or []
        while others:
            peer_id = others.pop(0)
            if peer_id in node.peers:
                node.mark_requested(obj)
                self.requests_retried += 1
                self._request(node_id, object_id, peer_id)
                return
        # Nobody else to ask. Clearing `requested` is still the right outcome:
        # the node can now accept a fresh announcement instead of ignoring every
        # one of them for the rest of the run.
        self._request_sources.pop(key, None)
        self.unserved_wants += 1

    def _deliver_want(self, message):
        """Answer a request -- but only if this node actually has the body.

        The request names an object, and it is tempting to answer straight from
        that reference. Doing so would let a node serve a body it never had, or
        one it has since dropped, which would make retention and eviction
        invisible: whatever policy discards bodies, requests would still be
        satisfied. So the responder consults its own view, and an unanswerable
        request is counted rather than silently fulfilled.
        """
        responder = self.nodes[message.receiver]
        if not responder.view.holds(self._object_of(message)):
            self.unserved_wants += 1
            return
        obj = self._object_of(message)
        payload = self.size_model.object_payload(obj, self._dep_count(obj),
                                                 solicited=True)
        self._send(message.receiver, message.sender, payload)

    # -- the aggregation tick ----------------------------------------------

    def schedule_ticks(self, count, first_tick=None):
        """Schedule `count` aggregation ticks for every node.

        Phases are spread by default, derived per node so they are reproducible
        and independent of everything else. Aligned ticks would put every uplink
        and every CPU to work at the same instant and idle between, which is a
        thundering herd rather than a network.
        """
        self._require_unfinished("schedule_ticks")
        if self.aggregation_trigger not in CLOCKED_TRIGGERS:
            raise ValueError(
                "aggregation_trigger='when_ready' has no clock to schedule: "
                "nodes fold when they are idle and hold enough to fold. "
                "Remove the schedule_ticks call, or use a clocked "
                "trigger: 'aligned' or 'spread'.")
        start = self.engine.now
        # Numbering continues from whatever has already been scheduled, because
        # generation-based cancellation compares tick numbers: restarting at one
        # would make a later plan look older than an earlier one.
        if first_tick is None:
            first_tick = self._next_tick
        self._next_tick = first_tick + count
        for node_id in range(len(self.nodes)):
            if self.aggregation_trigger == TRIGGER_SPREAD:
                offset = self.aggregation_interval * stable_fraction(
                    self.seed, "tick", node_id)
            else:
                offset = 0.0
            for index in range(count):
                when = start + offset + (index + 1) * self.aggregation_interval
                self.engine.schedule_at(when, EV_TICK,
                                        (node_id, first_tick + index))

    def _on_expire(self, engine, _payload):
        self._expire_due(self.engine.now)

    def _on_tick(self, engine, payload):
        node_id, tick = payload
        self._tick(node_id, tick)

    def _tick(self, node_id, tick):
        """One node's aggregation round: prune, select, plan, prove.

        The EIP has a node emit one wrapper per interval covering everything it
        still considers valid. Selection and planning are pure and happen here;
        the proving is queued, so a tick's output appears later and may appear
        after the next tick has already begun.
        """
        # No knowledge mark is needed here. The only synchronous view mutation
        # is `prune_discarded`, which drops objects whose entire claim is
        # settled; `settled` already outranks held evidence, so pruning cannot
        # change a knowledge class.
        if node_id == self._dedicated_builder:
            # A builder that only builds. Not counted as a tick: nothing was
            # decided, and a tick that decides nothing is not a round.
            return None
        node = self.nodes[node_id]
        self.ticks_run += 1
        self._trace_drops(node.id, node.prune_discarded(self.arena), "pruned")
        if self.on_tick_overrun == "abandon":
            # Unstarted proving from an earlier tick is decided against a view
            # that no longer exists, so drop it and re-decide rather than spend
            # the budget on a stale selection.
            dropped = self.cpu[node_id].cancel_waiting(
                lambda job: (job.kind == PROVE_RECURSIVE
                             and job.generation < tick))
            # A cancelled fifty-job tree is one abandoned plan, not fifty.
            self.proof_jobs_cancelled += len(dropped)
            self.plans_abandoned += len({job.tag[0] for job in dropped})
        selection = get_strategy("selection", self.selection_strategy)(
            node, self.arena, tick, self._selection_costs, self.proving_budget,
            self._previous_aggregate(node))
        if selection.is_empty():
            return None
        # A strategy may only choose from what this node holds. The shipped
        # ones find their candidates in `view.held` and are safe by
        # construction, but the registry is a documented extension point and
        # the property is the model's, not any one policy's: folding an object
        # a node does not hold is proving over evidence it never received.
        unheld = [object_id for object_id in selection.inputs
                  if object_id not in node.view.held]
        if unheld:
            raise ValueError(
                f"selection_strategy={self.selection_strategy!r} chose "
                f"{unheld}, which node {node_id} does not hold. A strategy "
                "may only select from node.view.held.")
        # A fold takes two inputs or more. Fewer is one of three things, none
        # of which is a fold: a re-proof of an existing proof, which invents
        # work and a fold depth for coverage that is already proven; a plan over
        # objects that have left the arena, which charges a CPU for folding
        # nothing and produces no aggregate; or a lone leaf, which is the one
        # real case, because a bare dependency has no recursive proof yet and
        # proving it once is what creates one.
        live = []
        for object_id in selection.inputs:
            obj = self.arena.objects.get(object_id)
            if obj is not None:
                live.append(obj)
        if not live:
            self.plans_abandoned += 1
            return None
        if self.fold_threshold == FOLD_AT_CROSSOVER:
            # A proof that asserts less evidence than it costs is worse than the
            # evidence. Measured against the claim's own witnesses, which is the
            # thing the proof would replace.
            # `fold_amortize` scales the witnesses' side, standing in for
            # the fold's unpriced benefits, which accrue beyond this node's
            # own uplink: a block slot carries the whole claim instead of
            # one object, downstream receivers verify one proof instead of
            # every witness, and nobody proves this coverage twice. One is
            # the strictly local reading.
            witnesses = self._mode_zero_bytes(selection.claimed)
            if (witnesses * self.fold_amortize
                    < self.size_model.recursive_proof):
                self.folds_deferred += 1
                return None
        if len(live) == 1 and not live[0].is_leaf():
            self._emit_wrapper(node, live[0], tick)
            return None
        plan = get_strategy("fold_plan", self.fold_plan)(selection)
        if self.prove_cost_basis == PROVE_COST_CIRCUIT:
            # Whether one proof can hold a job's verification is decided by the
            # trace length, a protocol constant, so an over-capacity job is
            # impossible rather than slow -- and knowable before any CPU is
            # spent. The whole plan is checked here, before the first
            # submission, because a plan is refused as a unit: checking inside
            # the loop let earlier jobs start (an idle server begins service
            # synchronously, and cancellation cannot recall work in progress),
            # so a plan declared impossible still consumed proving time and
            # completed internal proofs.
            circuit = self.costs.costs[PROVE_RECURSIVE]
            for job in plan.jobs:
                if not circuit.fits(*self._job_composition(node, job)):
                    self.proof_jobs_over_capacity += 1
                    self.plans_abandoned += 1
                    return None
        run = _PlanRun(plan, node_id, tick)
        for job in plan.jobs:
            # Priced by what this job proves, not by what the plan as a whole
            # will assert. Charging every fold for the whole selection would
            # erase the cost difference between plan shapes.
            #
            # Which number "this job" is measured by is the basis: under
            # `deps` the output's dependency count, under `inputs` how many
            # objects the job folds -- its arena inputs plus earlier jobs'
            # outputs it consumes. The count is taken from the plan, not from
            # `_resolve` at start time, because the cost of a fold is decided
            # by what the plan asks it to verify, not by how many of those
            # inputs still exist when the CPU gets around to it.
            if self.prove_cost_basis == PROVE_COST_CIRCUIT:
                # Capacity was settled for the whole plan above; what remains is
                # the price.
                duration = circuit.duration(*self._job_composition(node, job))
                if self.costs.jitter_frac:
                    duration += self.costs._rng.uniform(
                        0.0, self.costs.jitter_frac * duration)
            else:
                if self.prove_cost_basis == PROVE_COST_INPUTS:
                    cost_size = len(job.inputs) + len(job.depends_on)
                else:
                    # What this job will assert as best known *now*, not
                    # when the plan was built: knowledge moves between
                    # planning and submission, and the claim is frozen from
                    # current knowledge when the job starts. Reading the
                    # plan's count charged for dependencies that had settled
                    # in between.
                    cost_size = (job.output_mask
                                 & ~node.known_discarded()).bit_count()
                duration = self.costs.duration(PROVE_RECURSIVE,
                                               max(1, cost_size))
            if not self.cpu[node_id].submit(
                    Job(duration, on_done=self._proof_done,
                        on_start=self._proof_started, tag=(run, job),
                        generation=tick, kind=PROVE_RECURSIVE)):
                # The queue refused a fold, so the plan can never reach its
                # root and submitting the rest would be work with no product.
                # Account it like an overrun abandonment: withdraw waiting jobs
                # from this run, but let an in-service proof finish because work
                # in progress is non-preemptive. Its output remains internal.
                withdrawn = self.cpu[node_id].cancel_waiting(
                    lambda queued: (queued.kind == PROVE_RECURSIVE
                                    and queued.tag[0] is run))
                self.proof_jobs_cancelled += len(withdrawn)
                self.plans_abandoned += 1
                return None
        return run

    def _held_witness_mask(self, node):
        """Transactions whose witness this node can actually put on the wire.

        Holding a body is not holding a witness. A wrapper carries bodies and
        hashes; the witness travels only inside a raw object, so a node has one
        exactly when it holds that leaf as an object -- which it originated, or
        received under a relay policy that ships objects. Under `wrapper_only`
        nothing ships objects, so a node can concatenate only its own.
        """
        mask = 0
        for object_id in node.view.held:
            obj = self.arena.objects.get(object_id)
            if obj is not None and obj.leaf_index is not None:
                mask |= 1 << obj.leaf_index
        return mask

    def _mode_zero_bytes(self, claimed):
        """What mode 0 would cost for this claim: its dependencies' witnesses.

        A size, not a possession claim. Only the dependencies' schemes are read,
        which a node holding the claim holds in its triples, so this answers
        "what would the raw evidence for this claim weigh" for any node. Whether
        this node could actually *send* that encoding is the separate question
        `_prefers_mode_zero` asks, and only that one needs the witnesses in hand.
        """
        return self.size_model.witness_bytes(self.arena.schemes_in(claimed))

    def _prefers_mode_zero(self, node, claimed):
        """Whether raw witnesses beat a recursive proof for this claim.

        Two conditions, and the first is not about size. A node can only
        concatenate witnesses it holds as objects, so a claim inherited by
        folding a peer's aggregate cannot be emitted this way: the aggregate
        brought a proof and some bodies, never the witnesses behind them.
        Asking the arena what the claim covers would answer from global truth
        and let a node ship evidence it never received.

        The size comparison is then per emission rather than decided once,
        because the answer moves with what is claimed: signatures cross over
        near a hundred dependencies, privacy transactions at two. Only the proof
        term differs between the modes, so it is witnesses against one recursive
        proof.
        """
        if self.wrapper_mode != WRAPPER_MODE_CHEAPEST:
            return False
        if claimed & ~self._held_witness_mask(node):
            return False
        return self._mode_zero_bytes(claimed) < self.size_model.recursive_proof

    def _job_composition(self, node, job):
        """What this job makes the circuit verify, by kind.

        Computable before the job runs, which is what the CPU needs: arena ids
        carry their kind, and every `depends_on` is an earlier job's output and
        therefore a nested recursive proof. An input that has left the arena
        contributes nothing, the same way `_resolve` drops it later.

        The arena inputs are priced by the same function selection estimates
        with, because an estimate in a unit execution does not charge is a
        budget ceiling wearing a cost's clothing.
        """
        signatures, leanstarks, recursive = input_composition(
            self.arena, node.view, job.inputs)
        return signatures, leanstarks, recursive + len(job.depends_on)

    def _resolve(self, run, job):
        """The objects a plan job folds: arena objects, and earlier jobs' output."""
        inputs = []
        for object_id in job.inputs:
            obj = self.arena.objects.get(object_id)
            if obj is not None:
                inputs.append(obj)
        for produced_index in job.depends_on:
            produced = run.produced.get(produced_index)
            if produced is not None:
                inputs.append(produced)
        return inputs

    def _count_stale_work(self, claimed, node_id=None):
        """Proving effort about to be spent on already-settled dependencies.

        Measured against the *chain* rather than the prover's belief, because
        the gap between the two is the point -- but against what the proof will
        actually claim, so dependencies the prover already knew to drop are not
        counted as waste it chose to incur.
        """
        settled = self.chain.discarded_mask()
        if not settled:
            return 0
        stale = (claimed & settled).bit_count()
        self.stale_deps_proved += stale
        if node_id is not None:
            self.stale_by_node[node_id] += stale
        return stale

    def _freeze_claim(self, node, plan_job):
        """What this proof will assert, decided now and never revisited.

        The circuit takes its discards as an input, so the claim belongs to the
        moment the prover began -- not to what anyone knows when it finishes.
        Recomputing it at completion could let a receiver treat a dependency
        the prover explicitly dropped as proven.
        """
        return plan_job.output_mask & ~node.known_discarded()

    def _proof_started(self, job):
        """Book the work when it begins, not when its output appears.

        A proof cancelled or still running at the end of a run has spent its
        inputs regardless, so booking at completion would undercount exactly the
        work that produced nothing.
        """
        run, plan_job = job.tag
        inputs = self._resolve(run, plan_job)
        run.resolved[plan_job.index] = inputs
        run.claims[plan_job.index] = self._freeze_claim(
            self.nodes[run.node_id], plan_job)
        self.arena.note_consumption(inputs)
        self._count_stale_work(run.claims[plan_job.index], run.node_id)

    def _proof_done(self, job):
        # The job's tag is (run, plan_job); the node is on the run.
        run = job.tag[0]
        self.knowledge.mark(run.node_id)
        run, plan_job = job.tag
        inputs = run.resolved.get(plan_job.index) or self._resolve(run, plan_job)
        if not inputs:
            return
        node = self.nodes[run.node_id]
        aggregate = self.arena.new_aggregate(inputs, self.engine.now,
                                            run.node_id,
                                            claimed=run.claims.get(
                                                plan_job.index, 0))
        run.produced[plan_job.index] = aggregate
        self.proofs_completed += 1
        if plan_job.index != run.plan.root:
            return
        # The root is what the network sees. Everything below it was internal.
        node.finish_receive(aggregate, self.engine.now)
        self._material[node.id] += 1
        self._trace_drops(node.id, node.enforce_candidate_limit(
                              self.arena, self._protected_candidate(node.id)),
                          "evicted")
        # The whole selection is retired, not just the root job's inputs: with a
        # wide plan those are the same, but with a tree the original selection
        # sits below the root and would otherwise stay a candidate for ever.
        self._trace_drops(node.id, node.supersede(run.plan.selection.inputs),
                          "superseded")
        self.own_aggregate[run.node_id] = aggregate
        self._emit_wrapper(node, aggregate, run.tick)

    def _previous_aggregate(self, node):
        """This node's last aggregate, if it still holds it.

        The pointer records what a node produced and is not cleared when the
        object leaves its view -- eviction can take it. Offering it to selection
        anyway would let a node fold evidence it dropped, which is the
        possession boundary the whole model rests on, so the check is here at
        the one place selection is handed anything it did not find itself.
        """
        previous = self.own_aggregate[node.id]
        if previous is None or previous.id not in node.view.held:
            return None
        return previous

    def _protected_candidate(self, node_id):
        """The object eviction may not drop, or None."""
        if not self.pin_own_aggregate:
            return None
        previous = self.own_aggregate[node_id]
        return None if previous is None else previous.id

    def _wake_if_ready(self, node_id):
        """Fold now if this node can, under `when_ready`.

        v1's rule: an aggregator with a free CPU and something to fold does not
        wait for a clock. Called wherever either precondition can newly become
        true -- any CPU running dry, through `Server.on_idle`; a birth; and a
        change in what a node knows is settled, from expiry or a block notice.
        Cheap at all of them, since it is a few comparisons before it is
        anything else: a trigger has to stay cheaper than what it watches.
        """
        if self.aggregation_trigger != TRIGGER_WHEN_READY:
            return
        if node_id == self._dedicated_builder:
            return
        if self.cpu[node_id].is_busy() or self.cpu[node_id].waiting:
            return
        if len(self.nodes[node_id].view.held) < FOLDABLE_TO_WAKE:
            return
        # What this node holds *and* what it knows is settled: a discard makes
        # a previously refused plan feasible without a single object arriving,
        # so counting material alone left a node stranded after an expiry.
        stamp = (self._material[node_id],
                 self.nodes[node_id].notifier.generation(node_id))
        if stamp == self._woke_at[node_id]:
            # Nothing has changed since the last decision, so it would be the
            # same decision.
            return
        self._woke_at[node_id] = stamp
        self._next_generation[node_id] += 1
        self._tick(node_id, self._next_generation[node_id])

    def _emit_wrapper(self, node, aggregate, tick):
        """One wrapper core, and a differently sized copy for each peer.

        The aggregate carries the claim frozen when its proving job started.
        Emission must reuse that mask: recomputing against later knowledge could
        restore a dependency the prover explicitly discarded and let receivers
        treat it as proven.
        """
        claimed = self.arena.claim(aggregate)
        if not claimed:
            self.stale_proofs += 1
            return None
        witness_bytes = None
        mode = 1
        if self._prefers_mode_zero(node, claimed):
            mode = 0
            witness_bytes = self._mode_zero_bytes(claimed)
            self.mode_zero_wrappers += 1
        core = WrapperCore(wrapper_id=self._next_wrapper_id,
                           proof_object_id=aggregate.id,
                           creator=node.id, tick=tick, mode=mode,
                           tx_mask=claimed, dep_count=claimed.bit_count())
        self._next_wrapper_id += 1
        self.wrappers_emitted += 1
        self.tracer.emit("wrp", node.id, t=round(self.engine.now, 3),
                         w=core.wrapper_id, o=aggregate.id,
                         deps=core.dep_count, tick=tick)
        for peer in self._rotated(node.id, node.peers):
            # Bodies this peer is believed to lack, and that this node can
            # actually supply. Everything else travels as a hash.
            body_mask = node.bodies_to_send(peer, claimed) & node.view.bodies
            peer_has_proof = node.peer_has_proof(peer, aggregate.id)
            if (peer_has_proof and not body_mask
                    and self.wrapper_economy in (ECONOMY_SKIP_UNCHANGED,
                                                 ECONOMY_LEAN)):
                # The peer verifiably holds this exact proof and there is
                # nothing new to bring it: the wrapper would arrive, be
                # counted a duplicate, and change nothing. The belief formed
                # at delivery, so if the last copy was dropped there is no
                # belief and this branch cannot starve the peer.
                self.wrapper_sends_suppressed += 1
                continue
            proof_by_ref = (peer_has_proof
                            and self.wrapper_economy in (ECONOMY_PROOF_BY_REF,
                                                         ECONOMY_LEAN))
            self._send(node.id, peer,
                       self.size_model.wrapper(core, body_mask,
                                               proof_by_ref=proof_by_ref,
                                               witness_bytes=witness_bytes))
        return core

    def _deliver_wrapper(self, message):
        """A peer's wrapper: new evidence, new bodies, or nothing.

        Verification is charged like any other receipt. The aggregate is not
        admitted until that work completes, so verification remains on the
        store-and-forward propagation path.

        Wrappers are not relayed. Each node emits its own every interval, so one
        travels a single hop and spreading happens by re-aggregation, which is
        what makes the EIP's per-tick bandwidth constant per peer.
        """
        payload = message.payload
        core = payload.core
        node = self.nodes[message.receiver]
        if payload.proof_by_ref and not node.can_resolve_proof(
                core.proof_object_id):
            # The sender believed this node holds the proof, but it does not
            # -- abandoned under overflow after the belief formed. A reference
            # to bytes never held is not evidence; the bodies are real and are
            # kept, because the sender's body belief formed at delivery and
            # they will not be sent here in full again.
            node.receive_bodies(payload.body_mask)
            self.wrapper_refs_unresolved += 1
            return
        # What *this copy* carried, which is not the core's mode alone: a copy
        # substituting the proof by reference names bytes the recipient already
        # holds, so it leaves the evidence class it found.
        witness_backed = core.mode == 0 and not payload.proof_by_ref
        worth_verifying, fresh_bodies, upgraded = node.begin_wrapper(
            core.proof_object_id, payload.body_mask,
            witness_backed=witness_backed)
        if not worth_verifying:
            # An upgrade -- a recursive proof landing on a claim whose
            # witnesses are still being checked -- verifies nothing and may
            # bring no fresh body, yet it taught: the claim it leaves behind
            # resolves by reference where the one it found did not.
            if not fresh_bodies and not upgraded:
                self.duplicates += 1
                self.duplicate_bytes += message.size_bytes()
                self.ingress_duplicate_bytes[message.receiver] += (
                    message.size_bytes())
                self._trace_duplicate(message, core.proof_object_id,
                                      claim=core.tx_mask)
            # Verified already, or verifying: either way this node retains the
            # proof, so the sender's belief about it may finally form. Not for
            # mode 0's witnesses, which brought no proof to retain.
            if not witness_backed:
                self.nodes[message.sender].note_proof_at_peer(
                    message.receiver, core.proof_object_id)
            # A duplicate from a different peer still belongs to that peer's
            # wrapper stream. Otherwise provenance would remain attached only
            # to the first sender and a later wrapper from this peer could not
            # supersede it. Only held objects can be superseded; an in-flight
            # object joins the stream of whichever copy finishes.
            aggregate = self.arena.objects.get(core.proof_object_id)
            if aggregate is not None and aggregate.id in node.view.held:
                self._trace_drops(node.id, node.supersede_from_peer(
                    message.sender, aggregate, self.arena,
                    self._supersede_strategy), "superseded")
            return
        if core.mode == 0:
            # Mode 0 delivers the dependencies' own witnesses, and the
            # receiver verifies each one: the mode saves the proof's bytes,
            # never the verification of what stands behind them.
            schemes = self.arena.schemes_in(core.tx_mask)
            signatures = sum(1 for kind in schemes if kind == KIND_PQ)
            starks = len(schemes) - signatures
        else:
            signatures, starks = 0, 1
        duration = self._verify_duration(signatures, starks, core.dep_count)
        if duration == 0.0:
            if not witness_backed:
                self.nodes[message.sender].note_proof_at_peer(
                    message.receiver, core.proof_object_id)
            self._admit_wrapper(message)
            return
        if not self.cpu[message.receiver].submit(
                Job(duration, on_done=self._wrapper_verified, tag=message,
                    kind=VERIFY_RECV)):
            self._cpu_overflow(message, "verify_wrapper")
            # The proof is abandoned, but its bodies are kept because the
            # sender's body belief formed at delivery. No proof belief forms,
            # so the next tick may resend full evidence.
            self.nodes[message.receiver].abandon_wrapper(
                message.payload.core.proof_object_id,
                witness_backed=witness_backed)
            return
        # Accepted into the queue: this node will verify and retain the proof
        # (or abandon it only once its whole claim has settled, when the
        # optimistic belief costs nothing live). Only now may the sender
        # believe the peer has it -- and not from mode 0's witnesses, which
        # leave the receiver with no proof bytes to be referenced.
        if not witness_backed:
            self.nodes[message.sender].note_proof_at_peer(
                message.receiver, core.proof_object_id)

    def _wrapper_verified(self, job):
        self.knowledge.mark(job.tag.receiver,
                            self._delivered_leaves(job.tag),
                            peer=job.tag.sender,
                            obj=self._delivered_object(job.tag),
                            bodies=self._delivered_bodies(job.tag))
        self._admit_wrapper(job.tag)

    def _admit_wrapper(self, message):
        payload = message.payload
        core = payload.core
        node = self.nodes[message.receiver]
        # The class this job was checking, named again at completion: during
        # an upgrade the same proof id has a witness job and a proof job in
        # flight, and which evidence gets installed is the finished job's.
        witness_backed = core.mode == 0 and not payload.proof_by_ref
        # Everything it claimed may have settled while it sat in the queue.
        if useful_count(core.tx_mask, node.known_discarded()) == 0:
            node.abandon_wrapper(core.proof_object_id,
                                 witness_backed=witness_backed)
            self.discarded_after_work += 1
            return
        bodies = node.finish_wrapper(core.proof_object_id,
                                     witness_backed=witness_backed)
        aggregate = self.arena.objects.get(core.proof_object_id)
        if aggregate is None:
            return
        node.finish_receive(aggregate, self.engine.now, bodies)
        self._material[node.id] += 1
        # Knowledge arriving through aggregation is still knowledge arriving, so
        # this is recorded like any other admission -- tagged `via`, because the
        # object id is the aggregate's and not the transactions it covers.
        #
        # Admission must precede eviction because a residency reducer replays
        # line order. Otherwise it sees a `drop` for an unknown object and
        # cannot reconstruct the simulator's final residency.
        self.tracer.emit("adm", node.id, t=round(self.engine.now, 3),
                         o=aggregate.id, born=round(aggregate.born, 3),
                         via="wrapper")
        # Supersession before eviction: dropping what the policy says this
        # wrapper replaces may make room, and evicting something first only to
        # find the supersession would have freed the space is a self-inflicted
        # loss.
        self._trace_drops(node.id, node.supersede_from_peer(
            message.sender, aggregate, self.arena, self._supersede_strategy),
            "superseded")
        self._trace_drops(node.id, node.enforce_candidate_limit(
                              self.arena, self._protected_candidate(node.id)),
                          "evicted")
        self._wake_if_ready(node.id)

    # -- block building ----------------------------------------------------

    def schedule_slots(self, count, first_slot=None):
        """Schedule `count` boundaries, the first one slot after the clock now.

        Relative rather than absolute: multiples of the slot length may already
        be in the past when scheduling continues after a partial run.
        """
        self._require_unfinished("schedule_slots")
        start = self.engine.now
        if first_slot is None:
            first_slot = self._next_slot
        self._next_slot = first_slot + count
        for index in range(count):
            when = start + (index + 1) * self.slot_length
            self.engine.schedule_at(when, EV_SLOT, first_slot + index)

    def _on_slot(self, engine, slot):
        self._build_block(slot)

    def _expire_due(self, now):
        """Retire transactions that have stopped being includable.

        Expiry is the model's second reason for a transaction to leave, and the
        one that makes the EIP's removals tier mean anything: without it the only
        exit is inclusion, so there is never anything to prune.
        """
        due = 0
        while self._expiry_queue and self._expiry_queue[0][0] <= now:
            _when, leaf_index = heapq.heappop(self._expiry_queue)
            leaf = self.arena.leaves[leaf_index]
            mask = self.arena.coverage(leaf)
            newly_expired = self.chain.expire(mask)
            self.knowledge.mark_all(newly_expired)
            if newly_expired:
                due += 1
                # Every node's view of what is settled has moved, and a node
                # that was idle because its only plan was infeasible may now
                # have a feasible one.
                for node_id in range(len(self.nodes)):
                    self._wake_if_ready(node_id)
        return due

    def _build_block(self, slot):
        """The slot's proposer assembles a block from what it holds.

        The proposer uses its own partial, possibly stale view; it has no access
        to arena-wide holdings or canonical facts it has not been notified of.
        """
        now = self.engine.now
        # Expire first, so a proposer is not offered transactions that stopped
        # being includable at this very boundary.
        self._expire_due(now)
        proposer_id = self.proposers.proposer_for(slot)
        proposer = self.nodes[proposer_id]
        known_discarded = proposer.known_discarded()
        # Under body_required a claim is includable only if the proposer holds
        # the transaction's body: the block's execution payload carries every
        # included transaction, and a proof alone cannot conjure one. The
        # mask is enforced inside selection, which scores and accumulates only
        # body-masked claims. Structural coverage that the proof does not assert
        # can never be attempted.
        if self.inclusion_policy == INCLUSION_BODY_REQUIRED:
            body_mask = proposer.view.bodies
        else:
            body_mask = None
        picked, claimed = proposer.select_by_coverage(
            self.arena, self.block_budget + 1, known_discarded,
            body_mask=body_mask)
        delays = []
        attempted = claimed & ~known_discarded
        for leaf in self.arena.leaves_in(
                attempted & ~self.chain.discarded_mask()):
            delay = (now - leaf.born) / self.slot_length
            delays.append(delay)
            # Recorded with where it entered and who built the block, so a delay
            # can be attributed to a position in the graph.
            self.tx_records.append(
                (self.entry_of.get(leaf.leaf_index), proposer_id, delay))
        # Settlement changed for exactly the leaves this block included, and
        # under instant notification every node learns at once.
        block = self.chain.include(attempted, slot, proposer_id, now,
                                   [obj.id for obj in picked], delays)
        self.knowledge.mark_all(block.included)
        for delay in delays:
            self.delay_sum += delay
        self.delays.extend(delays)
        self.slots_run = slot
        # No tracer node id: `Tracer.emit` treats it as stream ownership and may
        # filter the event. Blocks belong to the global chain; the proposer is
        # event data.
        self.tracer.emit("blk", None, proposer=proposer_id,
                         t=round(now, 3), slot=slot,
                         txs=block.count(), objs=len(block.object_ids),
                         wasted=block.wasted().bit_count())
        # The block fact is created at the boundary, not when the proposer
        # finishes computing: assembly cost occupies its CPU, competing with
        # everything else it has to do, but does not move the chain.
        count = block.count()
        duration = self.costs.duration(BUILD_BLOCK, count)
        if duration > 0.0:
            if not self.cpu[proposer_id].submit(
                    Job(duration, tag=block, kind=BUILD_BLOCK)):
                # The block already exists at the boundary, so a refused
                # assembly job cannot unmake it. Count the refusal rather than
                # silently making assembly free; a nonzero count means block
                # costs in this run are understated.
                self.build_jobs_refused += 1
        self._announce_block(block, slot, proposer_id)
        return block

    # -- tracing -----------------------------------------------------------

    def _trace_drops(self, node_id, object_ids, why):
        """Record objects leaving a node's view.

        Batched per call rather than one record per object: pruning and eviction
        drop many objects at a single instant, and repeating the node, time and
        reason for each would be redundancy rather than information.
        """
        # Recorded whether or not tracing is on: the refusal boundary in the
        # measurement API depends on it, and that must not change with
        # instrumentation.
        self._departed.update(object_ids)
        # A departure changes the class of every leaf the object covered, and
        # those leaves have nothing to do with whatever event triggered it --
        # admitting one object can evict several unrelated ones. Every departed
        # object's leaves must therefore be marked independently.
        for object_id in object_ids:
            obj = self.arena.objects.get(object_id)
            if obj is not None:
                self.knowledge.mark(node_id, self.arena.coverage(obj))
        if not object_ids or not self.tracer.enabled():
            return
        self.tracer.emit("drop", node_id, t=round(self.engine.now, 3),
                         why=why, o=list(object_ids))

    def _trace_counters(self, now, label=None):
        """Cumulative per-node counters, sampled on the observation grid.

        Counters rather than rates: a rate is computed against one window and
        cannot be re-windowed afterwards, while differencing counters gives any
        window a reader asks for -- which is what "reproducible from the trace
        alone" requires.

        Called from an engine observer rather than block building so runs with
        no scheduled slots still emit counters. `label` marks boundaries that
        are not grid multiples: the start and end of a `run()` segment, without
        which its first and last intervals cannot be reconstructed.
        """
        if not self.tracer.enabled():
            return
        for node_id in range(len(self.nodes)):
            if not self.tracer.covers(node_id):
                continue
            fields = {}
            if label is not None:
                fields["seg"] = label
            self.tracer.emit(
                "cnt", node_id, t=round(now, 3), **fields,
                eb=round(self.egress[node_id].busy_time_at(now), 3),
                cb=round(self.cpu[node_id].busy_time_at(now), 3),
                ob=self.offered_bytes[node_id],
                sb=self.sent_bytes[node_id],
                ib=self.ingress_bytes[node_id],
                obk=self.offered_kind[node_id],
                sbk=self.sent_kind[node_id],
                ibk=self.ingress_kind[node_id],
                idb=self.ingress_duplicate_bytes[node_id],
                eq=self.egress[node_id].queue_depth(),
                # Queued *bytes* as well as messages, because the bound is in
                # bytes: depth in messages cannot say how full a send buffer is.
                eqb=self.egress[node_id].queued_weight,
                cq=self.cpu[node_id].queue_depth(),
                dr=self.dropped_by_node[node_id],
                drb=self.dropped_bytes_by_node[node_id],
                held=len(self.nodes[node_id].view.held),
                # Unavailable rather than zero where the model cannot
                # express it, matching what the metrics report.
                stale=(self.stale_by_node[node_id]
                       if self.notifier.diverges else -1))

    # -- block propagation -------------------------------------------------

    def _announce_block(self, block, slot, proposer_id):
        """Tell the network about a block, if learning takes time here.

        Under instant notification there is nothing to send: every node's
        knowledge advances with the chain. Under overlay notification the block
        is a message like any other, sharing the proposer's egress with its
        wrappers -- it is the same uplink, and hiding that would make block
        propagation free exactly where it competes.
        """
        if not self.notifier.diverges:
            return
        block_id = len(self.chain.blocks) - 1
        proposer = self.nodes[proposer_id]
        proposer.hears_about_block(block_id)
        self.notifier.notify(proposer_id, self.chain.discarded_mask())
        # The notice carries the whole settled set, not just this block's
        # inclusions. Expiry produces no block, so an inclusion-only notice
        # could never propagate expired dependencies.
        payload = self.size_model.block(block_id, slot, block.included,
                                        self.chain.discarded_mask())
        for peer in self._rotated(proposer_id, proposer.peers):
            self._send(proposer_id, peer, payload)

    def _deliver_block(self, message):
        """A block notice arrived. Checked before it is believed or relayed."""
        payload = message.payload
        node = self.nodes[message.receiver]
        if not node.hears_about_block(payload.block_id):
            self.duplicates += 1
            self.duplicate_bytes += message.size_bytes()
            self.ingress_duplicate_bytes[message.receiver] += message.size_bytes()
            return
        # A block notice carries the block itself -- the size model prices it
        # at one recursive proof plus hash references -- so under composition
        # pricing it pays one STARK-class verification before settlement is
        # believed, like any other proof-carrying delivery.
        duration = self._verify_duration(0, 1,
                                         max(1, payload.included.bit_count()))
        if duration == 0.0:
            self._admit_block(message)
            return
        if not self.cpu[message.receiver].submit(
                Job(duration, on_done=self._block_verified, tag=message,
                    kind=VERIFY_RECV)):
            # A dropped block notice is the worst of these: the node stays
            # ignorant of settlement and keeps proving work the chain has
            # already taken, which `stale_deps_proved` will show.
            self._cpu_overflow(message, "verify_block")

    def _block_verified(self, job):
        # A block changes settlement knowledge, and which leaves that covers is
        # the block's own mask rather than anything about the message.
        self.knowledge.mark(job.tag.receiver, peer=job.tag.sender, obj="block")
        self._admit_block(job.tag)

    def _admit_block(self, message):
        payload = message.payload
        node = self.nodes[message.receiver]
        self.block_notices += 1
        # Everything the notice settles, not only what this block included:
        # expiries carry no block of their own, so a notice that shipped only
        # `included` left them unable to reach anyone.
        self.notifier.notify(message.receiver, payload.settled)
        # Same reason as expiry: this node now knows more, which can make a
        # refused plan feasible without anything arriving.
        self._wake_if_ready(message.receiver)
        # Chain news floods: every node needs it, and it is small.
        for peer in self._rotated(message.receiver,
                                 node.relay_targets(message.sender)):
            self._send(message.receiver, peer, payload)

    # -- driving -----------------------------------------------------------

    def run(self, until=None):
        """Advance the simulation, then record the segment it covered.

        A segment is the unit of *observation*, not of simulation: it says what
        interval was watched and why the watching stopped, which is what makes
        right-censoring expressible. Without it a reader cannot tell "never
        reached" from "the file ends here".
        """
        self._require_unfinished("run")
        started = self.engine.now
        processed = self.engine.run(until=until)
        self.tracer.emit("seg", None, t=round(self.engine.now, 3),
                         start=round(started, 3), why=self.engine.stop_reason,
                         events=processed)
        return processed

    def finish_trace(self, status="complete"):
        """Declare the trace terminal. Nothing may be scheduled or run after.

        A `seg` boundary says how far *that* segment observed. Only this says no
        more data is coming, and the two are not interchangeable: a `run()` whose
        calendar drained can be followed by more births and another `run()`.

        The absence of this record therefore means exactly: still running,
        cleanly idle, crashed, or killed -- indistinguishable from the trace
        alone. A reader may treat recent wall-clock activity as a liveness hint
        but must never report a run as crashed, since a killed process emits
        nothing at all and may leave a half-written final line.

        The enforcement matters as much as the record: if scheduling could
        continue afterwards, `end` would not mean what it says.
        """
        if status not in ("complete", "error", "cancelled"):
            raise ValueError(f"unknown trace status {status!r}")
        self._require_unfinished("finish_trace")
        self._trace_finished = True
        self.tracer.emit("end", None, t=round(self.engine.now, 3),
                         status=status)
        # Seal the engine, not merely this wrapper. `sim.engine` is public, and
        # processing remaining calendar work after `end` would violate the
        # declared terminal boundary.
        #
        # Sealing rather than requiring an empty calendar: a `cancelled` finish
        # is a legitimate reason to stop with work outstanding, and demanding a
        # drained calendar would forbid exactly that.
        self.engine.seal()

    def _require_unfinished(self, what):
        if self._trace_finished:
            raise ValueError(
                f"cannot {what} after finish_trace(): the trace has declared "
                "itself terminal, and appending to it would make `end` a lie")

    def assert_invariants(self):
        now = self.engine.now
        for node in self.nodes:
            node.assert_invariants(now)
        for node in self.nodes:
            for object_id, admitted_at in node.view.held.items():
                assert admitted_at >= self.arena.objects[object_id].born
        # A node cannot know more than is true: its discard mask is always a
        # subset of the chain's. This is the invariant that would catch a
        # notifier leaking chain state straight into node belief.
        for node in self.nodes:
            assert node.known_discarded() & ~self.chain.discarded_mask() == 0
        self.engine.assert_invariants()

    def divergence_measurable(self):
        """Whether per-node knowledge can differ in this configuration.

        False under instant notification, and any metric that measures the cost
        of learning late -- stale aggregation above all -- must then report
        unavailable rather than zero. A structural zero read as a measurement
        would say the effect does not happen, when the model cannot express it.
        """
        return self.notifier.diverges

    # -- observation -------------------------------------------------------

    def delay_distance_correlation(self, reference_node=None):
        return _delay_distance_correlation(self, reference_node)

    def block_content_variance(self):
        """Variance in transactions per block: what rotation costs in steadiness."""
        counts = [block.count() for block in self.chain.blocks]
        if len(counts) < 2:
            return 0.0
        mean = sum(counts) / len(counts)
        return sum((count - mean) ** 2 for count in counts) / len(counts)

    def hop_distance_from(self, origin):
        return _hop_distance_from(self, origin)

    def admission_times(self, obj):
        """When each node learned of `obj`. None for nodes that never did."""
        times = []
        for node in self.nodes:
            times.append(node.view.admitted_at(obj))
        return times

    def residency_fraction(self, obj):
        return _residency_fraction(self, obj)

    def total_departures(self):
        """Held entries removed across all nodes, by every path.

        Supersession, discard pruning and candidate eviction -- the three ways an
        object leaves a view (`node.py:366`, `:378`, `:410`). Non-zero means the
        difference between reach and residency is live in this run.
        """
        total = 0
        for node in self.nodes:
            total += node.superseded + node.pruned + node.evicted
        return total

    def admission_delays(self, obj, window=None):
        return _admission_delays(self, obj, window)

    def coverage_fraction_within(self, obj, window):
        return _coverage_fraction_within(self, obj, window)

    def time_to_coverage(self, obj, fraction, window=None):
        return _time_to_coverage(self, obj, fraction, window)

    def inclusion_by_distance(self, reference_node):
        return _inclusion_by_distance(self, reference_node)

    def inclusion_distance_correlation(self, reference_node=None):
        """How strongly *being included at all* tracks distance from a node.

        Pearson between hops and a 0/1 inclusion indicator, over every
        transaction born rather than only those that reached a block. Negative
        means transactions entering far away are not merely late but dropped,
        which `delay_distance_correlation` cannot see: a transaction with no
        inclusion delay contributes nothing to a correlation over delays, so the
        strongest form of a placement penalty would leave that number untouched
        while quietly censoring the evidence for it.
        """
        if reference_node is None:
            reference_node = self.proposers.fixed_id
        pairs = []
        for hops, (born, included) in self.inclusion_by_distance(
                reference_node).items():
            for _ in range(included):
                pairs.append((float(hops), 1.0))
            for _ in range(born - included):
                pairs.append((float(hops), 0.0))
        return _pearson(pairs)

    def total_offered_bytes(self):
        """Bytes handed to egress queues, including anything still waiting."""
        return sum(self.offered_bytes)

    def total_sent_bytes(self):
        """Bytes of fully transmitted messages. Never exceeds link capacity."""
        return sum(self.sent_bytes)

    def wire_bytes(self, node_id):
        """Bytes this node has actually put on the wire, partial ones included.

        An egress server is busy exactly when it is transmitting at capacity, so
        busy time multiplied by capacity is the byte count -- and it accounts for
        the partly-sent message in service at a horizon, which counting whole
        messages cannot. Unmetered egress transmits instantly, so everything
        offered is on the wire.
        """
        if self.capacity is None:
            return self.offered_bytes[node_id]
        busy = self.egress[node_id].busy_time_at(self.engine.now)
        return busy * self.capacity

    def total_wire_bytes(self):
        total = 0.0
        for node_id in range(len(self.nodes)):
            total += self.wire_bytes(node_id)
        return total

    def total_ingress_bytes(self):
        return sum(self.ingress_bytes)

    def mean_egress_utilisation(self):
        if not self.nodes:
            return 0.0
        total = 0.0
        for node_id in range(len(self.nodes)):
            total += self.egress_utilisation(node_id)
        return total / len(self.nodes)

    def mean_cpu_utilisation(self):
        now = self.engine.now
        if now <= 0 or not self.nodes:
            return 0.0
        total = 0.0
        for server in self.cpu:
            total += server.busy_time_at(now)
        return total / (now * len(self.cpu))

    def max_cpu_depth(self):
        return max(server.max_waiting for server in self.cpu)

    def delay_mean(self):
        if not self.delays:
            return float("nan")
        return self.delay_sum / len(self.delays)

    def delay_percentile(self, fraction):
        if not self.delays:
            return float("nan")
        ordered = sorted(self.delays)
        index = max(0, math.ceil(fraction * len(ordered)) - 1)
        return ordered[index]

    # -- variant identity and metrics --------------------------------------

    def variant(self):
        return _variant(self)

    def metrics(self):
        return _metrics(self)

    def egress_utilisation(self, node_id, window_end=None):
        return _egress_utilisation(self, node_id, window_end)

    def max_egress_depth(self):
        return _max_egress_depth(self)


_SIGNATURE_DEFAULTS = None


def _field_default(name):
    """The signature's own default for `name`, read once and cached.

    Read rather than restated, so a default and the rule that compares against
    it cannot drift apart.
    """
    global _SIGNATURE_DEFAULTS
    if _SIGNATURE_DEFAULTS is None:
        _SIGNATURE_DEFAULTS = {
            parameter_name: parameter.default
            for parameter_name, parameter
            in inspect.signature(NetworkSim.__init__).parameters.items()}
    return _SIGNATURE_DEFAULTS[name]

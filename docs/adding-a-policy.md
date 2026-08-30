# Adding a policy or mode

The simulator represents configurable choices in two ways. Use a registered
callable when several implementations share one stable interface. Use a
string-validated mode when the choice changes wiring or state shape and cannot
be expressed by one existing callable boundary.

In either case, the selected name must appear in `NetworkSim.variant`, be
exposed by the relevant bench, and have tests for behavior, validation, and
determinism.

## Registered policy families

`starksim.strategies` holds four families. These are the exact callable
signatures used by the current wiring:

```python
# Return an immutable Selection.
selection(node, arena, tick, costs, budget, previous_aggregate=None)

# Return an immutable AggregationPlan; NetworkSim passes a non-empty selection.
fold_plan(selection)

# Return True when the older object should leave this node's view.
supersede(arena, known_discarded, old_object, new_object)

# Return a numeric retention score; the lowest scores are evicted first.
eviction(arena, known_discarded, obj)
```

Selection functions must use `arena.claim(obj)` for evidence and
`node.known_discarded()` for local liveness. They return a `Selection` whose
`inputs` and `input_claims` are parallel tuples, whose `claim_union` is their
union, whose `claimed` mask removes locally known discards, and whose revision
comes from `node.notifier.generation(node.id)`. The estimated cost comes from
`estimate_cost`, which must be given the arena, the node's view and the chosen
ids so that a `CircuitCost` prices what the circuit will verify rather than
reading a bare count as that many nested proofs. Tie-breaking must be explicit
and deterministic.

Fold planners must not mutate the selection, node, arena, or engine. Each
`PlanJob.inputs` entry is an arena object id, each `depends_on` entry is an
earlier job index, and the plan has one root index. `PlanJob.output_mask` and
`dep_count` must describe that job rather than the final plan.

Supersede functions compare an older and newer aggregate from the same peer.
They receive the node's discard belief, not canonical chain truth. Eviction
functions score one held object; `Node.enforce_candidate_limit` breaks equal
scores by admission age.

## Registration and discovery

Decorate the function with `register(family, name)`:

```python
from starksim.strategies import register

@register("fold_plan", "my_plan")
def my_plan(selection):
    ...
```

Registration happens when Python imports the module containing the decorated
function. There is no directory scan, entry-point loader, or plugin discovery.
The built-in selection and fold-plan functions register when
`starksim.aggregation` is imported; supersede and eviction functions register
when `starksim.node` is imported. A policy placed in a new module needs an
explicit import on the startup path before `NetworkSim` resolves its name.

`strategies.families()` returns the sorted family names and
`strategies.names(family)` returns the registered names in one family. The
decorator does not reject duplicate names; a later registration replaces the
earlier callable, so built-in names must remain unique.

`NetworkSim` resolves eviction and supersede callables during construction,
selection at each tick, and fold planning after selection. Unknown names fail
through `strategies.get` rather than falling back silently.

## String-validated modes

These `NetworkSim` fields select a mode that is implemented in the wiring
rather than a registry: `relay_policy`, `wrapper_economy`, `wrapper_mode`,
`fold_threshold`, `prove_cost_basis`, `cpu_discipline`, `inclusion_policy`,
`chain_notify`, `aggregation_trigger`, `on_tick_overrun`, plus
`proposer_mode`, which `ProposerSchedule.__init__` validates rather than
`NetworkSim.__init__`.

[configuration.md](configuration.md) lists their accepted values, defaults and
meanings; `tests/test_configuration.py` checks that list against the code.

The distinction matters when deciding where a new choice belongs. A registry
family is the right home when the choice is a *decision procedure* a reader
might want to replace — what to fold, what to evict, when to supersede. A mode
is the right home when the choice changes a mechanism the wiring owns, such as
whether a payload is even sent. If a mode's alternatives start to look like
pluggable procedures, it wants promoting to a family.

Add a mode only when the callable families cannot express the choice. Update
the validation set, the wiring branches, `variant`, and tests together. A new
mode must reject unknown values clearly and must not change the behavior of an
existing name.

## Variant, CSV, and sweep duties

A new choice is incomplete until its identity and experiment path are visible:

1. Add the selected name or value to `NetworkSim.variant`. Configuration does
   not belong in `metrics` alone because the trace header is built from the
   variant.
2. If the value is a variant field in CSV output, append its column to
   `starksim.netsim.CSV_COLUMNS`. Never reorder existing columns.
3. Make the configuration field reachable from `bench.sweep.AXES` when it is a
   generic sweep axis. Adding another name to an already exposed family, such
   as `selection_strategy`, needs no new axis entry.
4. Add focused unit tests for the callable or branch, its unknown-name failure,
   its variant identity, and at least one case where its output differs from an
   existing choice.
5. Add or extend a bench only if it supplies a workload in which the choice can
   affect a measured outcome. A selectable policy that never binds has not been
   compared.

`bench/sweep.py` accepts only fields in `AXES`; add a new generic sweep field
there so commands remain validated and reproducible. `--set KEY=VALUE` holds any
of the same names at a non-default value while a different one is swept.

## Determinism requirements

A policy must not draw from shared mutable randomness. Derive values from a
stable identity with `stable_fraction` or `stable_index`, or use a dedicated
labelled stream when no stable identity exists. State all tie-breaks; do not
depend on set or dictionary traversal order.

Bench workload generation must be a function of its seed alone. It must not
consume different random draws according to the policy under test. Repeating a
seed must produce byte-identical CSV, and a one-parameter sweep must keep all
unrelated workload choices paired.

## Worked example: a selection policy

This self-contained command registers a policy that selects the lowest-id live
object, confirms discovery, and runs one tick. It modifies no repository files:

```sh
python3 - <<'PY'
from starksim.aggregation import Selection, estimate_cost
from starksim.arena import KIND_PQ
from starksim.netsim import NetworkSim
from starksim.network import Overlay
from starksim.strategies import families, names, register


@register("selection", "lowest_id")
def lowest_id(node, arena, tick, costs, budget,
              previous_aggregate=None):
    known_discarded = node.known_discarded()
    candidates = {}
    for object_id in node.view.held:
        obj = arena.objects.get(object_id)
        if obj is not None and arena.claim(obj) & ~known_discarded:
            candidates[obj.id] = obj
    if previous_aggregate is not None:
        claim = arena.claim(previous_aggregate)
        if claim & ~known_discarded:
            candidates[previous_aggregate.id] = previous_aggregate

    if not candidates:
        return Selection((), (), 0, 0, tick,
                         node.notifier.generation(node.id),
                         estimate_cost(costs, 0))

    obj = candidates[min(candidates)]
    coverage = arena.claim(obj)
    claimed = coverage & ~known_discarded
    return Selection((obj.id,), (coverage,), coverage, claimed, tick,
                     node.notifier.generation(node.id),
                     estimate_cost(costs, claimed.bit_count(), arena,
                                   node.view, (obj.id,)))


print(families())
print(names("selection"))
overlay = Overlay.from_adjacency([[]], base_latency=0.0)
sim = NetworkSim(overlay, selection_strategy="lowest_id",
                 aggregation_trigger="aligned")
sim.schedule_birth(0.0, KIND_PQ, node_id=0)
sim.schedule_ticks(1)
sim.run()
print(sim.variant()["selection_strategy"], sim.wrappers_emitted)
PY
```

The final line is `lowest_id 1`. For a repository contribution, place the
function with the other selection strategies in `starksim/aggregation.py`, add
tests, and run it through the existing `selection_strategy` sweep axis.

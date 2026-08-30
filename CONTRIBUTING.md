# Contributing

The simulator has no runtime dependencies. Use Python 3.10 or newer from the
repository root; installation is not required.

## Run the simulator

User-facing run instructions are in [README.md](README.md), and
[docs/configuration.md](docs/configuration.md) shows direct programmatic use of
`NetworkSim`.

The benches, the viewer checks and the test suite all run from the repository
root without installation. [README.md](README.md) lists every command, and
`--help` on a bench module gives its workload and sweep parameters. Run
`python3 -m unittest discover -s tests` before committing, and the viewer
checks as well whenever the trace format or the viewer changed.

## Code style

- Do not use `@property` or getter/setter methods; prefer direct attribute
  access.
- Prefix private methods with an underscore.
- Group imports as standard-library imports, a blank line, then `starksim`
  imports.
- Prefer descriptive names over abbreviations.
- Avoid dense one-liners, nested ternaries, multi-clause comprehensions, and
  expressions that become difficult to scan when wrapped. Expand them into
  ordinary `if` and `for` statements.
- Explain math-heavy code with enough detail to reconstruct the reasoning.

A comment states a current constraint, invariant, complexity bound, or
counterintuitive behaviour. It must not narrate a review, phase, earlier draft,
or who found a bug. Preserve non-trivial comments when moving code; when a
comment must change, rewrite the underlying reason as a timeless constraint.

## Determinism

Determinism keeps seeded policy comparisons paired. The semantic contract is
recorded in
[ADR 0003](docs/decisions/0003-deterministic-randomness.md):

- Derive random values from stable identities such as `(seed, edge)` or use a
  dedicated, labelled random stream such as `"{seed}:fees"`. Do not let an
  unrelated draw change later values.
- A benchmark workload must be a function of its seed alone, never of the
  policy under test or the order in which parameter rows run. This keeps
  parameter sweeps paired.
- Repeating a command with the same seed must produce byte-identical CSV. Add a
  regression test when introducing a new source of randomness or a new sweep.
- Emit all parameters needed to identify a run in its variant and output.

## Evidence standards

Every published result must include its reproduction command, fixture and seed
count, evidence class, confidence, scope, and relevant model limits. Keep the
complete command and output. Label arithmetic-derived values as arithmetic
rather than measurements; do not present a single fixture as a general result.

Vary the setup to ensure a statistic measures its stated subject; see
[docs/pitfalls.md](docs/pitfalls.md). Evidence classes and confidence terms are
defined once in [docs/results.md](docs/results.md).

## Load-bearing contracts

Do not change these contracts as incidental cleanup:

- `starksim.netsim.CSV_COLUMNS` is ordered output schema. Append compatible
  columns when necessary; do not reorder or remove existing columns casually.
- Trace event names, fields, ordering, and meanings are replay contracts. A
  semantic or incompatible schema change requires a `SCHEMA_VERSION` bump and
  corresponding reducer, fixture, and test updates.
- Events sharing a timestamp execute in insertion-sequence order. The
  `(time, sequence)` tie-break and the order of floating-point accumulation are
  simulator semantics.
- Seed derivation and labelled stream identities are experiment contracts.
  Changing either can invalidate paired comparisons and published baselines.

For extension mechanics and required variant, CSV, sweep, and test updates, see
[docs/adding-a-policy.md](docs/adding-a-policy.md). Keep the boundaries in
[docs/architecture.md](docs/architecture.md) intact; a change to a recorded
boundary must update or supersede the corresponding architecture decision.

## Changes and review

Make atomic, Linux-style commits: one logical change, an imperative subject,
and no compound “this and that” summary. Include tests that fail without a
behavioural fix, and keep generated benchmark output out of commits unless it
is an intentional published fixture.

Read [docs/pitfalls.md](docs/pitfalls.md) before adding an instrument or
reporting a result.

Commit before a mutation-testing experiment, and clear `__pycache__` between
runs. Reverting mutants with `git checkout --` discards uncommitted work in the
same files, which can silently delete the feature under test and make every
mutant look killed; and a rewrite can beat mtime resolution, so a stale `.pyc`
reports a surviving mutant as caught.

An adversarial review is required before merging a change to model semantics,
an experiment harness, a published result, a stable output or trace contract,
or a cross-cutting architecture decision. The review compares the relevant
specification, implementation, tests, and claimed results. Reproduce each
finding independently before changing code in response to it. A green suite is
necessary, but it does not detect a model that omits a cost, assumes unavailable
knowledge, or measures its own harness.

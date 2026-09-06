A discrete-event simulation for EIP-8288.

## Code Patterns

Follow [CONTRIBUTING.md](CONTRIBUTING.md), including its code-style,
determinism, evidence, stable-output, architecture, testing, and review
requirements. When producing or changing a measurement, also follow the
evidence contract in [docs/results.md](docs/results.md) and the traps in
[docs/pitfalls.md](docs/pitfalls.md).

**Comments:**
- When moving code, preserve original comments. Don't modify non-trivial comments without asking first.
- Don't add significant comments during big refactors without asking first.

# git
- Follow the atomic-commit and generated-output rules in [CONTRIBUTING.md](CONTRIBUTING.md).

# Layout:
- starksim/: the business-logic package
  - engine.py: domain-agnostic discrete-event engine (calendar, servers, queues)
  - derive.py: seeded values derived from stable identities
  - arena.py: pool objects and coverage masks (global truth only)
  - chain.py: chain facts, notifiers, blocks, proposer schedule
  - network.py: overlay, link latency, size model
  - payload.py: typed wire payloads
  - node.py: node state and per-node beliefs
  - aggregation.py: selection and fold plans (pure)
  - strategies.py: the registry of swappable policies
  - presets.py: named configurations as constructor keyword arguments
  - costs.py: computation cost table
  - trace.py: JSONL tracing with focus and sampling
  - netsim.py: domain wiring and event handlers
  - reporting.py: analytical queries, variant, metrics, and CSV contract
  - knowledge.py: per-node knowledge classification and tracing
- tests/: unittests
- bench/: reproducible measurement scripts
- tools/: viewer fixtures, checks, and the paced live runner
- serve.py: static server with byte-range support for live traces
- viewer.html: live view / replay of trace.jsonl

# Files in docs/:
- quickstart.md: the newcomer's commands, in order; owns no facts
- explainer.md: EIP-8288, the model's moving parts, and how to read a run and the viewer; owns no facts
- model.md: READ FIRST -- current simulation semantics and non-goals
- architecture.md: module map, state ownership, event walkthrough, extension points
- configuration.md: every selectable parameter, policy and mode, with defaults
- results.md: published measurements, commands, scope, and confidence
- roadmap.md: ranked open questions and known limitations
- trace-format.md: trace.jsonl event schema and reconstruction rules
- viewer.md: trace production, replay, live following, and verification
- adding-a-policy.md: registry signatures, modes, and extension example
- glossary.md: model, policy, objective, and trace terms
- pitfalls.md: concepts that are easy to conflate and instrumentation that lies
- decisions/: current architecture decisions and code locations
- proposals/: candidate protocol mechanisms to evaluate; not specification
- history/: retired designs and plans; not current specification
- eip-8288.md: snapshot of the draft EIP used by the model

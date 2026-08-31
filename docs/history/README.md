# Historical records

Two retired documents, kept because the current ones cannot replace them:

- [v1-model.md](v1-model.md) — the model this simulator replaced. It derived
  visibility from arithmetic on timestamps, and it is the clearest statement of
  the assumption v2 exists to remove. Its final section records what v2 does
  differently, measured: `bench/v1_calibration.py` runs the v2 side, and its
  docstring carries the command that extracts and runs v1 from the commit that
  still has it.
- [networking-plan.md](networking-plan.md) — v2's design as written and revised,
  with the rationale behind each decision and the questions it left open. Source
  comments cite its `[Dn]` labels where a design decision is still load-bearing.

Neither is current specification. Current behaviour and supported extension
points come from [model.md](../model.md), [architecture.md](../architecture.md),
[configuration.md](../configuration.md), [results.md](../results.md) and
[roadmap.md](../roadmap.md), or from the code — never from a historical plan or
a result table inside one.

Both documents name things that no longer exist: files that were reorganised or
deleted with v1, event names that never reached the code, and result tables since
shown to be confounded and marked as such where they appear. They are records of
intent at a moment, not of the tree as it stands.

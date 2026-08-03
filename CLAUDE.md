A discreet-time simulation for EIP-8288.

## Code Patterns

**Code style:**
- Avoid `@property` decorators and getter/setter methods. Prefer direct attribute access.
- Prefix private methods with underscores (e.g., `_helper_method`).
- Group imports: stdlib imports first, blank line, then soundcalc modules.
- Prefer bigger variable names, than abbrevations (e.g. `selection` over `sel`, `effective` over `eff`).
- No dense one-liners: avoid nested ternaries, comprehensions with multiple `for` clauses or compound conditions, and any expression that wraps across lines. Unfold them into plain multi-line `if`/`for` statements — more lines is fine if it reads clearer.

**Comments:**
- When moving code, preserve original comments. Don't modify non-trivial comments without asking first.
- Don't add significant comments during big refactors without asking first.
- For math-heavy code, add verbose comments explaining the reasoning.

# Layout:
- starksim/: the business-logic package
  - objects.py: mempool objects
  - actors.py: the various actors of the simulation
  - sim.py: Simulation engine (run with `python -m starksim.sim`)
- tests/: unittests
- viewer.html: live view / replay of trace.jsonl

# Files in docs/:
- The EIP verbatim is saved at eip-8288.md

#!/usr/bin/env python3
"""
The registry of swappable policies.

The simulator exists to compare design options, so a protocol or policy choice
is a named strategy selected by configuration rather than a branch in the wiring.
The distinction matters in practice: a branch cannot be enumerated, cannot be
recorded in a run's variant identity, and cannot be swept.

Lives on the domain side, not in `engine.py`, which knows nothing about mempools.
"""

_REGISTRIES = {}


def register(family, name):
    """Decorator: put a strategy in a family under a name."""
    def attach(function):
        _REGISTRIES.setdefault(family, {})[name] = function
        return function
    return attach


def get(family, name):
    family_entries = _REGISTRIES.get(family)
    if family_entries is None:
        raise KeyError(f"unknown strategy family {family!r}")
    strategy = family_entries.get(name)
    if strategy is None:
        known = ", ".join(sorted(family_entries))
        raise KeyError(f"unknown {family} strategy {name!r}; known: {known}")
    return strategy


def names(family):
    return sorted(_REGISTRIES.get(family, {}))


def families():
    return sorted(_REGISTRIES)

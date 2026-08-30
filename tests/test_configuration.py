#!/usr/bin/env python3
"""`docs/configuration.md` against the code it describes.

A list of parameters, choices and defaults is derivable from the code, so a
hand-written copy of it is a defect waiting to happen: three separate reviews of
this repository found exactly that -- a table naming two of three proving bases,
one claiming sweep axes were missing that were present, one naming a field that
had been renamed.

So the document carries the prose and this carries the facts. Nothing here reads
a description or polices wording; every assertion is about a name, a value, or a
default. A parameter added, renamed, retyped or re-defaulted without touching the
document fails the suite instead of misleading a reader.
"""

import inspect
import re
import unittest
from pathlib import Path

from starksim.costs import CircuitCost, CostTable
from starksim.netsim import (CPU_DISCIPLINES, FOLD_THRESHOLDS,
                             INCLUSION_POLICIES, PROVE_COST_BASES,
                             RELAY_POLICIES, WRAPPER_ECONOMIES, WRAPPER_MODES,
                             NetworkSim)
from starksim.network import SizeModel
from starksim.strategies import names

DOCUMENT = Path(__file__).resolve().parent.parent / "docs" / "configuration.md"

# Parameters that are not configuration: `overlay` is positional, and `self`
# is not an argument.
NOT_CONFIGURATION = {"self", "overlay"}

# The choice sets the document offers, against the code that validates them.
CHOICE_SETS = {
    "relay_policy": RELAY_POLICIES,
    "wrapper_economy": WRAPPER_ECONOMIES,
    "wrapper_mode": WRAPPER_MODES,
    "fold_threshold": FOLD_THRESHOLDS,
    "prove_cost_basis": PROVE_COST_BASES,
    "cpu_discipline": CPU_DISCIPLINES,
    "inclusion_policy": INCLUSION_POLICIES,
    "selection_strategy": tuple(names("selection")),
    "fold_plan": tuple(names("fold_plan")),
    "supersede_policy": tuple(names("supersede")),
    "eviction_policy": tuple(names("eviction")),
    "chain_notify": ("instant", "overlay"),
    "on_tick_overrun": ("fifo", "abandon"),
    "aggregation_trigger": ("aligned", "spread", "when_ready"),
    "proposer_mode": ("fixed", "rotating", "random", "dedicated"),
}


def documented_rows(text):
    """Every table row of the documented form, as {owner: {name: default}}.

    The convention the document states: the parameter in backticks in the first
    column, its default in backticks in the second. A row whose second column is
    not a bare backticked value is a description row and is skipped, so prose
    tables cost nothing here.

    Rows are attributed to an owner by their table's header: a first column of
    "`X` field" belongs to class `X`, anything else to `NetworkSim`. Without that,
    a name two classes share -- `seed` is on both `NetworkSim` and `CostTable` --
    would have one row silently checked against the other's default.

    Only tables whose header names a Default column are read. Any other table
    may use backticks in its first two columns without being mistaken for a
    parameter listing, which the compatibility table below promptly did.
    """
    rows = {"NetworkSim": {}}
    owner = "NetworkSim"
    reading = False
    header = re.compile(r"^\|\s*`([A-Za-z_][A-Za-z0-9_]*)`\s+field\s*\|")
    field = re.compile(r"^\|\s*`([A-Za-z_][A-Za-z0-9_]*)`\s*\|\s*(.*?)\s*\|")
    for line in text.splitlines():
        if line.startswith("## "):
            owner, reading = "NetworkSim", False
            continue
        if not line.startswith("|"):
            # A table ends where the pipes stop. Without this, a row detached
            # from its header by a paragraph was still read as part of the
            # table above it -- so a malformed document parsed as a valid one.
            reading = False
            continue
        columns = [cell.strip() for cell in line.strip("|").split("|")]
        if all(set(cell) <= {"-", ":"} for cell in columns if cell):
            continue                                    # the separator row
        owning = header.match(line)
        if owning is not None:                          # "| `X` field | ... |"
            owner, reading = owning.group(1), True
            rows.setdefault(owner, {})
            continue
        if "`" not in line:                             # any table's header row
            owner = "NetworkSim"
            reading = columns[1:2] == ["Default"]
            continue
        if not reading:
            continue
        match = field.match(line)
        if match is None:
            continue
        name, default_cell = match.groups()
        value = re.fullmatch(r"`([^`]*)`", default_cell)
        if value is not None:
            if name in rows[owner]:
                # Two rows for one parameter: the later silently won, so a
                # contradictory duplicate could sit above the correct one and
                # be checked against nothing.
                rows[owner][name] = "DUPLICATE"
                continue
            rows[owner][name] = value.group(1)
        elif default_cell == "required":
            rows[owner][name] = "required"
        elif default_cell == "":
            # An empty Default cell is not "documented elsewhere", it is a
            # documented parameter with its default missing -- which is how a
            # wrong default could be hidden by deleting it.
            rows[owner][name] = ""
    return rows


def normalise(value):
    """One spelling for a default, so `flood` and `'flood'` compare equal."""
    if isinstance(value, str):
        return value.strip("'\"")
    if value is None:
        return "None"
    return str(value)


def same_default(documented, actual):
    """Whether two spellings of a default agree.

    Numbers compare numerically, so a table may write `1000` or `1000.0` for the
    same float without the check caring which. Everything else is exact.
    """
    if documented == actual:
        return True
    try:
        return float(documented) == float(actual)
    except (TypeError, ValueError):
        return False


def code_defaults(callable_object):
    defaults = {}
    for name, parameter in inspect.signature(callable_object).parameters.items():
        if name in NOT_CONFIGURATION:
            continue
        if parameter.default is inspect.Parameter.empty:
            defaults[name] = "required"
        else:
            defaults[name] = normalise(parameter.default)
    return defaults


class TestConfigurationDocument(unittest.TestCase):
    OWNERS = (("NetworkSim", NetworkSim.__init__),
              ("SizeModel", SizeModel.__init__),
              ("CostTable", CostTable.__init__),
              ("CircuitCost", CircuitCost.__init__))

    def setUp(self):
        self.text = DOCUMENT.read_text(encoding="utf-8")
        # The EIP-correspondence table lists the same fields for a different
        # purpose -- what the specification says, not what the parameter is --
        # and `tests/test_eip_faithful.py` owns it. Two tables about one field
        # are not duplicates when only one of them is the parameter listing.
        self.parameters = re.sub(r"<!-- eip-faithful:start -->.*?"
                                 r"<!-- eip-faithful:end -->", "",
                                 self.text, flags=re.S)
        self.rows = documented_rows(self.parameters)

    def test_every_parameter_is_documented(self):
        for owner, callable_object in self.OWNERS:
            with self.subTest(owner=owner):
                expected = set(code_defaults(callable_object))
                missing = expected - set(self.rows.get(owner, {}))
                self.assertEqual(missing, set(),
                                 f"{owner} parameters absent from "
                                 "docs/configuration.md")

    def test_nothing_documented_has_been_removed(self):
        # The other direction: a parameter deleted from the code leaves a row
        # describing something that no longer exists.
        for owner, callable_object in self.OWNERS:
            with self.subTest(owner=owner):
                stale = set(self.rows.get(owner, {}))
                stale -= set(code_defaults(callable_object))
                self.assertEqual(stale, set(),
                                 f"documented {owner} fields that no longer "
                                 "exist in the code")

    def test_documented_defaults_match_the_code(self):
        for owner, callable_object in self.OWNERS:
            documented_here = self.rows.get(owner, {})
            for name, actual in code_defaults(callable_object).items():
                documented = documented_here.get(name)
                if documented is None:
                    continue
                self.assertNotEqual(
                    documented, "DUPLICATE",
                    f"{owner}.{name} is documented more than once")
                self.assertNotEqual(
                    documented, "",
                    f"{owner}.{name} has an empty Default cell; it defaults "
                    f"to {actual!r}")
                with self.subTest(owner=owner, field=name):
                    self.assertTrue(
                        same_default(documented, actual),
                        f"{owner}.{name} is documented as {documented!r} and "
                        f"defaults to {actual!r} in the code")

    def test_every_offered_choice_is_one_the_code_accepts(self):
        # Read the Choices *column*, not the whole row: descriptions mention
        # other field names in backticks, and intersecting them away would let
        # an invalid choice through unnoticed. Set equality both ways, so an
        # offered value the code rejects fails as loudly as a valid one the
        # document never mentions.
        for field, valid in CHOICE_SETS.items():
            with self.subTest(field=field):
                cells = [cell.strip() for cell in
                         self._row_for(field).strip("|").split("|")]
                self.assertGreaterEqual(len(cells), 4,
                                        f"{field} has no Choices column")
                offered = set(re.findall(r"`([^`]+)`", cells[2]))
                self.assertEqual(offered, set(valid),
                                 f"{field} offers {sorted(offered)}, "
                                 f"code accepts {sorted(valid)}")

    def test_the_sweep_axes_are_listed_exactly(self):
        from bench.sweep import AXES

        # Anchored on a comment rather than on prose. The previous marker was
        # a sentence, and renaming "five" to "six" left this splitting on
        # nothing: the check silently scanned the whole document and could no
        # longer fail. A split that matches nothing must say so.
        self.assertIn("<!-- sweep-axes:start -->", self.parameters)
        self.assertIn("<!-- sweep-axes:end -->", self.parameters)
        section = self.parameters.split("<!-- sweep-axes:start -->", 1)[1]
        section = section.split("<!-- sweep-axes:end -->", 1)[0]
        listed = set(re.findall(r"`([a-z_]+)`", section))
        listed -= {"--axis", "--set"}
        listed -= {"axis", "set", "values"}
        self.assertEqual(listed, set(AXES),
                         "the document's axis list and bench.sweep.AXES "
                         f"differ: {sorted(listed ^ set(AXES))}")

    def _row_for(self, field):
        for line in self.parameters.splitlines():
            if line.startswith(f"| `{field}` |"):
                return line
        self.fail(f"no table row for {field}")


if __name__ == "__main__":
    unittest.main()

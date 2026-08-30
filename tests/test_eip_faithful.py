#!/usr/bin/env python3
"""The defaults are the EIP as written, and this holds them to it.

Every measured deviation in `docs/results.md` is measured against the defaults,
which only means anything if the defaults are the specification's own reading.
That claim is stated once, in `docs/configuration.md`, and it is the kind of
claim that rots quietly: a default changed for a good local reason leaves the
whole results section comparing against something nobody chose.

So the correspondence is a table the document carries and this test checks. It
asserts values, never prose -- the citation and the reasoning are the
document's, and are free to be rewritten.
"""

import re
import unittest
from pathlib import Path

from starksim.netsim import NetworkSim

from tests.support import two_node_overlay

DOCUMENT = Path(__file__).resolve().parent.parent / "docs" / "configuration.md"
START = "<!-- eip-faithful:start -->"
END = "<!-- eip-faithful:end -->"


def faithful_rows(text):
    """{field: documented default} from the anchored table.

    Anchored on comments rather than on a heading or a sentence: the last
    checker in this repository was silently disabled when prose it split on was
    reworded, so the anchors are asserted to exist before anything is read.
    """
    assert START in text and END in text
    section = text.split(START, 1)[1].split(END, 1)[0]
    rows = {}
    pattern = re.compile(r"^\|\s*`([a-z_]+)`\s*\|\s*`([^`]*)`\s*\|")
    for line in section.splitlines():
        match = pattern.match(line)
        if match is not None:
            rows[match.group(1)] = match.group(2)
    return rows


class TestTheDefaultsAreTheEip(unittest.TestCase):
    def setUp(self):
        self.text = DOCUMENT.read_text(encoding="utf-8")
        self.rows = faithful_rows(self.text)
        self.variant = NetworkSim(two_node_overlay(10.0)).variant()

    def test_the_anchors_exist(self):
        # A split that matches nothing must fail rather than read everything.
        self.assertIn(START, self.text)
        self.assertIn(END, self.text)
        self.assertGreater(len(self.rows), 10)

    def test_every_documented_field_holds_its_documented_value(self):
        for field, documented in self.rows.items():
            with self.subTest(field=field):
                self.assertIn(field, self.variant,
                              f"{field} is not part of a run's identity")
                actual = self.variant[field]
                if isinstance(actual, float):
                    self.assertEqual(float(documented), actual)
                else:
                    self.assertEqual(documented, str(actual))

    def test_the_table_covers_every_field_a_reader_would_ask_about(self):
        # The policy choices, specifically: a reader comparing a result against
        # "the EIP" needs to know which of these the EIP actually decided.
        for field in ("aggregation_trigger", "aggregation_interval",
                      "wrapper_mode", "fold_threshold", "fold_plan",
                      "selection_strategy", "inclusion_policy",
                      "wrapper_economy", "supersede_policy", "relay_policy"):
            with self.subTest(field=field):
                self.assertIn(field, self.rows)

    def test_each_row_says_whether_the_eip_decided_it(self):
        # The distinction is the point of the table: a citation, or an explicit
        # "not specified". A row with neither is a claim about the EIP that
        # nobody can check.
        section = self.text.split(START, 1)[1].split(END, 1)[0]
        for line in section.splitlines():
            if not line.startswith("| `"):
                continue
            with self.subTest(row=line[:32]):
                self.assertTrue(
                    re.search(r"`:\d+`", line) or "Not specified" in line,
                    "a row must cite the EIP or say it is not specified")


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""The throughput bench's machine-readable records describe their own run."""

import contextlib
import io
import json
import sys
import unittest
from unittest import mock

from bench import throughput


class TestJsonRecords(unittest.TestCase):
    def test_each_record_carries_its_own_numeric_rate(self):
        # A sweep is one argument string but many runs; copying the namespace
        # verbatim would file the whole sweep string under `tps` in every
        # record, so a reader could not tell which rate a record measured.
        argv = ["throughput", "--json", "--tps", "1,2", "--nodes", "2",
                "--degree", "1", "--window", "1000", "--drain", "0",
                "--seeds", "1", "--presets", "eip_faithful"]
        stdout = io.StringIO()
        with mock.patch.object(sys, "argv", argv):
            with contextlib.redirect_stdout(stdout):
                throughput.main()
        records = [json.loads(line) for line in
                   stdout.getvalue().splitlines()]
        self.assertEqual(len(records), 2)
        for record, tps in zip(records, (1.0, 2.0)):
            workload = record["workload"]
            self.assertEqual(workload["tps"], tps)
            self.assertEqual(workload["transactions"],
                             int(round(tps * 1000 / 1000.0)))


if __name__ == "__main__":
    unittest.main()

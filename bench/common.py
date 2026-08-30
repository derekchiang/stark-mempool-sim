"""Shared mechanics for benchmark harnesses.

Workload construction and interpretation stay in each benchmark. This module
contains only contracts repeated across harnesses: topology derivation, simple
summary operations, schedule alignment, table rules, and JSONL envelopes.
"""

import json
import math
import random
import statistics

from starksim.presets import names as preset_names, preset
from starksim.network import Overlay


def random_regular_overlay(nodes, degree, latency, seed, jitter_frac=0.1):
    """Build the benchmark topology without changing its random stream.

    The labelled seed and the separate latency seed are part of reproducibility:
    topology construction consumes only ``"{seed}:topology"`` while edge
    latency jitter is identity-derived from ``seed``.
    """
    topology_rng = random.Random(f"{seed}:topology")
    return Overlay.random_regular(
        nodes, degree, topology_rng, base_latency=latency,
        jitter_frac=jitter_frac, latency_seed=seed)


def mean_field(runs, key):
    """Arithmetic mean of one top-level field across run summaries."""
    return statistics.fmean(run[key] for run in runs)


def mean_path(runs, *path):
    """Arithmetic mean of one nested field path across run summaries."""
    values = []
    for run in runs:
        value = run
        for key in path:
            value = value[key]
        values.append(value)
    return statistics.fmean(values)


def add_preset_argument(parser):
    """`--preset NAME`: start from a named configuration."""
    parser.add_argument("--preset", default=None, choices=preset_names(),
                        help="start from a named configuration "
                             "(starksim.presets); explicit policy flags "
                             "still win over it")


def preset_settings(args):
    """The keyword arguments a chosen preset contributes, or none."""
    return preset(args.preset) if getattr(args, "preset", None) else {}


def print_table_header(header):
    """Print a table heading followed by a width-matched ASCII rule."""
    print(header)
    print("-" * len(header))


def schedule_horizon(sim, horizon):
    """Schedule whole tick and slot periods through ``horizon``."""
    sim.schedule_ticks(int(horizon / sim.aggregation_interval))
    sim.schedule_slots(int(horizon / sim.slot_length))


def workload_parameters(args, **specific):
    """JSON-ready per-run workload settings from an argparse namespace."""
    workload = vars(args).copy()
    for output_field in ("json", "csv", "seeds"):
        workload.pop(output_field, None)
    workload.update(specific)
    return workload


def emit_json_record(benchmark, workload, seed, sim, metrics=None):
    """Write one strict-JSON record for one completed simulation run."""
    run_metrics = sim.metrics()
    if metrics is not None:
        run_metrics.update(metrics)
    record = {
        "benchmark": benchmark,
        "workload": workload,
        "seed": seed,
        "variant": sim.variant(),
        "metrics": run_metrics,
    }
    print(json.dumps(_json_value(record), sort_keys=True,
                     separators=(",", ":"), allow_nan=False))


def _json_value(value):
    """Replace non-finite floats, which JSON cannot represent, with null."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value

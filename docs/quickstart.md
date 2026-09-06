# Quick start

Five minutes from a clone to a run you can read. This page owns the commands
a newcomer runs first; what they measure is defined in
[model.md](model.md), the flags in [configuration.md](configuration.md), and
the numbers that have been published in [results.md](results.md). Nothing
here is a result.

Python 3.10 or newer, nothing to install. Every bench command below runs
from the repository root and finishes in seconds.

## Run something

```sh
python3 -m bench.overload --tps 1.5 --seeds 1
```

This offers a small privacy-transaction load to a forty-node overlay for a
few seconds, stops, and watches the backlog drain. It is a smoke run: the row
it prints demonstrates the measurement vocabulary and is not a published
claim. Published rows, their commands and their confidence are in
[results.md](results.md).

The header names the topology, the uplink, the relay policy, the queue
bounds and the seed; every bench prints one, because a row without its
configuration cannot be attributed to anything. The load table asks what
breaks first as offered load approaches what the uplinks can physically
carry. The recovery table asks whether the backlog that existed when births
stopped clears within the drain the bench allows; how long after the load
stops the network is still working through it is the number that matters,
and a finite drain cannot tell a slow queue from one that never clears. The
footer defines the columns, and the two it dwells on, offered
against ceiling and residency against reach, are the ones most often
misread; [glossary.md](glossary.md#model-terms) defines the terms.

## A mainnet-shaped run

```sh
python3 -m bench.mainnet --seeds 1 --tps 15 --privacy-share 0.2 \
    --presets eip_faithful
```

One cell of the mainnet-shaped workload: 12-second slots, a transaction
stream in which one in five carries a privacy STARK and the rest bare
post-quantum signatures, and proving priced from a real prover's published
cycle counts. The header states the calibration and what it does not
cover; read it once.

The row is the point. Its four columns, inclusion, delay, one machine's CPU
utilisation over the birth window, and the mean claim per root aggregate,
say together whether an ordinary node keeps pace with this load and what is
binding when it does not; the footer defines each. This is one seed at one
operating point. The published frontier, swept over rate, privacy share and
machine speed with several seeds, is the O2 entry in
[results.md](results.md). This cell is your check that the harness runs and
what its output looks like, not a substitute for it.

## Change one thing

```sh
python3 -m bench.mainnet --seeds 1 --tps 15 --privacy-share 0.2 \
    --presets eip_faithful,high_throughput
```

Same seed, same births, same topology, two policy sets. `eip_faithful` is
the EIP as this simulator reads it; `high_throughput` is the set of
bandwidth levers that a measurement found to compose. Compare the two rows
column by column. Whatever moved, the workload did not, so the policy did.
That pairing is the simulator's basic instrument, and every comparison in
[results.md](results.md) is built from it.

A preset names policy fields only. The overlay, the message sizes and the
cost table come from the bench, so a preset is not a complete configuration.
`python3 -m bench.mainnet --help` lists what this bench sets and sweeps;
[configuration.md](configuration.md#named-configurations) describes the
presets, and the rest of that page every field a run can select.

## Watch a run

The tables report what happened. The viewer replays what was traced:
propagation, knowledge and resource state, event by event.

```sh
python3 -m tools.live_run --duration 48 --speed 16 --tps 4 --follow-rate 1
```

This writes `trace.jsonl` in the current directory, overwriting one already
there, and stops after 48 simulated seconds; the last line it prints says
`sealed: complete`. Without `--duration` the runner paces itself against the
wall clock and runs until stopped, for watching a run unfold live.

Serve the directory and open the viewer:

```sh
python3 serve.py
```

Open <http://127.0.0.1:8000/viewer.html>. Pause, read the line under the
title, which is the trace header, and press the `?` beside the page tabs:
it says what each page answers and how to read the colours. From there,
[explainer.md](explainer.md#reading-the-viewer) has the reading order for
each page and [viewer.md](viewer.md#what-the-viewer-shows) the mechanics.
With `--follow-rate 1` every transaction was recorded, so all of them are
there to step through.

To replay a trace produced elsewhere, place it at `trace.jsonl` beside
`viewer.html` and reload; [viewer.md](viewer.md#serve-and-replay) has the
server's contract.

## Where next

| You want to | Read |
| --- | --- |
| Understand what the simulator models, and what it refuses to | [explainer.md](explainer.md), then [model.md](model.md) |
| Know what has been measured and how far it travels | [results.md](results.md) |
| Change a parameter or a policy | [configuration.md](configuration.md) |
| Add a policy of your own | [adding-a-policy.md](adding-a-policy.md) |
| Produce or interpret a trace programmatically | [viewer.md](viewer.md), [trace-format.md](trace-format.md) |
| Avoid the mistakes the instruments invite | [pitfalls.md](pitfalls.md) |
| Know what is still open | [roadmap.md](roadmap.md) |
| Contribute | [CONTRIBUTING.md](../CONTRIBUTING.md) |

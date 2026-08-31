# starksim

`starksim` is a standard-library Python discrete-event simulator for the
mempool protocol proposed by [EIP-8288](docs/eip-8288.md). It models mempool
nodes on an overlay, finite egress bandwidth, link latency, local knowledge,
verification and proving queues, periodic recursive aggregation, per-peer
wrappers, and a slot proposer building from its own partial view.

There is no cryptography in the simulator. A proof is a transaction-claim mask,
a wire size, and a configured duration. EIP-8288 is a draft, and this repository
does not model FOCIL, adversaries, reorgs, transaction nonces or ordering, or a
fee market beyond a scalar. See [the model](docs/model.md) for the complete
semantics and interpretation limits.

Python 3.10 or newer is required. Running the simulator and benches needs no
installation and no third-party package. Node.js is optional and is used only
to check the HTML viewer.

## Run a quick experiment

From the repository root:

```sh
python3 -m bench.overload --tps 1.5 --seeds 1
```

This short fixture prints a load table and a recovery table:

```text
load sweep — what breaks first, measured at the end of the load
offered/ceil  births   wire MB  drop msg  drop MB  queue MB  uplink  residency
------------------------------------------------------------------------------
        0.58      12     568.3         0      0.0      12.9  56.8%     100.0%

recovery — does the backlog drain once the load stops
offered/ceil  queue at load  queue after drain  drained  wire during drain  residency
-------------------------------------------------------------------------------------
        0.58          12.9M               0.0M    1/1              65.4M     100.0%
```

The header records the topology, capacities, policy, bounds, and seed. Here
`offered/ceil` compares accepted egress traffic with the physical uplink ceiling;
the second table checks whether queued bytes drain after births stop.

## Named configurations

Three configurations are referred to by name, in `starksim.presets`:

- `eip_faithful` — the EIP as written. These are the constructor's defaults,
  held to the EIP snapshot citation by citation in
  [configuration.md](docs/configuration.md).
- `v1_equivalent` — the retired v1 model, as far as configuration reaches
  it: free bytes, instant settlement, continuous uniform folding, claims
  enough for inclusion. `bench.v1_calibration` adds the complete graph and
  the constant fold cost that configuration cannot.
- `high_throughput` — the bandwidth levers that raise the transaction rate a
  25 Mbit/s uplink sustains: every lever `bench.levers` measures except
  `capped8`, which `bench.throughput` showed caps inclusion. The name is what
  that measurement found on its fixture.

`NetworkSim(overlay, **preset("high_throughput"))` starts from one;
`tools.live_run --preset NAME` runs one live.

## Reproduce the experiments

The repository includes these benchmark modules:

```sh
python3 -m bench.propagation  # uncontended and loaded propagation, relay comparison
python3 -m bench.economy      # wrapper, raw-object, and proof-economy costs
python3 -m bench.overload     # queueing, loss, and recovery around capacity
python3 -m bench.placement    # proposer position, delay, and exclusion
python3 -m bench.triage       # selection and eviction under a proving budget
python3 -m bench.sweep --axis fold_plan --values wide,binary
python3 -m bench.v1_calibration  # the retired v1 model, and what replacing it cost
python3 -m bench.levers          # do the bandwidth levers compose?
python3 -m bench.throughput      # sustainable transaction rate, per named configuration
python3 -m bench.mainnet         # 12 s slots, mixed PQ/privacy load, leanVM-b-calibrated proving
```

Use `--help` on a module for its workload and sweep parameters. Keep the full
command and stdout with any reported result. [results.md](docs/results.md)
records the published tables, commands, scope, and confidence.

## View a trace

The viewer is a self-contained HTML file that can replay a completed JSONL trace
or follow one while it grows. Start a paced run and the range-capable server in
separate terminals:

```sh
python3 -m tools.live_run
python3 serve.py
```

Then open <http://127.0.0.1:8000/viewer.html>. Use `serve.py` for a live trace;
`python3 -m http.server` does not support the byte ranges needed for incremental
polling.

The runner takes a named configuration, a transaction rate, and the policy
flags of `NetworkSim`; `--help` lists them all. Some runs worth watching:

```sh
python3 -m tools.live_run --preset high_throughput --tps 32        # the levers under load, ~128 per block
python3 -m tools.live_run --preset eip_faithful --tps 32           # the EIP as written on the same load
python3 -m tools.live_run --preset v1_equivalent --tps 8           # the retired model, free bytes, instant settlement
python3 -m tools.live_run --tps 64 --egress-bytes 2000000          # overload: a small send buffer refusing messages
python3 -m tools.live_run --relay-policy announce_fetch --tps 16   # one policy flag over the defaults
python3 -m tools.live_run --tps 4 --follow-rate 1 --speed 0.25     # every transaction followed, at quarter speed
```

The viewer has three pages: charts of the whole run, the network coloured
by what each node knows of the followed transaction, and that transaction's
life on its own clock. The follow mode picks the transaction worth watching
as the run goes; pin one from the ticker to hold it.

Check the viewer's data path and rendering with:

```sh
python3 -m tools.make_viewer_fixture .render-fixture
node tools/check_viewer.mjs .render-fixture viewer.html
npm ci
npx playwright install chromium
npx playwright test
```

See [viewer.md](docs/viewer.md) for programmatic trace generation, replay, live
runner options, and what each verification layer covers.

## Test

Run the complete Python suite from the repository root:

```sh
python3 -m unittest discover -s tests
```

Contributor conventions, deterministic-randomness rules, and stable output
contracts are in [CONTRIBUTING.md](CONTRIBUTING.md).

## Documentation

- [Model](docs/model.md) — current simulation semantics and non-goals.
- [Architecture](docs/architecture.md) — module map, state ownership, and event walkthrough.
- [Configuration](docs/configuration.md) — every parameter, policy and mode a run can select, with its default.
- [Results](docs/results.md) — what each objective's standing is, then every measurement with its command, scope and confidence.
- [Roadmap](docs/roadmap.md) — ranked open questions and known limitations.
- [Trace format](docs/trace-format.md) — JSONL schema and reconstruction rules.
- [Viewer](docs/viewer.md) — trace production, replay, live following, and checks.
- [Adding a policy](docs/adding-a-policy.md) — registry signatures, modes, and a runnable extension example.
- [Glossary](docs/glossary.md) — model, policy, objective, and trace terms.
- [Pitfalls](docs/pitfalls.md) — concepts that are easy to conflate and instrumentation that lies.
- [Architecture decisions](docs/decisions/) — current cross-cutting rationale tied to code symbols.
- [Proposals](docs/proposals/) — candidate protocol mechanisms this simulator could evaluate; not specification.
- [Historical records](docs/history/) — retired designs, the failure-mode journal, and adversarial-review transcripts; not current specification.
- [EIP-8288 snapshot](docs/eip-8288.md) — the draft text used as the protocol reference.

## License

MIT. See [LICENSE](LICENSE).

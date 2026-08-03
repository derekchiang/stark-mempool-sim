# starksim

Discrete-time simulation of the EIP-8288 mempool: aggregators fold post-quantum
signatures into STARKs, a builder packs them into blocks, and the sim reports
inclusion-delay metrics.

## Run

```
python -m starksim.sim --trace trace.jsonl
```

See `python -m starksim.sim --help` for parameters (`--r`, `--n`, `--lam-priv`, ...).

## View

```
python -m http.server
```

Then open http://localhost:8000/viewer.html to watch the trace live or replay it.

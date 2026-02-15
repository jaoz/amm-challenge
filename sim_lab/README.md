# Python Simulation Lab

`sim_lab/` is a root-level workspace for deterministic Python-first strategy design before translating logic to Solidity.

## What is included

- `sim_lab/strategy.py`: deterministic adaptive policy with:
  - hidden-price interval filter (`p_low`, `p_high`, `p_step`)
  - probable-arb classification
  - continuous state EWMAs (`lambda_hat`, `arb_hat`, `vol_hat`)
  - discrete one-step fee action search
- `sim_lab/simulator.py`: pure Python two-AMM simulator (submission vs fixed 30 bps normalizer), edge accounting, and trace exports.
- `sim_lab/optimizer.py`: deterministic train/validation/test parameter search with disjoint seed sets and paired-delta promotion.
- `sim_lab/cli.py`: command line entry point.

## Quick start

From repo root:

```bash
python -m sim_lab.cli trace --seed 20260215 --steps 3000 --out-dir sim_lab/out
```

Outputs:

- step trace CSV: external fair price, internal estimate, spot, fees, state
- event trace CSV: per-trade records (arb/retail)
- summary JSON
- price plot PNG (if `matplotlib` is available)

## Deterministic optimization

```bash
python -m sim_lab.cli optimize \
  --iterations 18 \
  --keep-top 5 \
  --train-sims 24 \
  --val-sims 60 \
  --test-sims 120 \
  --train-steps 1500 \
  --val-steps 3000 \
  --test-steps 5000 \
  --seed 20260215 \
  --out-dir sim_lab/out
```

This writes:

- `sim_lab/out/optimization_runs.jsonl`: stage metrics per candidate
- `sim_lab/out/optimization_summary.json`: winner, val/test scores, config, pass flag

## Notes

- The workflow is deterministic for fixed seeds and config.
- The seed protocol uses disjoint `S_train`, `S_val`, and `S_test`.
- You can increase `--*-steps` to `10000` and increase sim counts to mirror full competition settings, but runtime will grow significantly in pure Python.

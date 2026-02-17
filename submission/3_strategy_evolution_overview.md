# Strategy Evolution and Major Pivots (from `Strat/*.sol` timestamps)

## Scope
- Source reviewed: `Strat` (131 Solidity strategy files).
- Timeline window: **2026-02-14 21:10:27** (`my_strategy.sol`) to **2026-02-16 21:12:44** (`baseline_sourced_enriched_simple.sol`), about **48.0 hours**.
- Evaluation evidence used: strategy files plus `scripts/*` optimization runners and `logs/match_runs.jsonl`, `logs/enrich_probe.jsonl`.

## Strategy Evaluation Workflow
1. **Brute-force fee baselines**
- `sweep_fixed.sh`: fixed symmetric fee sweep (`fixed_*.sol`).
- `sweep_asym_fixed.sh`: fixed asymmetric grid (`fixed_asym_*`).
- Purpose: map baseline edge vs fee level before adaptive logic.

2. **Adaptive model iteration**
- Rapid hand-tuned versions (`my_strategy*`, `worldstate_v1..v6`) with arb/retail state inference, volatility tracking, and inventory/fairness tilts.

3. **Automated parameter search**
- `optimize_worldstate_lowfee.py`: low-fee search on worldstate v6 (objective = robust edge + flow terms - fee penalty; target ~35.5 bps).
- `optimize_worldstate_tiltfair_parallel.py`: 4-worker search over fair-tilt + inventory-tilt worldstate variants (objective = avg edge).
- `optimize_powell_direct_v2_no_share_parallel.py`: high-dimensional parallel search on direct-controller strategy with significance-gated promotion.
- `optimize_theo1_local_parallel.py`: local neighborhood search around `theo1.sol` constants (avg-edge objective).

4. **Structured validation/test logging**
- `run_match_and_log.py` writes per-run metrics (mean edge, p10, avg fee, arb/retail volume) into JSONL.

## Chronological Evolution (Major Pivots)
### 1) Initial concept: MM2 tracking + staleness banding
- **2026-02-14 21:10**: `my_strategy.sol`.
- Core idea: infer competitor state, estimate fair proxy (`pHat`), then band fees by stale distance (`|spot-pHat|`).
- Pivot rationale: first attempt to monetize retail when aligned and collapse fees when stale.

### 2) Baseline fee mapping (symmetric then asymmetric)
- **2026-02-14 23:12-23:24**: `fixed_*` and `fixed_asym_*` generation (24 symmetric + 82 asymmetric files).
- Pivot rationale: establish a hard baseline and identify viable fee zones before adding more model complexity.

### 3) Regime/state modeling phase
- **2026-02-14 23:27**: `my_strategy_phase_shift.sol` (volatility-regime base fee + inventory tilt).
- **2026-02-14 23:38 to 2026-02-15 01:22**: `my_strategy_worldstate_v1` -> `v6`.
- Key change: explicit world-state EWMAs (arb probability, volatility, retail pressure), tighter fee clamps, stronger first-touch arb logic.

### 4) Low-fee search and fair-tilt branch
- **2026-02-15 04:33**: `search_v6_lowfee_smoke.best.sol`.
- **2026-02-15 06:59**: `search_v6_lowfee_4h.best.sol`.
- **2026-02-15 04:57**: `my_strategy_worldstate_tiltfair_v1_20260215.sol`.
- **2026-02-15 09:04**: `best_avg_edge_4workers.sol`.
- Pivot rationale: move from broad worldstate tuning to targeted fair-vs-inventory directional skew and parallel optimization.

### 5) Lambda/arrival-rate modeling experiments
- **2026-02-15 11:02**: `mean_edge_lambda_phase_v2.sol`.
- **2026-02-15 12:22**: `mean_edge_lambda_single_stage_v2.sol`.
- Pivot rationale: explicitly model arrival intensity (`lambda`), gap cadence, and step flow as fee drivers.

### 6) AdaptiveBelief architecture
- **2026-02-15 14:16**: `Strategy.sol` (`MeanEdge_AdaptiveBelief_v1`).
- **2026-02-15 16:57**: `Strategy_v2.sol` (`MeanEdge_AdaptiveBelief_v2`).
- Key change: hidden fair-price interval (`p_low/p_high/p_step`), weak/strong updates, and constrained mid+skew control.

### 7) Powell-derived direct controllers
- **2026-02-15 22:13**: `strategy_powell_base_v1.sol`.
- **2026-02-15 23:25**: `strategy_powell_direct_v1.sol`.
- **2026-02-16 00:22**: `strategy_powell_direct_v2_no_share.sol`.
- Pivot rationale: simplify from candidate scoring to deterministic direct control, then reduce share-state coupling.

### 8) Sourced baseline + enriched/theoretical variants sourced from https://github.com/jiayaoqijia/amm-challenge-yq enriched with arbitrage-free shielding and tail harvesting.
- **2026-02-16 20:11**: `baseline_sourced.sol`. 
- **2026-02-16 21:08**: `theo1.sol`.
- **2026-02-16 21:12**: `baseline_sourced_enriched_simple.sol`.
- Key change: sourced model with toxicity/flow shaping, then enrichment with arb classifier + tail harvest + no-arb shield.

## Quantitative Checkpoints (from logged runs)
- Fixed baseline reference:
  - `fixed_30.sol`: mean edge **341.80** (300 sims, fee 30 bps).
  - `fixed_40.sol`: mean edge **353.45** (300 sims, fee 40 bps).
- AdaptiveBelief:
  - `Strategy.sol` v1: mean edge **361.55** (300 sims, fee 44 bps).
  - `Strategy_v2.sol`: best logged mean edge **375.44** (300 sims; higher-fee setting ~71 bps).
- Powell direct:
  - `strategy_powell_direct_v1.sol`: mean edge **421.65** (300 sims).
  - after optimization plateaued at ~**497**
- Final sourced family:
  - `baseline_sourced.sol`: mean edge **532.57** (200 sims, ~37.9 bps).
  - `baseline_sourced_enriched_simple.sol`: best validation mean edge **532.58** (200 sims), repeated near-identical results across many reruns.
  - 1000-sim tests for `baseline_sourced_enriched_simple.sol`: mean edge around **524.62** (stable), essentially on par with `baseline_sourced.sol` test run (**524.63**).
  - `theo1.sol` (100 sims): mean edge **521.79**.

## Interview-Ready Summary of Major Pivots
- Started with **mechanistic stale-price tracking**, then validated fee economics via **large fixed-fee sweeps**.
- Shifted to **state inference** (arb/retail/volatility) and repeatedly tightened bands (`worldstate v1..v6`).
- Split into two optimization streams: **low-fee robust search** and **fair-tilt directional control** with parallel workers.
- Re-architected into **AdaptiveBelief** and then **Powell direct-controller** families for stronger controllability.
- Finalized with **sourced baseline + enriched safeguards**, which delivered the strongest and most stable logged performance.

## Notes
- Metric comparisons are strongest when `n_simulations`, seed set, and stage are aligned; logs include mixed stages (`initial`, `validation`, `test`).
- `Strat/Strategy_gemini.sol` is a zero-byte placeholder and not part of the effective strategy path.

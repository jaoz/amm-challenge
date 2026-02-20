# Simulation Experiment Design Proposal

## Context

This proposal defines how to evaluate strategy improvements under a Monte Carlo simulator where:

- `eval_seed` largely controls world-level randomness (market path/regime).
- `quick_sims`/`refine_sims` produce multiple realizations that are often close in world regime for a given seed.
- The optimizer mutates a high-dimensional parameter set (about 50 constants).
- Naive promotion rules (for example, `candidate mean delta > 0` on a small seed set) have caused many regressions.

Goal: reduce false positives and promote candidates that generalize to leaderboard-style evaluation.

---

## World Design Assumptions

### Two noise sources

1. **Across-world variance** (`eval_seed` changes): dominant source for generalization risk.
2. **Within-world Monte Carlo variance** (`quick_sims`, `refine_sims` at fixed seed): smaller but still relevant.

Implication: increasing `quick_sims` alone is not enough. Robustness must come from diverse `eval_seeds`.

### Selection bias risk

With ~50 mutable parameters, random search will produce many "lucky" candidates on small seed sets.
This creates winner's curse unless promotion uses stronger statistical gates.

---

## Proposed Evaluation Framework

Use paired deltas versus incumbent:

- For each seed `s`, compute `d_s = edge(candidate, s) - edge(incumbent, s)`.
- Make decisions using distribution of `{d_s}`, not just raw candidate edge.

### Required summary statistics

- `mean(d)`
- bootstrap CI for `mean(d)` (95%)
- `LCB95(mean(d))`
- tail metrics: `p10(d)`, `min(d)`

---

## Staged Experiment Design

### Stage A: Search (high throughput)

Purpose: discover candidates, not final selection.

- workers: N (validate on the hardware with smal benchmark)
- sim_workers: 1
- quick_sims: 4-8
- refine_sims: 8-16
- search eval seeds: 4-6

Promotion from Stage A to Stage B should **not** be `mean(d) > 0`.
Use a buffer to reduce noisy handoffs:

- trigger if `mean(d_search) >= +5` **or**
- candidate is in top-K by search score with stable tail (`p10(d_search) >= -10`)

### Stage B: Gate (statistical decision)

Purpose: reject noise winners.

- evaluate candidate + incumbent on fresh paired seeds (disjoint from Stage A)
- gate seed count: 16-24
- sims: same or slightly higher than Stage A (`refine_sims` 12-24)

Promotion rule:

- `LCB95(mean(d_gate)) > 0`
- and `p10(d_gate) >= -X` (choose X based on risk tolerance; start with 10)

### Stage C: Pre-submission validation

Purpose: confidence before leaderboard submission.

- paired seeds: 32-64
- consistent config for candidate and incumbent

Final pass:

- `LCB95(mean(d_final)) > 0`
- no severe tail degradation

---

## Why `mean delta > 0` is insufficient

With small seed counts and many parameters:

- probability of false positive is high
- optimizer can exploit noise pockets
- performance often drops after promotion

Adding CI + tail constraints explicitly controls this failure mode.

---

## Parameter-Space Strategy (~50 parameters)

### Recommended search policy

1. **Phased mutability**:
   - Phase 1: mutate a focused subset (10-15 high-impact params).
   - Phase 2: expand subset only after stable gains.

2. **Mutation sparsity**:
   - low `max_changes` early (2-4) to limit noisy jumps.

3. **Trust-region drift**:
   - strict drift cap around incumbent for search stages.

4. **Ablation checkpointing**:
   - periodically test "remove 1 changed parameter" to confirm contribution.

---

## Compute Budget Guidance

- 70-80%: Stage A search
- 15-25%: Stage B gate
- 5-10%: Stage C final validation

This balances candidate generation with robust decision quality.

---

## Operational Logging Requirements

For each run, persist:

- exact seed sets by stage
- candidate/incumbent per-seed deltas
- CI outputs and pass/fail reasons
- mutable parameter set and mutation hyperparameters

Store in run folder logs so review is reproducible.

---

## Initial Default Proposal

- Stage A: seeds=5, quick=6, refine=12
- Stage B: seeds=20, quick=6, refine=16
- Stage C: seeds=48, quick=8, refine=20
- Gate rule: `LCB95(mean(delta)) > 0` and `p10(delta) >= -10`

Tune thresholds only after collecting 5-10 completed candidate histories.


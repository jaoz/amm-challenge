# Research Timeline — AMM Challenge

## Overview

This document describes the structured research process used to design and refine a mean-edge-optimized AMM strategy for the AMM Challenge.

The objective was strictly:

- Maximize expected average total edge
- Over 1000 simulations
- With 10,000 steps per simulation

Variance and tail metrics were intentionally ignored unless they affected mean edge.

The strategy design was guided by a formal specification (see `Promt for the best strategy.md`) and strict Docker-based evaluation workflow (see `guidelines.md`).

---

# Phase 0 — Baseline & Infrastructure Validation

### Goal
Establish a correct baseline strategy and verify:

- Simulator ordering (arb → retail)
- Fee semantics
- Timestamp visibility rules
- Gas constraints (250k cap)
- Docker execution reproducibility

### Actions

- Implemented minimal base strategy
- Verified behavior via `amm-match validate` and `amm-match run`
- Confirmed callback ordering and censored observation behavior
- Standardized Docker-first workflow per guidelines

### Outcome

Established a reliable starting point for structured experimentation.

---

# Phase 1 — Agent-Oriented Development Framework

### Problem

Early iterations showed that unstructured prompting produced inconsistent agent output and architectural drift.

### Solution

Created structured development artifacts:

- `Promt for the best strategy.md`
- `guidelines.md`
- `.aiignore`

These formalized:

- World model assumptions (GBM, λ distribution, trade sizes)
- Hidden-state estimation requirements
- Timestamp leak handling
- Policy architecture constraints
- Seed discipline
- 4-hour optimization calibration schedule

### Tooling Evaluation

Tested:
- Codex
- Gemini

Codex showed:
- Better long-context reasoning
- More consistent Solidity output
- Stronger adherence to structural constraints

Standardized on Codex for development.

---

# Phase 2 — Optimization-Driven Plateau

### Approach

Ran parameter optimization:

- Parallel workers (4–8)
- Staged simulation budgets (quick → refine → 1000 sims)
- Promotion only on statistically significant improvement

### Result

Reached ~522–523 mean edge.

### Diagnosis

Performance plateau suggested:
- Structural model weakness
- Hidden-state misestimation
- Arb bleed
- Drift under retail distortion

Tuning alone was insufficient.

---

# Phase 3 — Python Simulator Reconstruction

### Motivation

Solidity-only debugging obscured hidden state behavior.

Rebuilt Python simulation environment to:

- Observe true fair price
- Inspect pool drift
- Trace arbitrage boundaries
- Inspect timestamp gaps
- Visualize internal estimate vs truth

Generated traces such as:

`trace_ts_20260215_185928_seed_20260215_steps_1500_prices.png`

### Key Findings

1. Internal fair price drifted during quiet periods
2. Retail trades temporarily distorted spot
3. Arbitrage trades revealed boundary-implied prices, not exact fair price
4. Misclassification caused edge leakage

This was the turning point.

---

# Phase 4 — Two-Layer Protection Model

## Layer 1 — Arb Boundary Anchoring

Introduced:

- Side-specific fee inversion
- Strong vs weak updates
- Belief interval `[p_low, p_high]`
- Per-step anchoring rule
- Single-anchor per timestamp

Principle:

> Arbitrage reveals boundary-implied fair price, not truth.
> Only anchor on first trade in step if classified as high-confidence arb.

This reduced arb bleed significantly.

**Result: Large jump in mean edge.**

---

## Layer 2 — Retail Distortion Dampening

Retail trades distort spot temporarily.

Added:

- Weak update bounds
- Epsilon slack
- Diffusion slack between steps
- Interval reopening logic on collapse

This stabilized belief state.

---

# Phase 5 — Timestamp & Censored Observation Refinement

Observability constraints:

- Callback only when our AMM trades
- Zero-trade steps invisible

Improvements:

- `delta_t`-based λ inference
- `stepTrades` tracking
- Arb classification using ordering + size
- First-trade-only anchor rule
- Same-timestamp forced retail classification

This improved:

- `lambda_hat` stability
- Toxicity detection
- Fee adaptation timing

---

# Phase 6 — Share Sensitivity & Routing Modeling

Retail routing depends on:

- Fee differential
- Reserves
- Fixed 30bps competitor

Introduced:

- `s_buy_base`, `s_sell_base`
- `kappa_buy`, `kappa_sell`
- Share sensitivity updates via fee perturbation
- Route competitiveness proxy

Moved from static fee heuristics to adaptive routing inference.

---

# Phase 7 — Python → Solidity Compression

Python implementation included:

- Log blending
- Smooth sigmoid curves
- Continuous state transitions

However, gas limit (250k) forced simplification.

Removed:

- Expensive transcendental approximations
- Multi-stage smoothing
- Heavy belief math

Replaced with:

- Piecewise approximations
- 3x3 candidate grid search per callback
- Bounded per-step fee jump
- Simplified scoring proxy

Maintained structure, reduced gas.

---

# Phase 8 — Controlled Monte Carlo Optimization

To avoid overfitting:

- Fixed `S_train`
- Independent `S_val`
- Independent `S_test`
- Paired seed difference testing
- Promotion only if:
  - `mean_delta > z_alpha * se`
  - `mean_delta > min_effect`

Final evaluation done on fresh 1000+ sims.

---

# Major  Insights

1. Retail intensity λ is dominant edge driver.
2. Fair price inference under censored observation is the core problem.
3. Arb reveals boundary-implied price.
4. Retail must be dampened in estimation.
5. Overfitting via optimizer noise is real.
6. Gas constraints force architectural compression.

---

# Engineering Trade-Off

Python sim:
- More expressive
- Cleaner state transitions
- Better interpretability
- Word and strategy detailed debug

Solidity model:
- Compressed
- Gas-constrained
- Deterministic
- Approximate but structurally consistent

Edge gains came from structural improvements, not brute-force tuning.

---

# Conclusion

This project evolved from:

Baseline tuning  
→ Structural debugging  
→ Hidden-state modeling  
→ Arb-boundary inference  
→ Censored λ estimation  
→ Route sensitivity modeling  
→ Gas-constrained control approximation  

The final strategy reflects:

- Partial observability reasoning
- Adversarial routing modeling
- Latent-state adaptive control
- Monte Carlo experiment discipline
- Engineering trade-off awareness

The research focus was on structural correctness and belief-state control rather than leaderboard brute-force optimization.
# AMM Challenge Overview

## Objective
The challenge objective is to maximize expected mean edge in simulation.

Evaluation setup:
- 10,000 steps per simulation
- 1,000+ simulations for final checks
- primary metric: mean edge

## Problem
At each callback, the strategy sets `(bidFee, askFee)` under partial observability:
- callbacks occur only when our AMM is hit
- zero-trade steps are unobserved
- first trade in a timestamp can be arbitrage; later same-timestamp trades are retail

This creates a control problem where fees must balance:
- retail spread capture
- arbitrage toxicity and stale-price risk

## Approach Summary
The final strategy family uses online belief-state control:
- infer latent state (`pHat`, flow intensity, toxicity, directional pressure)
- classify probable arbitrage on first-touch transitions
- update fees asymmetrically using stale direction and inventory pressure
- apply no-arb fee floors when imbalance creates one-sided exposure
- exploit world constraints: 
  - low volatility variance,
  - low trade size variance,
  - constant world parameters in each simulation,
  - high retail trade flow variance
Core principle:
maximize retail capture while tightly limiting toxic flow losses.

## Development Method
Work was agent-assisted and experiment-driven:
- rapid strategy iteration with Codex
- parallel parameter search with quick/refine evaluation loops
- structured JSONL logging for comparison across seed sets
- gas-aware Solidity simplification for deployment constraints

## Included Artifacts
This submission folder includes:
- `Strategy.sol` (submitted strategy)
- `2_research_timeline.md` (research process)
- `3_strategy_evolution_overview.md` (timestamp-based pivots)
- `chats history/` (full chat logs)
- `chats_history_index.md` (chat log index)
- `trace_ts_20260215_185928_seed_20260215_steps_1500_prices.png` (example diagnostic trace)

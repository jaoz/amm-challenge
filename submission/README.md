# Submission README

## What This Submission Covers
This package is my delivery for the AMM take-home:
- completed strategy development for https://ammchallenge.com/
- used coding agents (primarily Codex; one early Gemini pass)
- included chat history so reviewer can evaluate how I steered the agent

## Folder Contents
- `Strategy.sol`: submitted Solidity strategy contract.
- `1_overview.md`: concise problem framing and method summary.
- `2_research_timeline.md`: end-to-end research and iteration process.
- `3_strategy_evolution_overview.md`: timestamp-based strategy pivot analysis from `Strat/*.sol`.
- `trace_ts_20260215_185928_seed_20260215_steps_1500_prices.png`: diagnostic price/belief trace sample.
- `chats history/`: raw chat exports.
- `4_chats_history_index.md`: index for navigating chat logs.

## How To Review Quickly
1. Read `1_overview.md`.
2. Read `3_strategy_evolution_overview.md` for concrete pivots and evaluations.
3. Open `chats_history_index.md`, then inspect the mapped chat logs in `chats history/`.
4. Review `Strategy.sol` last.


## Notes For Reviewers
- The development process combined market-structure reasoning with adjacent engineering work (simulation tooling, optimization scripts, and experiment tracking).
- Chat logs are intentionally unfiltered so the steering process is auditable. Several chats are missing due to IDE crash/bugs long.
- link to my profile https://www.ammchallenge.com/user/defi_theo
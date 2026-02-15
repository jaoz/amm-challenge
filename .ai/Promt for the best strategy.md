# Build an AMM Strategy From Scratch (Max Mean Edge Only)

You are writing a new Solidity strategy for the AMM challenge from scratch.

Create a new file in `Strat/` with contract name `Strategy`.
Do not reuse prior strategy logic by default.
Before proposing code, read and follow `.ai/guidelines.md` for any additional local rules.

## Objective

Maximize only:
- expected average total edge across runs

Evaluation target:
- 1000 simulations
- 10,000 steps per simulation

Do NOT optimize for:
- variance
- tail metrics
- target fee level

If mean edge increases, accept the change.

## Leader Benchmark Target

Use this reference from the current leader snapshot:
- `mean_edge_ref = 528.71`
- `avg_fee_ref = 37.0 bps`

Hard task target:
- produce a strategy that beats `528.0` mean edge on final test (`S_test`)

Stretch target:
- exceed `530.0` mean edge on final test

Fee note for calibration (not the optimization objective):
- use `~37 bps` as an initialization prior / search center
- practical sweep center band: `34-40 bps`
- do not prefer higher/lower fee unless it improves mean edge

## Known World Model

- Fair price follows GBM per step.
- `sigma ~ U[0.088%, 0.101%]` per run.
- Retail intensity `lambda ~ U[0.6, 1.0]` per run.
- Retail mean size in Y terms `~ U[19, 21]` per run.
- Hyperparameters are fixed within run, resampled between runs.
- Two AMMs only: ours and fixed 30 bps normalizer.
- Initial reserves for both: `x=100`, `y=10000`.

## Observability and Timestamp

- `trade.timestamp` is observable and equals simulation step index.
- `afterSwap` is called only when our AMM is traded.
- Therefore observations are censored.
- Multiple trades may share the same timestamp.

Use:
- `delta_t = timestamp - last_timestamp` as a no-trade gap signal.
- `stepTrades` to index multiple trades inside a step.

## Continuous Adaptive Control (No Phases)

Use one continuous controller.
No discovery/exploitation stage switching.

State vector should include:
- `lambda_hat` (primary)
- `arb_hat` toxicity
- hidden-price belief (`p_low`, `p_high`, `p_step`)
- inventory imbalance
- optional low-weight `vol_hat`

Control:
- choose `bidFee`, `askFee` each callback
- bounded per-update change
- bounded asymmetry

## Route-Dependent Objective Math (Robust Observable Version)

Retail routing depends on both fees and reserves, but competitor reserves are not directly observed in callbacks.
Use a robust reduced model based on latent route competitiveness and online sensitivities.

Maintain latent route states:
- `r_buy_hat`: effective competitiveness for buy flow (trader buys X)
- `r_sell_hat`: effective competitiveness for sell flow (trader sells X)
- `s_buy_base`, `s_sell_base`: baseline captured share by side
- `kappa_buy`, `kappa_sell`: local sensitivity of captured share to fee deltas

Reference from exact router math (for design intuition only):
- buy side uses `A_i = sqrt(x_i * (1-askFee_i) * y_i)` and split by ratio `A_1/A_2`
- sell side uses `B_i = sqrt(y_i * (1-bidFee_i) * x_i)` and split by ratio `B_1/B_2`

Online observable approximation:
- `s_buy_hat = clamp(s_buy_base + kappa_buy * (askFee_norm - askFee), 0, 1)`
- `s_sell_hat = clamp(s_sell_base + kappa_sell * (bidFee_norm - bidFee), 0, 1)`
- update `s_*_base` and `kappa_*` with EWMA from realized side volumes after small fee perturbations

Use these share estimates in one-step edge approximation:
- `E[edge_t | state, fees] = E_retail_t(state, s_buy_hat, s_sell_hat, fees) - E_arb_t(state, fees)`

Avoid static fee-only sigmoid share curves; keep sensitivities state-dependent and continuously updated.

## Hidden True Price Estimation (Best Practical Filter)

Fair price is hidden and constant inside a step.
Multiple retail trades in the same step can bias spot, so estimate per-step and lock it.

Maintain:
- `p_low`, `p_high` as belief interval
- `p_step` as point estimate
- `anchored_this_step` boolean

Per trade with current spot `spot = reserveY / reserveX`:

No-arb bounds with asymmetric fees:
- `spot_lower = p_true * (1 - askFee)`
- `spot_upper = p_true / (1 - bidFee)`
- equivalent:
- `p_true in [ spot * (1 - bidFee), spot / (1 - askFee) ]`

Arb boundary inversion (if classified probable arb):
- if `trade.isBuy = true` (arb sold X to AMM):
- `p_anchor = spot * (1 - bidFee_used)`
- if `trade.isBuy = false` (arb bought X from AMM):
- `p_anchor = spot / (1 - askFee_used)`

Important:
- `p_anchor` is boundary-implied, not exact true price.
- Fee skew shifts boundary; always use side-specific fee used by that trade.
- fee semantics:
- `bidFee_used`/`askFee_used` are the exact fees active before this executed trade (the quote that trade actually faced)
- `bidFee_ref`/`askFee_ref` are latest known pre-trade quote values used for conservative bounds when trade type is not high-confidence arb

Update rule:

1. On new timestamp:
- freeze prior step: `p_prev = p_step`
- carry interval through diffusion slack:
- `p_low <- p_low * exp(-k_sigma)`
- `p_high <- p_high * exp(+k_sigma)`
- choose `k_sigma = c_sigma * sigma_hat` with small `c_sigma` (for this challenge usually near 1-3)
- set `anchored_this_step = false`

2. On each callback:
- if trade is high-confidence arb:
- apply tight intersection bound:
- `l = spot * (1 - bidFee_used)`
- `u = spot / (1 - askFee_used)`
- `p_low = max(p_low, l)`, `p_high = min(p_high, u)`
- else (likely retail/noisy):
- apply weak update only (do not over-tighten on retail-distorted spot):
- `l = spot * (1 - bidFee_ref)`
- `u = spot / (1 - askFee_ref)`
- `p_low = max(p_low, l * (1 - eps_weak))`
- `p_high = min(p_high, u * (1 + eps_weak))`
- if interval collapses (`p_low > p_high`), reopen by epsilon buffer around geometric midpoint

3. If first high-confidence arb in step:
- anchor to correct boundary with confidence `w_arb`:
- `log(p_step) = (1 - w_arb) * log(sqrt(p_low * p_high)) + w_arb * log(p_anchor)`
- set `anchored_this_step = true`

4. Additional same-step trades:
- do not re-anchor
- keep `p_step` fixed or tiny smoothing toward `sqrt(p_low * p_high)`

5. End-of-step effective estimate:
- default `p_step = sqrt(p_low * p_high)` if no arb anchor

Use `p_step` for:
- stale/toxicity estimation
- inventory value
- side tilt decisions

## Policy Optimization Under Uncertainty

Model as adaptive control with belief state `B_t`.
Bellman form:
- `V_t(B_t, S_t) = max_f E[ g(B_t, S_t, f, W_{t+1}) + V_{t+1}(B_{t+1}, S_{t+1}) ]`

Practical approximation:
- evaluate a discrete action set of fee pairs each callback
- one-step lookahead with value proxy:
- `f_t = argmax_{f in F} (E[edge_next | B_t, f] + V_hat(post_state(B_t, f)))`

Where:
- `E[edge_next | ...]` uses route-dependent split formulas above
- `V_hat` penalizes toxic stale exposure and extreme inventory drift
- `V_hat` must use explicit features:
- `phi = [arb_hat, stale_mag, inv_abs, inv_signed, lambda_hat, spread_to_norm, fee_jump]`
- fit `V_hat(phi)` offline on rollout data (linear/quadratic model is enough), then freeze for policy search

## Candidate Search and Evaluation

Because this is Monte Carlo optimization on a random generator, avoid optimizer bias:
- compare candidates on independent seed batches
- final ranking by fresh large batch mean edge

Required seed protocol:

1. Train/tune phase (fixed seeds):
- use one fixed training seed set `S_train`
- do all rapid iterations only on `S_train`
- typical size: 80-200 simulations

2. Validation phase (different seeds):
- evaluate promising candidates on independent `S_val` (`S_val` disjoint from `S_train`)
- typical size: 300-1000 simulations
- model selection is based on validation mean edge

3. Test phase (different seeds):
- run final chosen candidate on independent `S_test` (`S_test` disjoint from both `S_train`, `S_val`)
- typical size: 1000-3000 simulations
- do not tune using `S_test`; use it only for final unbiased estimate

Selection and reporting rule:
- use paired seeds on `S_val` and compare deltas:
- `d_i = edge_A(i) - edge_B(i)` for identical seed `i in S_val`
- `mean_d = avg(d_i)`, `se_d = std(d_i)/sqrt(n_val)`
- candidate A beats B only if `mean_d > z_alpha * se_d` and `mean_d > min_effect`
- recommended: `z_alpha = 1.64` (one-sided), `min_effect = 1.0` edge
- report both validation and test mean edge for final candidate
- final success flag:
- `PASS` if `mean_edge_test > 528.0`
- `STRETCH PASS` if `mean_edge_test > 530.0`

## Execution Environment (Docker-First, No Local Rust Build)

Do not require Rust toolchain on host machine.
Run simulations via Docker or via Python scripts that execute inside the Docker container.

Preferred workflow:

1. Build image once:
- `docker build -t amm-challenge .`

2. Run commands in container with repo mounted:
- `docker run --rm -v ${PWD}:/app -w /app amm-challenge python scripts/run_match_and_log.py Strat/<strategy>.sol --simulations <N>`

3. All optimization/evaluation scripts must be runnable in this container path setup.

## Run Logging Requirements (All Attempts)

Log every attempt to a structured file (`.csv` or `.jsonl`) with at least:
- `run_id`
- `start_ts`, `end_ts`
- `stage` (`initial`, `train`, `validation`, `test`)
- `strategy_path`, `strategy_name`
- `params_json` (all tunable parameters used for that run)
- `seed_set_id` (`S_train`, `S_val`, `S_test`)
- `n_simulations`, `n_steps`

Required metrics per run:
- `mean_edge`
- `p10_edge`
- `p90_edge`
- `max_edge`
- `avg_fee_bps`
- `arb_volume_y`
- `retail_volume_y`

Also log counts if available:
- `arb_trade_count`
- `retail_trade_count`

If trade counts are not exposed by current API, extend the simulator/result schema to expose them, or log explicit placeholders and document the gap.

Use `scripts/run_match_and_log.py` as base, but extend it (or add a companion script) to produce this full metric set.

## Workflow Separation (Mandatory)

Use two distinct phases:

1. Initial strategy phase (no high-dimensional search)
- build first coherent policy
- run smoke checks (`10-50` sims)
- run one baseline evaluation (`200-400` sims)
- freeze architecture after this phase

2. Optimization phase (only after strategy is ready)
- tune numeric parameters only
- no major architecture rewrites during optimization
- run train/validation/test seed protocol from above

## 4-Hour Calibration Policy (Laptop Budget)

Assume fixed wall-clock budget of 4 hours.
Use this schedule:

1. `0:00-0:25` setup + sanity
- container check, logger check, one smoke run

2. `0:25-1:00` initial strategy quality gate
- 3-6 architecture variants, `60-120` sims each on `S_train`
- choose one architecture

3. `1:00-3:10` high-dimensional tuning
- iterative search on `S_train` (`80-150` sims per candidate)
- promote top candidates to `S_val` (`300-600` sims)
- keep only candidates passing paired-delta threshold

4. `3:10-3:45` strong validation
- top 2-3 candidates on larger `S_val` (`800-1200` sims)

5. `3:45-4:00` final test
- final winner on `S_test` (`1000+` sims if time allows)
- output final report with train/val/test metrics

Priority under time pressure:
- spend more compute on validation of fewer candidates, not shallow evaluation of many.

## Deliverables

Return:

1. Full Solidity strategy source.
2. Explanation of belief state, hidden-price filter, and control policy.
3. Parameter table.
4. Exact commands for local validation and 1000+ sim evaluation.
5. Why this should push mean edge above current baseline.

## Constraints

- Respect challenge constraints (gas, 32 slots, no forbidden ops).
- Deterministic logic only.
- Safe fixed-point math and fee clamps.
- No external dependencies.

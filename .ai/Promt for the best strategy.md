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
- do not prefer higher/lower fee unless it improves mean edge
- opening quote condition: start at symmetric `29 bps` (`bidFee=askFee=29`) to be slightly better than the 30 bps normalizer and capture early flow for state estimation

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

## Timestamp Ordering Leak (Use Explicitly)

The simulator trade ordering per step is:
1. fair price update
2. arbitrage on each AMM (at most one arb trade on our AMM for that timestamp)
3. retail routing/execution (zero or more trades)

Implication for our callback stream:
- first observed trade at a new `timestamp` can be arb or retail
- second and later observed trades with same `timestamp` are always retail

Required classification rule:
- if `stepTrades == 1`: run arb-vs-retail classifier with boundary checks
- if `stepTrades >= 2`: classify as retail (no arb), skip arb-anchor updates

Use this leak in:
- hidden price anchoring (only allow arb anchor on `stepTrades == 1`)
- `lambda_hat` updates (same-timestamp additional trades are strong retail signal)
- toxicity/staleness logic (do not mark later same-step trades as arb)

Additional opening condition:
- keep opening quote at `29 bps` until the first observed trade callback, then switch to normal adaptive control

## Trade Size Signal for Arb/Retail Classification

Use trade size in Y terms as an additional classifier feature on `stepTrades == 1`.

Retail prior (known world model):
- `amountY_retail ~ LogNormal(mu_r, sigma_r)` with `sigma_r = 1.2`
- `mean_retail ~ 20` (run-specific in `[19, 21]`), so
- `mu_r = log(mean_retail_hat) - 0.5 * sigma_r^2`

Use full-distribution tail score (not only small cutoff):
- `z = (log(amountY) - mu_r) / sigma_r`
- `F = Phi(z)` (standard normal CDF)
- `tailProb = 2 * min(F, 1 - F)`  (two-sided tail probability)
- `arbSizeScore = 1 - tailProb`

Interpretation:
- both very small and very large trades can be arb-like (high `arbSizeScore`)
- mid-sized trades near the retail mode are more retail-like

Required classifier integration:
- keep boundary/direction checks as primary conditions
- keep a small-trade heuristic as an optional extra feature
- add two-sided tail-based boost using `arbSizeScore` (or `tailProb` threshold + strength)
- relax anchor/move thresholds more when tail signal is stronger
- keep `stepTrades >= 2` forced retail (size signal must not override timestamp leak rule)

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

Large-imbalance protection priority:
- after a retail fill can leave pool imbalanced vs `p_step`; this creates immediate one-sided arb exposure
- compute required no-arb side fee from current spot:
- `bid_required = max(0, 1 - p_step / spot)`, `ask_required = max(0, 1 - spot / p_step)`
- cap required no-arb fee at `10%` before applying any buffer
- when required side fee is small (up to about `35 bps`), normal inventory tilt logic can dominate
- when required side fee exceeds `~35 bps`, no-arb protection must override inventory-tilt limits:
- enforce side fee floor `required + 35 bps`
- allow that exposed side to jump beyond normal per-step fee-change cap
- allow temporary asymmetry beyond normal asymmetry cap if needed to remove arb exposure
- non-exposed side can be `0 bps` only as an optional candidate (never forced), and only when that side's flow direction compensates current internal inventory imbalance

## Route-Dependent Objective Math (Robust Observable Version)

Retail routing depends on both fees and reserves, but competitor reserves are not directly observed in callbacks.
Use an explicit latent-state model of the normalizer AMM and update it online from observed fills.

Maintain latent normalizer state:
- `norm_x_hat`, `norm_y_hat` (normalizer reserve estimates)
- `norm_k` (invariant, approximately constant)
- optional low-weight share/sensitivity EWMAs for diagnostics (`s_*`, `kappa_*`)

Use exact 2-AMM router equations (same as simulator):
- buy side uses `A_i = sqrt(x_i * (1-askFee_i) * y_i)` and split by ratio `A_1/A_2`
- sell side uses `B_i = sqrt(y_i * (1-bidFee_i) * x_i)` and split by ratio `B_1/B_2`

State update from observed submission retail fills:
- infer full order size and counterpart normalizer fill by inverting split equations
- if inversion is non-interior, allow clamped one-sided routing (`submission=100%`, `normalizer=0%`)
- apply inferred normalizer trade to `norm_x_hat`,`norm_y_hat` with smoothing `alpha_norm_state`
- on new timestamp, project normalizer toward no-arb boundary around `p_step` with low-confidence arb projection (`alpha_norm_arb`)

One-step expected edge (per callback):
- evaluate candidate `(bidFee, askFee)` pairs
- for buy flow, compute split using `(our reserves, norm estimates, ask fees)` and edge from exact AMM quote
- for sell flow, compute split using `(our reserves, norm estimates, bid fees)` and edge from exact AMM quote
- combine sides with `buyProb_hat` and expected order count `lambda_hat`
- subtract arb/toxicity and inventory penalties

Avoid static fee-only sigmoid share curves; fee choice should come from split math + latent normalizer state at current step.

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
- `E[edge_next | ...]` must be computed from exact split formulas using `(our reserves, norm_x_hat, norm_y_hat)` rather than only fee-distance heuristics

Where:
- `E[edge_next | ...]` uses route-dependent split formulas + latent normalizer reserve estimates
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

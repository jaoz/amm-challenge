## Research journal (Cursor phase)

### 2026-02-18 — Multi-seed + holdout guardrails baseline
- **Run**: `Cursor/runs/theo1_local_2h_ms_guard_exp_w7_q6r16_s0275_mc4_t42000-42011_h43000-43005_20260218T101922Z/`
- **Base strategy**: `.../base.sol` (copied from champion snapshot)
- **Train seeds**: 42000..42011 (12)
- **Holdout seeds**: 43000..43005 (6)
- **Eval seed diversity**: per seed, MatchRunner uses `seed = seed_offset + i` and we set `seed_offset = eval_seed * 1_000_000`.

#### Baseline metrics (from worker base refine, 16 sims/seed)
- **Train mean edge**: ~525.95
- **Holdout mean edge**: ~514.69
- **Fee**: ~39.6 bps
- **Retail vol (Y)**: ~74.3k
- **Arb vol (Y)**: ~21.0k

#### What happened
- Many refined candidates were **very close** to baseline (delta edge ~0.00–0.02) and were rejected.
- Root cause: `--min-delta-edge 1.0` is **too strict** for this sim budget; it blocks essentially all plausible improvements.

#### Notes / hypotheses
- Observed that quick scores clustered around ~517 while refined baseline was ~526. This can happen if **the first small block of deterministic seeds** (seed_offset + i for small i) is not representative. Not necessarily a bug, but it makes quick-stage ranking noisy/biased at very low `--quick-sims`.
- Important detail: `MatchRunner` always sets per-sim `cfg.seed = seed_offset + i`, so `SimulationConfig.seed` passed into `build_base_config(seed=...)` does **not** affect diversity; only `seed_offset` matters.
- “Edge drop” compared to leaderboard screenshots is expected when you change the seed window (e.g. optimizer uses `eval_seed*1_000_000`, while proxy/leaderboard checks often use `seed_offset=0/1000/2000`). Those are different scenario sets, so absolute mean edge shifts.

#### Next step (planned; not executed here)
- Re-run with **lower** `--min-delta-edge` (e.g. 0.05–0.10) so improvements can be promoted.
- Consider increasing `--quick-sims` modestly or using a different quick seed window to reduce “first-N” bias.

### 2026-02-18 — Structural Fix Run (v2 fixed, 1h Stage A) ✅ PASSED ALL GATES

- **Run path**: `Cursor/runs/20260218T205008Z_theo1_staged_pipeline/`
- **Base strategy**: `Cursor/strategies/champions/theo1_v2_fixed_20260218.sol`
- **Promoted**: `Cursor/strategies/champions/theo1_v2_promoted_20260218.sol`
- **Key params**: `--a-hours 1`, 20 workers, mutable=22 (added 6 new fix constants)

#### Structural fixes applied (in order):
1. **Fix 1 — Opening quote 30 bps−5**: `OPENING_QUOTE=2999999999999995` (was 5 bps → bled arb edge at init)
2. **Fix 2 — sizeHat unconditional**: Added `SIZE_SMALL_DECAY=0.98` else-branch for sub-threshold trades
3. **Fix 5 — Two-sided arb size scorer**: `ARB_TR_MIN_SMALL=5bps`, relax floor for tiny (<0.25×mode) and large (>3×mode) prints
4. **Fix 6 — pHat gap-aware**: `GAP_PHAT_ALPHA_BOOST=0.03/step`, `GAP_GATE_PER_STEP=0.25/step` after no-trade gaps
5. **Fix 3 — toxEma proper EMA**: `TOX_BLEND_DECAY` 0.051→0.78 (now mutable); optimizer settled at **0.740**
6. **Fix 4 — lambdaHat**: Verified correct (elapsedRaw denominator handles gaps); no structural change
7. **Fix 7 — Stale/dir coeffs**: `STALE_DIR_COEF`, `STALE_ATTRACT_FRAC` added to DEFAULT_MUTABLE

#### Stage outcomes:
| Stage | Metric | Value | Pass? |
|---|---|---|---|
| A trigger | mean_delta / p10_delta | +1.08 / +0.76 | ✅ |
| B gate | lcb95 / p10 | +1.03 / +0.88 | ✅ |
| C final | lcb95 / p10 / min | **+1.037 / +0.895 / +0.742** | ✅ |

- Previous champion run Stage C: lcb95=−0.016, p10=−0.071, min=−0.675 → FAILED
- This run: ALL 48 seeds positive; seed 45027 (prev −0.675 outlier) now **+0.944**

#### Optimizer findings:
- `TOX_BLEND_DECAY` settled at 0.740 (not 0.78 — wants slightly faster tox response)
- `TRADE_TOX_BOOST` trimmed slightly (290 vs 297 original)
- Structural fixes account for the bulk of the ~+1.08 mean delta vs champion

#### Recommendation:
- Submit `theo1_v2_promoted_20260218.sol` as new champion
- Next search: increase `--a-hours 2` from this new base to search deeper
- Consider tightening `ARB_SIZE_HIGH_MULT` / `ARB_SIZE_LOW_FRAC` (add to mutable)
- The `STALE_ATTRACT_FRAC` signal the optimizer may want to push (watch next run)

---

### 2026-02-19 — GCP 8h Stage A/B/C run (48 workers, n2-highcpu-48) ✅ PASSED ALL GATES

- **Run path**: `Cursor/runs/20260219T034902Z_theo1_staged_pipeline/`  (local copy: `Strat/gcp_20260219T034902Z_theo1_staged_pipeline/`)
- **Base strategy**: `Cursor/strategies/champions/theo1_v2_promoted_20260218.sol` (lcb95=+1.037)
- **Promoted**: `Strat/gcp_20260219T034902Z_theo1_staged_pipeline/promoted_best.sol`
- **Key params**: `--a-hours 8`, 48 workers, `--a-max-safe-workers 56`, `--a-sim-workers 1`
- **Note**: First attempt used 60 workers (caused kernel network starvation ~2.5h in, VM hung). Restarted with 48 workers — load average held exactly 48.x for 8 hours, no instability.

#### Stage outcomes:
| Stage | n_seeds | mean_delta | lcb95_mean_delta | p10_delta | min_delta | Pass |
|---|---|---|---|---|---|---|
| A trigger | 5 | +2.138 | +1.997 | +1.866 | +1.866 | ✅ |
| B gate | 20 | +2.343 | +2.209 | +1.729 | +1.689 | ✅ |
| C final | 48 | +2.285 | **+2.203** | +1.984 | +1.473 | ✅ |

- **Previous champion**: lcb95=+1.037 → **New champion: lcb95=+2.203** (2.1× better)
- ALL 48 Stage C seeds positive (min_delta=+1.473)

#### Parameter changes found (D15 — 15 params changed from base):

Top 3 movers (by % change):
| Parameter | Base | New | Δ% | Interpretation |
|---|---|---|---|---|
| **TOX_BLEND_DECAY** | 0.7401 | 0.6390 | **−13.7%** | toxEma responds FASTER to current toxicity |
| **LAMBDA_COEF** | 1342 | 1249 | −6.9% | Less fee loading from trade arrival rate |
| **PHAT_ALPHA** | 0.2415 | 0.2259 | −6.5% | pHat updates more slowly (conservative) |
| SHIELD_TRIGGER | 3528 | 3324 | −5.8% | Shield kicks in at lower threshold |
| STALE_ATTRACT_FRAC | 1.124 | 1.091 | −3.0% | Less aggressive attraction discount |
| TOX_CUBIC_COEF | 0.811 | 0.830 | +2.4% | More cubic toxicity term |
| MIN_GATE | 30764 | 30023 | −2.4% | Smaller minimum gate |
| STALE_DIR_COEF | 0.695 | 0.705 | +1.5% | More directional protection |

Remaining 7 params: <2% drift (noise-level adjustments).

#### Optimizer findings:
- Core insight: **faster toxEma + slower pHat tracking** is synergistic — detect toxic regimes faster but don't over-react on individual price signals
- The search found this via 15 simultaneous small-step coordinate moves (the optimizer's stochastic hill-climb)
- Improvement rate decelerated over 8h: +0.76 avg_edge/hr at 2h → +0.12/hr at 6.5h — suggests local optimum approached

#### Recommendations for next run:
1. Use `promoted_best.sol` as new base
2. **Add to mutable list**: `DIR_DECAY`, `SIZE_BLEND_DECAY`, `TOX_DECAY`, `ARB_RET_MIN`, `ARB_TOX_MIN`, `GAP_GATE_PER_STEP`, `TAIL_SLOPE_PROTECT`, `TAIL_SLOPE_ATTRACT` (currently hard-coded but high-value)
3. Consider `--step-pct 0.07` (slightly larger steps to escape current local optimum)
4. Consider `--max-changes 3` (allow 3-param simultaneous moves for diagonal exploration)
5. Keep 48 workers — confirmed stable on n2-highcpu-48; do NOT use 60+

---

### 2026-02-19 — PLANNED: 2-VM Parallel Run (target ~5.5h wall-clock)

- **Design goal**: close the +15 edge-point gap to next leaderboard tier (+515 → +530 Avg Edge per Sim)
- **Architecture**: amm-opt-1 + amm-opt-2 (disk clone) running simultaneously
- **VM2 creation**: snapshot amm-opt-1 disk → create amm-opt-2 from snapshot (preserves venv + Rust)

#### Strategy rationale

Previous 8h run decelerated sharply: +0.76 avg_edge/hr at 2h → +0.12/hr at 6.5h.
Root cause: 48 workers sharing same random walk converge to same local optimum.
Fix: two VMs with different step sizes run diverse searches in parallel.

| | VM1 (exploitation) | VM2 (exploration) |
|--|--|--|
| `step_pct` | 0.05 | **0.08** (larger jumps) |
| `max_changes` | 3 | **4** (diagonal moves) |
| `hours` | 5.0 | 5.0 |
| Role | Deep search near v3 optimum | Escape to new basins |

#### New parameters added (8 unexplored dimensions, total 30):
`DIR_DECAY`, `SIZE_BLEND_DECAY`, `TOX_DECAY` — EMA lifetime parameters
`ARB_RET_MIN`, `ARB_TOX_MIN` — arb classifier sensitivity
`GAP_GATE_PER_STEP` — gate widening speed after gaps
`TAIL_SLOPE_PROTECT`, `TAIL_SLOPE_ATTRACT` — asymmetric tail compression (currently both locked at 0.799)

#### Expected outcomes:
- Wall-clock: ~5.5h (vs 8.5h single VM) — 35% faster
- Compute: 2× n2-highcpu-48 × 5.5h ≈ $12.50 (vs $9.70 for single VM)
- If new params (esp. TAIL_SLOPE asymmetry, TOX_DECAY) are high-value: lcb95 > 2.5 plausible
- If new params are near-neutral: expect +2.3–2.4 (same trajectory as previous run)
- Pick max(VM1_lcb95, VM2_lcb95) → save as theo1_v4_promoted_YYYYMMDD.sol

---

### 2026-02-18 — Cursor Stage A/B/C run (1h Stage A)
- **Run path**: `Cursor/runs/20260218T160633Z_theo1_staged_pipeline/`
- **Key params**: `--a-hours 1`, Stage A workers `20`, max-safe-workers `24`, Stage A sim_workers `1`, spawn_stagger_seconds `0.5` (Stage B/C defaults unchanged).
- **Stage outcomes**:
  - Stage A trigger: **PASS** (`mean_delta=0.0823`, `p10_delta=0.0281`)
  - Stage B gate: **PASS** (`lcb95_mean_delta=0.0187`, `p10_delta=-0.1190`)
  - Stage C final gate: **FAIL** (`lcb95_mean_delta=-0.0157`, `p10_delta=-0.0711`)
  - Final status: `stopped_stage_c_final_failed` (`promoted_path=null`)
- **Recommendation**: Keep the same pipeline flow, but bias next search toward reducing left-tail downside (focus on seeds with largest negative Stage C deltas, especially the outlier around seed `45027`) before re-running Stage B/C gate checks.


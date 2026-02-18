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

### 2026-02-18 — Cursor Stage A/B/C run (1h Stage A)
- **Run path**: `Cursor/runs/20260218T160633Z_theo1_staged_pipeline/`
- **Key params**: `--a-hours 1`, Stage A workers `20`, max-safe-workers `24`, Stage A sim_workers `1`, spawn_stagger_seconds `0.5` (Stage B/C defaults unchanged).
- **Stage outcomes**:
  - Stage A trigger: **PASS** (`mean_delta=0.0823`, `p10_delta=0.0281`)
  - Stage B gate: **PASS** (`lcb95_mean_delta=0.0187`, `p10_delta=-0.1190`)
  - Stage C final gate: **FAIL** (`lcb95_mean_delta=-0.0157`, `p10_delta=-0.0711`)
  - Final status: `stopped_stage_c_final_failed` (`promoted_path=null`)
- **Recommendation**: Keep the same pipeline flow, but bias next search toward reducing left-tail downside (focus on seeds with largest negative Stage C deltas, especially the outlier around seed `45027`) before re-running Stage B/C gate checks.


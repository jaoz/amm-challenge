# New Agent Search Runbook

## Goal
Improve leaderboard performance, not single-seed score.

Primary target:
- Increase `mean_edge` on broad seed sets.

Guardrails:
- Do not worsen fee materially.
- Do not reduce retail and arb capture.

## Current Champion Base
Use this strategy as the starting point:

`Strat/theo1_local_islandga_10h_expanded_q30r100_w7i5_seed42000_20260217T2250358541760Z/worker_0.best.sol`

## Important Context
- Optimizer script: `scripts/optimize_theo1_local_parallel.py`
- It supports:
  - `--eval-seeds` for train seeds
  - `--holdout-seeds` for promotion guardrails
- Seed diversity fix is already applied in evaluation:
  - `MatchRunner(..., seed_offset=eval_seed * 1_000_000)`
- The latest multi-seed run showed only tiny edge gain and worse fee/flow, so do not promote it as champion.

## 1) Preflight: Stop Any Running Optimizers
```powershell
Get-CimInstance Win32_Process |
  Where-Object { $_.Name -like 'python*.exe' -and $_.CommandLine -like '*optimize_theo1_local_parallel.py*' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force }

Get-Process | Where-Object { $_.ProcessName -like 'python*' }
```

## 2) Launch New 2h Search (Train + Holdout)
Recommended parameter block:
- workers: `7`
- hours: `2`
- quick/refine: `6 / 16`
- refine-every: `4`
- step-pct: `0.03`
- max-changes: `4`
- train seeds: `42000..42011`
- holdout seeds: `43000..43005`

```powershell
$ts=(Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssfffffffZ')
$out="Strat/theo1_local_2h_multiseed_holdout_w7_q6r16_$ts"

py -3.10 scripts/optimize_theo1_local_parallel.py --mode launch `
  --base-strategy Strat/theo1_local_islandga_10h_expanded_q30r100_w7i5_seed42000_20260217T2250358541760Z/worker_0.best.sol `
  --workers 7 `
  --hours 2 `
  --quick-sims 6 `
  --refine-sims 16 `
  --refine-every 4 `
  --seed 42000 `
  --eval-seeds 42000,42001,42002,42003,42004,42005,42006,42007,42008,42009,42010,42011 `
  --holdout-seeds 43000,43001,43002,43003,43004,43005 `
  --mutable-constants BASE_FEE,MIN_GATE,GATE_SIGMA_MULT,RET_CAP,PHAT_ALPHA_RETAIL,PHAT_ALPHA,SIGMA_COEF,LAMBDA_COEF,FLOW_SIZE_COEF,TOX_COEF,TOX_QUAD_COEF,TOX_CUBIC_COEF,SHIELD_TRIGGER,SHIELD_BUFFER,DIR_TOX_COEF,SIGMA_TOX_COEF `
  --step-pct 0.03 `
  --max-changes 4 `
  --max-drift 2.0 `
  --restart-prob 0.2 `
  --global-parent-prob 0.55 `
  --global-top-k 128 `
  --share-sync-every 4 `
  --min-delta-edge 0.005 `
  --holdout-max-drop-edge 0.10 `
  --holdout-max-drop-retail 200 `
  --holdout-max-drop-arb 100 `
  --out-dir $out

$out
```

## 3) Monitor During Run
```powershell
Get-CimInstance Win32_Process |
  Where-Object { $_.Name -like 'python*.exe' -and $_.CommandLine -like '*optimize_theo1_local_parallel.py*' } |
  Select-Object ProcessId,CommandLine
```

```powershell
$d="<OUT_DIR_FROM_LAUNCH>"
0..6 | ForEach-Object { "`n### worker $_"; Get-Content "$d/worker_$_.stdout.log" -Tail 20 }
```

If no meaningful improvement by midway, do one adjustment only:
- either increase `step-pct` to `0.035`, or
- reduce mutable set to core fee/gate/flow/tox.

## 4) Collect Best Artifact
```powershell
$d="<OUT_DIR_FROM_LAUNCH>"
py -3.10 scripts/optimize_theo1_local_parallel.py --mode collect --out-dir $d
Get-Content "$d/best_overall.json"
```

## 5) 1000-Sim Proxy Validation (Base vs New Best)
Use `scripts/run_match_and_log.py` with seed offsets `0`, `1000`, `2000`.

```powershell
$base="Strat/theo1_local_islandga_10h_expanded_q30r100_w7i5_seed42000_20260217T2250358541760Z/worker_0.best.sol"
$cand="<OUT_DIR_FROM_LAUNCH>/best_overall.sol"
$log="logs/leaderboard_proxy_compare_$(Get-Date -Format yyyyMMddTHHmmss).jsonl"

py -3.10 scripts/run_match_and_log.py $base --log-path $log --stage validation --seed-set-id proxy --seed-offset 0 --workers 7
py -3.10 scripts/run_match_and_log.py $cand --log-path $log --stage validation --seed-set-id proxy --seed-offset 0 --workers 7
py -3.10 scripts/run_match_and_log.py $base --log-path $log --stage validation --seed-set-id proxy --seed-offset 1000 --workers 7
py -3.10 scripts/run_match_and_log.py $cand --log-path $log --stage validation --seed-set-id proxy --seed-offset 1000 --workers 7
py -3.10 scripts/run_match_and_log.py $base --log-path $log --stage validation --seed-set-id proxy --seed-offset 2000 --workers 7
py -3.10 scripts/run_match_and_log.py $cand --log-path $log --stage validation --seed-set-id proxy --seed-offset 2000 --workers 7
```

## 6) Promotion Rule
Promote candidate only if proxy validation shows:
- clear edge gain (not noise-level),
- no fee deterioration that harms competitiveness,
- no persistent retail/arb drop.

If not met, keep current champion and iterate with a different search regime.

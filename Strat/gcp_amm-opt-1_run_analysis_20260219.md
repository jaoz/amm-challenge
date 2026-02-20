# amm-opt-1 Run Analysis (2026-02-19)

## Scope
- VM: `amm-opt-1`
- Zone: `europe-central2-b`
- Data source: `/root/amm-challenge/Cursor/runs/*theo1*staged_pipeline`
- Collection time (UTC): `2026-02-19`

## Completed Runs Found

| Run Dir | Base Strategy | Workers | Duration (h) | Stage A mean/p10 | Stage B lcb95/p10 | Stage C lcb95/mean/p10/min | Status |
|---|---|---:|---:|---:|---:|---:|---|
| `20260219T131344Z_theo1_staged_pipeline` | `theo1_v3_promoted_20260219.sol` | 48 | 8.475 | 2.8346 / 2.5877 | 2.5387 / 2.4375 | **2.7090 / 2.7837 / 2.4664 / 2.1750** | passed_all_stages |
| `20260219T034902Z_theo1_staged_pipeline` | `theo1_v2_promoted_20260218.sol` | 48 | 8.397 | 2.1376 / 1.8658 | 2.2094 / 1.7293 | 2.2034 / 2.2846 / 1.9839 / 1.4732 | passed_all_stages |
| `20260219T133347Z_theo1_staged_pipeline` | `theo1_v2_promoted_20260218.sol` | 32 | 8.399 | 1.3306 / 0.9859 | 1.3329 / 1.1387 | 1.2510 / 1.3125 / 1.0863 / 0.6847 | passed_all_stages |

## Best Selection
- Best run by Stage C `lcb95_mean_delta`: `20260219T131344Z_theo1_staged_pipeline`
- Best Stage C `lcb95_mean_delta`: `2.7090177081`
- Previous champion Stage C `lcb95_mean_delta`: `2.2034442403` (`20260219T034902Z`)
- Improvement: `+0.5055734678` (`+22.94%`)

Additional robustness lift vs previous champion:
- Stage C mean delta: `+0.4991316027`
- Stage C p10 delta: `+0.4824978658`
- Stage C min delta: `+0.7017955687`

## Artifacts Collected Locally
- Best run bundle: `Strat/gcp_vm1_20260219T131344Z_theo1_staged_pipeline`
- Other run bundles:
  - `Strat/gcp_vm1_20260219T034902Z_theo1_staged_pipeline`
  - `Strat/gcp_vm1_20260219T133347Z_theo1_staged_pipeline`

## Promotion Output
- Promoted strategy copied to:
  - `Cursor/strategies/champions/theo1_v4_promoted_20260219.sol`
- Source:
  - `Strat/gcp_vm1_20260219T131344Z_theo1_staged_pipeline/promoted_best.sol`

## Notes
- The latest run (`20260219T133347Z`) is not the strongest run on this VM.
- It used 32 workers and a `v2` base, which likely explains weaker outcomes vs the 48-worker `v3`-based run.

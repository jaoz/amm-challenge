# GCP 2-VM Focused 3h Run Analysis (2026-02-20)

## Run Setup
- VM1: `amm-opt-1` (`20260220T143206Z_theo1_staged_pipeline`)
  - Base: `theo1_v6_promoted_20260220.sol`
  - Search lane: `step_pct=0.09`, `max_changes=4`, `workers=48`
- VM2: `amm-opt-2` (`20260220T143209Z_theo1_staged_pipeline`)
  - Base: `theo1_v6_promoted_20260220.sol`
  - Search lane: `step_pct=0.12`, `max_changes=5`, `workers=48`
- Focused mutable set (12): `TOX_BLEND_DECAY`, `SIZE_BLEND_DECAY`, `SHIELD_TRIGGER`, `DIR_DECAY`, `SIGMA_TOX_COEF`, `TRADE_TOX_BOOST`, `GAP_PHAT_ALPHA_BOOST`, `TAIL_SLOPE_PROTECT`, `SIZE_SMALL_DECAY`, `BASE_FEE`, `SHIELD_BUFFER`, `PHAT_ALPHA_RETAIL`.

## Results

### VM1
- Status: `passed_all_stages`
- Stage A: `mean_delta=0.3042`, `p10_delta=0.0310`, `min_delta=0.0310`
- Stage B: `lcb95=0.1469`, `mean=0.2427`, `p10=-0.0289`, `min=-0.3859`
- Stage C: `lcb95=0.2674`, `mean=0.3043`, `p10=0.1031`, `min=0.0508`

### VM2 (winner)
- Status: `passed_all_stages`
- Stage A: `mean_delta=0.5418`, `p10_delta=0.3017`, `min_delta=0.3017`
- Stage B: `lcb95=0.3012`, `mean=0.3889`, `p10=0.0812`, `min=-0.1300`
- Stage C: `lcb95=0.4040`, `mean=0.4506`, `p10=0.2529`, `min=0.1412`

## Winner
- Winner by Stage C `lcb95_mean_delta`: **VM2** (`0.4040`)
- Compared with prior `v6` origin run (`lcb95=0.3415`): `+0.0625` absolute (~`+18.3%` relative).

## Parameter Change Review (vs `theo1_v6_promoted_20260220.sol`)

### Shared directional signal
- `GAP_PHAT_ALPHA_BOOST`: down on both (VM1 `-19.15%`, VM2 `-23.82%`)
- `TOX_BLEND_DECAY`: down on both (VM1 `-4.21%`, VM2 `-13.83%`)
- `SIZE_BLEND_DECAY`: down on both (VM1 `-4.96%`, VM2 `-12.11%`)
- `SHIELD_TRIGGER`: down on both (VM1 `-5.25%`, VM2 `-11.30%`)
- `DIR_DECAY`: down on both (VM1 `-0.58%`, VM2 `-11.49%`)
- `SIZE_SMALL_DECAY`: up on both to `1.0` (`+1.73%`)
- `PHAT_ALPHA_RETAIL`: up on both (VM1 `+7.84%`, VM2 `+1.22%`)
- `SHIELD_BUFFER`: down on both (VM1 `-1.49%`, VM2 `-11.18%`)
- `BASE_FEE`: down on both (VM1 `-1.30%`, VM2 `-11.62%`)

### VM2-specific differentiators
- Larger decreases in memory/gap controls (`TOX_BLEND_DECAY`, `SIZE_BLEND_DECAY`, `GAP_PHAT_ALPHA_BOOST`)
- `SIGMA_TOX_COEF`: up `+6.61%` (VM1 unchanged)
- `TRADE_TOX_BOOST`: VM1 up `+10.62%`, VM2 unchanged

## Notes
- Artifacts were recovered from VM disks due guest-network failure preventing direct SSH/SCP during collection.
- Recovered files now exist under:
  - `vm1_20260220T143206Z_theo1_staged_pipeline/`
  - `vm2_20260220T143209Z_theo1_staged_pipeline/`
- Both original compute VMs are now `TERMINATED`.

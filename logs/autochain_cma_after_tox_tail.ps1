$ErrorActionPreference = "Stop"
$runDir = "Strat/theo1_local_search_2h_tox_tail_recovery_from_1h_feecore_20260217T1515323647067Z"
$tag = "theo1_local_search_2h_tox_tail_recovery_from_1h_feecore_20260217T1515323647067Z"
$mutable = "TOX_COEF,TOX_QUAD_COEF,TOX_CUBIC_COEF,SIGMA_TOX_COEF,DIR_TOX_COEF,TRADE_TOX_BOOST,ARB_TOX_MIN,RET_CAP,TAIL_KNEE,TAIL_SLOPE_PROTECT,SHIELD_TRIGGER,SHIELD_BUFFER"
$seed = 40000

"[$((Get-Date).ToUniversalTime().ToString('o'))] watcher-start runDir=$runDir" | Out-File -FilePath "logs/autochain_cma_after_tox_tail.log" -Encoding utf8 -Append

while ($true) {
  $procs = Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" | Where-Object { $_.CommandLine -like "*$tag*" }
  if (-not $procs) { break }
  "[$((Get-Date).ToUniversalTime().ToString('o'))] waiting active_workers=$($procs.Count)" | Out-File -FilePath "logs/autochain_cma_after_tox_tail.log" -Encoding utf8 -Append
  Start-Sleep -Seconds 30
}

"[$((Get-Date).ToUniversalTime().ToString('o'))] collecting $runDir" | Out-File -FilePath "logs/autochain_cma_after_tox_tail.log" -Encoding utf8 -Append
py -3.10 scripts/optimize_theo1_local_parallel.py --mode collect --out-dir $runDir | Out-File -FilePath "logs/autochain_cma_after_tox_tail.log" -Encoding utf8 -Append

$base = "$runDir/best_overall.sol"
if (-not (Test-Path $base)) {
  "[$((Get-Date).ToUniversalTime().ToString('o'))] ERROR missing base strategy $base" | Out-File -FilePath "logs/autochain_cma_after_tox_tail.log" -Encoding utf8 -Append
  exit 1
}

$ts=(Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssfffffffZ')
$out="Strat/theo1_cmaes_2h_tox_tail_from_local_$ts"
"[$((Get-Date).ToUniversalTime().ToString('o'))] launching cma run out=$out" | Out-File -FilePath "logs/autochain_cma_after_tox_tail.log" -Encoding utf8 -Append

py -3.10 scripts/optimize_theo1_cmaes_parallel.py --mode launch --base-strategy $base --workers 8 --hours 2 --quick-sims 50 --refine-sims 100 --refine-every 5 --seed $seed --mutable-constants $mutable --step-pct 0.12 --max-drift 2.5 --min-delta-edge 0.05 --out-dir $out | Out-File -FilePath "logs/autochain_cma_after_tox_tail.log" -Encoding utf8 -Append

"[$((Get-Date).ToUniversalTime().ToString('o'))] done cma_out=$out" | Out-File -FilePath "logs/autochain_cma_after_tox_tail.log" -Encoding utf8 -Append

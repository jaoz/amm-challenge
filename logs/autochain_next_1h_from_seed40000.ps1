$ErrorActionPreference = "Stop"
$runDir = "Strat/theo1_local_search_2h_fee_tox_seed40000_20260217T1201175748912Z"
$tag = "theo1_local_search_2h_fee_tox_seed40000_20260217T1201175748912Z"
$mutable = "BASE_FEE,SIGMA_COEF,LAMBDA_COEF,FLOW_SIZE_COEF,DIR_IMPACT_MULT,GATE_SIGMA_MULT,MIN_GATE,RET_CAP,ELAPSED_CAP,PHAT_ALPHA_RETAIL,PHAT_ALPHA,SIGMA_DECAY,LAMBDA_DECAY"
$seed = 40000

"[$((Get-Date).ToUniversalTime().ToString('o'))] watcher-start runDir=$runDir" | Out-File -FilePath "logs/autochain_next_1h_from_seed40000.log" -Encoding utf8 -Append

while ($true) {
  $procs = Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" | Where-Object { $_.CommandLine -like "*$tag*" }
  if (-not $procs) { break }
  "[$((Get-Date).ToUniversalTime().ToString('o'))] waiting active_workers=$($procs.Count)" | Out-File -FilePath "logs/autochain_next_1h_from_seed40000.log" -Encoding utf8 -Append
  Start-Sleep -Seconds 30
}

"[$((Get-Date).ToUniversalTime().ToString('o'))] collecting $runDir" | Out-File -FilePath "logs/autochain_next_1h_from_seed40000.log" -Encoding utf8 -Append
py -3.10 scripts/optimize_theo1_local_parallel.py --mode collect --out-dir $runDir | Out-File -FilePath "logs/autochain_next_1h_from_seed40000.log" -Encoding utf8 -Append

$base = "$runDir/best_overall.sol"
if (-not (Test-Path $base)) {
  "[$((Get-Date).ToUniversalTime().ToString('o'))] ERROR missing base strategy $base" | Out-File -FilePath "logs/autochain_next_1h_from_seed40000.log" -Encoding utf8 -Append
  exit 1
}

$ts=(Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssfffffffZ')
$out="Strat/theo1_local_search_1h_feecore_seed40000_from_prev_$ts"
"[$((Get-Date).ToUniversalTime().ToString('o'))] launching next run out=$out" | Out-File -FilePath "logs/autochain_next_1h_from_seed40000.log" -Encoding utf8 -Append

py -3.10 scripts/optimize_theo1_local_parallel.py --mode launch --base-strategy $base --workers 8 --hours 1 --quick-sims 50 --refine-sims 100 --refine-every 5 --seed $seed --mutable-constants $mutable --step-pct 0.20 --max-changes 8 --max-drift 4.0 --restart-prob 0.45 --min-delta-edge 0.0 --out-dir $out | Out-File -FilePath "logs/autochain_next_1h_from_seed40000.log" -Encoding utf8 -Append

"[$((Get-Date).ToUniversalTime().ToString('o'))] done next_out=$out" | Out-File -FilePath "logs/autochain_next_1h_from_seed40000.log" -Encoding utf8 -Append

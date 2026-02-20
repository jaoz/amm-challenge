$ErrorActionPreference = "Stop"
$runDir = "Strat/theo1_cmaes_2h_tox_tail_from_local_manual_20260217T1546035948131Z"
$tag = "theo1_cmaes_2h_tox_tail_from_local_manual_20260217T1546035948131Z"
$log = "logs/postprocess_cma_latest_normalize.log"

"[$((Get-Date).ToUniversalTime().ToString('o'))] watcher-start runDir=$runDir" | Out-File -FilePath $log -Encoding utf8 -Append
while ($true) {
  $procs = Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" | Where-Object { $_.CommandLine -like "*$tag*" }
  if (-not $procs) { break }
  "[$((Get-Date).ToUniversalTime().ToString('o'))] waiting active_workers=$($procs.Count)" | Out-File -FilePath $log -Encoding utf8 -Append
  Start-Sleep -Seconds 30
}

"[$((Get-Date).ToUniversalTime().ToString('o'))] collecting $runDir" | Out-File -FilePath $log -Encoding utf8 -Append
py -3.10 scripts/optimize_theo1_cmaes_parallel.py --mode collect --out-dir $runDir | Out-File -FilePath $log -Encoding utf8 -Append

$bestSol = "$runDir/best_overall.sol"
if (-not (Test-Path $bestSol)) {
  "[$((Get-Date).ToUniversalTime().ToString('o'))] ERROR missing $bestSol" | Out-File -FilePath $log -Encoding utf8 -Append
  exit 1
}

$backup = "$runDir/best_overall_pre_normalize.sol"
Copy-Item $bestSol $backup -Force
"[$((Get-Date).ToUniversalTime().ToString('o'))] backup=$backup" | Out-File -FilePath $log -Encoding utf8 -Append

py -3.10 scripts/normalize_theo1_constants_no_div.py --in-path $bestSol --out-path $bestSol --line-limit 100 | Out-File -FilePath $log -Encoding utf8 -Append

"[$((Get-Date).ToUniversalTime().ToString('o'))] done normalized_in_place=$bestSol" | Out-File -FilePath $log -Encoding utf8 -Append

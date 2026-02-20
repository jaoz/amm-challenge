$PLINK  = "C:\Program Files (x86)\Google\Cloud SDK\google-cloud-sdk\bin\sdk\plink.exe"
$PSCP   = "C:\Program Files (x86)\Google\Cloud SDK\google-cloud-sdk\bin\sdk\pscp.exe"
$GCLOUD = "C:\Program Files (x86)\Google\Cloud SDK\google-cloud-sdk\bin\gcloud.cmd"
$PPK    = "$env:USERPROFILE\.ssh\google_compute_engine.ppk"
$IP     = "34.116.240.219"
$FPR    = "SHA256:dPd3B7F67qf4K+hqnD5T8QKsoWzHlv+/AQD2tMrESuw"
$USER   = $env:USERNAME
$STRAT  = "C:\Users\azhel\PycharmProjects\amm-challenge\Strat"
$RUNS   = "C:\Users\azhel\PycharmProjects\amm-challenge\Cursor\runs"
$LOG    = "$RUNS\gcp_monitor.log"

function Log { param([string]$m); $t=(Get-Date -Format "HH:mmZ"); "$t  $m" | Tee-Object -Append -FilePath $LOG }
function SSH { param([string]$c); & $PLINK -batch -hostkey $FPR -i $PPK -l $USER $IP $c 2>&1 }
function SCP { param([string]$r,[string]$l); & $PSCP -batch -hostkey $FPR -i $PPK "${USER}@${IP}:${r}" $l 2>&1 }

Log "=== MONITOR: n2-highcpu-48 production run ==="

# Wait for benchmark to finish (~15 min)
Log "Waiting for production benchmark..."
$d = (Get-Date).AddMinutes(35)
while ((Get-Date) -lt $d) {
    Start-Sleep 90
    $r = SSH "sudo cat /tmp/bench_production_done 2>/dev/null" | Out-String
    Log "bench: $($r.Trim())"
    if ($r -match "BENCH_DONE") { break }
}
Log "Benchmark done. Ranked:"; SSH "sudo cat /tmp/bench_ranked.txt 2>/dev/null" | ForEach-Object { Log "  $_" }

# Wait for pipeline to start
$d = (Get-Date).AddMinutes(15)
while ((Get-Date) -lt $d) {
    Start-Sleep 30
    $r = SSH "sudo cat /tmp/pipeline_started 2>/dev/null" | Out-String
    if ($r -match "PIPELINE_STARTED") { Log "Pipeline: $($r.Trim())"; break }
}

# Wait for CPU check
$d = (Get-Date).AddMinutes(20)
while ((Get-Date) -lt $d) {
    Start-Sleep 60
    $r = SSH "sudo ls /tmp/cpu_check_done 2>/dev/null" | Out-String
    if ($r -match "cpu_check_done") { break }
}
Log "=== CPU CHECK ==="; SSH "sudo cat /tmp/cpu_check.log 2>/dev/null" | ForEach-Object { Log "  $_" }

# Poll pipeline every 30 min
Log "Polling 8h pipeline (30 min intervals)..."
$d = (Get-Date).AddHours(11)
while ((Get-Date) -lt $d) {
    Start-Sleep 1800
    $tail = SSH "sudo tail -2 /tmp/pipeline_stdout.log 2>/dev/null" | Out-String
    Log "pipe: $($tail.Trim())"
    $done = SSH "sudo grep -c PRODUCTION_DONE /tmp/production_done.txt 2>/dev/null" | Out-String
    if ($done.Trim() -ne "0" -and $done.Trim() -ne "") { Log "PIPELINE DONE"; break }
}

# Collect results
Log "=== Collecting results ==="
$doneRaw = SSH "sudo cat /tmp/production_done.txt 2>/dev/null" | Out-String
Log $doneRaw
$RUN_DIR = ($doneRaw -split "`n" | Where-Object { $_ -match "^RUN_DIR=" }) -replace "RUN_DIR=",""
$RUN_DIR = $RUN_DIR.Trim()
$STATUS  = ($doneRaw -split "`n" | Where-Object { $_ -match "^STATUS=" }) -replace "STATUS=",""
$STATUS  = $STATUS.Trim()
Log "RUN=$RUN_DIR  STATUS=$STATUS"

$rbase = "/root/amm-challenge/Cursor/runs/$RUN_DIR"
$pref  = "$STRAT\gcp_${RUN_DIR}"
foreach ($f in @("pipeline_result.json","stage_a_report.json","stage_b_report.json","stage_c_report.json")) {
    SCP "$rbase/$f" "${pref}_$f" | ForEach-Object { Log $_ }
}
if ($STATUS -eq "PROMOTED") {
    SCP "$rbase/promoted_best.sol" "${pref}_promoted_best.sol" | ForEach-Object { Log $_ }
    Log "Promoted: ${pref}_promoted_best.sol"
}
SCP "/tmp/bench_production/benchmark_report.json" "$RUNS\gcp_${RUN_DIR}_bench48.json" | ForEach-Object { Log $_ }
SCP "/tmp/cpu_check.log"                          "$RUNS\gcp_${RUN_DIR}_cpu_check.log" | ForEach-Object { Log $_ }

Log "=== Stopping VM ==="
& $GCLOUD compute instances stop amm-opt-1 --zone=europe-central2-b --project=ammopt 2>&1 | ForEach-Object { Log $_ }
Log "VM stopped. DONE."

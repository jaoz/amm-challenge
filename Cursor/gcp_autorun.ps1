param(
    [string]$Phase = "all"
)

$GCLOUD   = "C:\Program Files (x86)\Google\Cloud SDK\google-cloud-sdk\bin\gcloud.cmd"
$PROJECT  = "ammopt"
$ZONE     = "europe-central2-b"
$VM       = "amm-opt-1"
$REPO     = "C:\Users\azhel\PycharmProjects\amm-challenge"
$STRAT    = "$REPO\Strat"
$RUNS     = "$REPO\Cursor\runs"
$LOGFILE  = "$RUNS\gcp_autorun.log"
$SMOKE_SH = "$REPO\Cursor\gcp_bench_startup.sh"
$PROD_SH  = "$REPO\Cursor\gcp_production_startup.sh"

New-Item -ItemType Directory -Force -Path $RUNS | Out-Null

function Log {
    param([string]$msg)
    $ts = (Get-Date -Format "yyyy-MM-ddTHH:mm:ssZ")
    $line = "$ts  $msg"
    $line | Tee-Object -Append -FilePath $LOGFILE
}

function GC { & $GCLOUD @args 2>&1 }

function SSH {
    param([string]$cmd)
    GC compute ssh $VM --zone=$ZONE --project=$PROJECT "--ssh-flag=-batch" --command=$cmd
}

function WaitForFlag {
    param([string]$flag, [int]$pollSec=120, [int]$timeoutMin=60, [string]$label="")
    $deadline = (Get-Date).AddMinutes($timeoutMin)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Seconds $pollSec
        $r = (SSH "ls $flag 2>/dev/null && echo FOUND || echo WAITING") | Out-String
        Log "${label}: $($r.Trim())"
        if ($r -match "FOUND") { return $true }
    }
    Log "TIMEOUT waiting for $flag"
    return $false
}

function Download {
    param([string]$remote, [string]$local)
    Log "Downloading $remote -> $local"
    GC compute scp --zone=$ZONE --project=$PROJECT "--ssh-flag=-batch" "${VM}:${remote}" $local
}

# ---- Phase 1: Smoke test on n2-highcpu-4 ------------------------------------
if ($Phase -eq "all") {
    Log "====== PHASE 1: smoke on n2-highcpu-4 ======"
    Log "Creating VM..."
    GC compute instances create $VM `
        --project=$PROJECT --zone=$ZONE `
        --machine-type=n2-highcpu-4 `
        --image-family=ubuntu-2204-lts --image-project=ubuntu-os-cloud `
        --boot-disk-size=50GB --boot-disk-type=pd-ssd `
        --scopes=cloud-platform --metadata=enable-oslogin=FALSE `
        "--metadata-from-file=startup-script=$SMOKE_SH"
    Log "VM created. Waiting for smoke (timeout 50 min)..."

    $ok = WaitForFlag "/tmp/bench_done" 120 50 "smoke"
    if (-not $ok) {
        Log "ERROR: smoke timed out. SSH in and check /tmp/startup.log"
        exit 1
    }
    Log "Smoke PASSED:"
    SSH "cat /tmp/bench_summary.txt 2>/dev/null || echo no summary" | ForEach-Object { Log "  $_" }

    Log "====== Resizing to n2-highcpu-48 ======"
    GC compute instances stop $VM --zone=$ZONE --project=$PROJECT
    Log "VM stopped."
    GC compute instances set-machine-type $VM --machine-type=n2-highcpu-48 --zone=$ZONE --project=$PROJECT
    Log "Machine type -> n2-highcpu-48"
    GC compute instances add-metadata $VM --zone=$ZONE --project=$PROJECT "--metadata-from-file=startup-script=$PROD_SH"
    Log "Startup script -> production"
    GC compute instances start $VM --zone=$ZONE --project=$PROJECT
    Log "VM started."
}

# ---- Phase 2: Production run on n2-highcpu-48 --------------------------------
Log "====== PHASE 2: production run on n2-highcpu-48 ======"
Log "Waiting for production benchmark (timeout 30 min)..."
$ok = WaitForFlag "/tmp/bench_production_done" 90 30 "bench48"
if (-not $ok) { Log "ERROR: production benchmark timed out"; exit 1 }

Log "Benchmark done:"
SSH "cat /tmp/bench_ranked.txt 2>/dev/null" | ForEach-Object { Log "  $_" }
SSH "cat /tmp/bench_production_done 2>/dev/null" | ForEach-Object { Log "  $_" }

Log "Waiting for pipeline to start (timeout 10 min)..."
WaitForFlag "/tmp/pipeline_started" 30 10 "pipe_start" | Out-Null
SSH "cat /tmp/pipeline_started 2>/dev/null" | ForEach-Object { Log "  $_" }

Log "Waiting for CPU check (timeout 20 min)..."
$ok = WaitForFlag "/tmp/cpu_check_done" 60 20 "cpu_check"
if ($ok) {
    Log "CPU check results:"
    SSH "cat /tmp/cpu_check.log 2>/dev/null" | ForEach-Object { Log "  $_" }
}

Log "Polling pipeline every 30 min (timeout 11h)..."
$deadline = (Get-Date).AddHours(11)
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 1800
    $tail = (SSH "tail -3 /tmp/pipeline_stdout.log 2>/dev/null") | Out-String
    Log "pipeline: $($tail.Trim())"
    $done = (SSH "grep -c PRODUCTION_DONE /tmp/production_done.txt 2>/dev/null || echo 0") | Out-String
    if ($done.Trim() -ne "0") { Log "Pipeline DONE"; break }
}

# ---- Phase 3: Collect results ------------------------------------------------
Log "====== PHASE 3: collecting results ======"
$doneRaw = (SSH "cat /tmp/production_done.txt 2>/dev/null") | Out-String
Log "Done file: $($doneRaw.Trim())"

$RUN_DIR = ($doneRaw -split "`n" | Where-Object { $_ -match "^RUN_DIR=" }) -replace "RUN_DIR=",""
$RUN_DIR = $RUN_DIR.Trim()
$STATUS  = ($doneRaw -split "`n" | Where-Object { $_ -match "^STATUS=" }) -replace "STATUS=",""
$STATUS  = $STATUS.Trim()
Log "RUN_DIR=$RUN_DIR  STATUS=$STATUS"

$remote = "/root/amm-challenge/Cursor/runs/$RUN_DIR"
$pref   = "$STRAT\gcp_${RUN_DIR}"

foreach ($f in @("pipeline_result.json","stage_a_report.json","stage_b_report.json","stage_c_report.json")) {
    Download "$remote/$f" "${pref}_$f"
}
if ($STATUS -eq "PROMOTED") {
    Download "$remote/promoted_best.sol" "${pref}_promoted_best.sol"
    Log "Promoted file saved: ${pref}_promoted_best.sol"
}
Download "/tmp/bench_production/benchmark_report.json" "$RUNS\gcp_${RUN_DIR}_benchmark.json"
Download "/tmp/cpu_check.log"                           "$RUNS\gcp_${RUN_DIR}_cpu_check.log"

# Print summary
Log "========================================"
Log "=== GCP Cloud Run Summary ==="
Log "VM: $VM  (n2-highcpu-48, $ZONE)"
Log "Run: $RUN_DIR"
Log "STATUS: $STATUS"

$rjson = "${pref}_pipeline_result.json"
if (Test-Path $rjson) {
    $r = Get-Content $rjson | ConvertFrom-Json
    $sa = $r.stage_a; $sb = $r.stage_b; $sc = $r.stage_c
    Log "Stage A: pass=$($sa.pass)  mean_delta=$($sa.evaluation.summary.mean_delta)  p10=$($sa.evaluation.summary.p10_delta)"
    Log "Stage B: pass=$($sb.pass)  lcb95=$($sb.evaluation.summary.lcb95_mean_delta)  p10=$($sb.evaluation.summary.p10_delta)"
    Log "Stage C: pass=$($sc.pass)  lcb95=$($sc.evaluation.summary.lcb95_mean_delta)  p10=$($sc.evaluation.summary.p10_delta)"
    Log "vs baseline:  B lcb95 1.03 -> $($sb.evaluation.summary.lcb95_mean_delta)"
    Log "              C lcb95 1.04 -> $($sc.evaluation.summary.lcb95_mean_delta)"
}
if ($STATUS -eq "PROMOTED") {
    Log "Recommendation: USE promoted_best.sol as new champion"
} else {
    Log "Recommendation: No promotion -- check stage reports"
}
Log "========================================"

Log "Stopping VM..."
GC compute instances stop $VM --zone=$ZONE --project=$PROJECT
Log "VM stopped. Zero cost."
Log "====== AUTOMATION COMPLETE ======"

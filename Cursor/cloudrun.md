# Cloud Optimization Run — Agent Runbook

## Purpose
This document is a self-contained prompt for an autonomous agent to execute a **complete GCP optimization cycle** for the AMM Challenge Theo1 strategy:
VM start → pipeline launch → monitoring → result download → VM stop.

Read this entire file before taking any action. Execute every step in order.

---

## Context: What This Project Is

- **Competition**: AMM (Automated Market Maker) fee-strategy optimization challenge.
- **Goal**: Maximize `mean_edge` — the average per-trade edge earned by the strategy across simulation seeds.
- **Strategy file**: A Solidity `.sol` file with embedded integer constants that are optimized numerically.
- **Pipeline**: `Cursor/tools/run_theo1_staged_pipeline.py` — a Python script that:
  - **Stage A**: Runs N parallel local-search workers (each tries random parameter mutations, keeps improvements). Duration: configurable (`--a-hours`).
  - **Stage B**: Evaluates the best candidate found by Stage A against 20 holdout seeds. Gate: `lcb95_mean_delta > 0` AND `p10_delta >= -10`.
  - **Stage C**: Evaluates the same candidate against 48 holdout seeds. Gate: same rule.
  - On full pass → writes `promoted_best.sol` to the run directory.
- **Current champion**: `Cursor/strategies/champions/theo1_v3_promoted_20260219.sol`
- **Current champion scores** (GCP 8h run `20260219T034902Z`, 48 workers, n2-highcpu-48):
  - Stage A: mean_delta=2.138, p10_delta=1.866
  - Stage B (20 seeds): lcb95=2.209, p10=1.729
  - Stage C (48 seeds): **lcb95=2.203**, p10=1.984, min_delta=1.473 — ALL seeds positive
- **Leaderboard score**: +515.20 Avg Edge per Sim — target is +530 (+15 gap to close)

---

## THIS RUN: 2-VM Parallel Strategy (Target: ~5.5h wall-clock vs 8.5h single-VM)

### Why two VMs?

The previous 8h run showed clear diminishing returns:
- +0.76 avg_edge/hr at 2h → +0.12/hr at 6.5h → essentially converged at 8h
- **Root cause**: 48 workers all share the same hill-climb trajectory and converge to the same local optimum

Two VMs running 5h each gives:
- **96 effective workers** of parallel exploration (vs 48 for 8h)
- **Diversity** via different step sizes (VM1=0.05 conservative, VM2=0.08 aggressive)
- **35% faster wall-clock** (~5.5h vs ~8.5h)
- **8 new unexplored parameters** added to search space (could unlock next local optimum)

### Architecture

```
VM1 (amm-opt-1, n2-highcpu-48, existing):       VM2 (amm-opt-2, n2-highcpu-48, cloned):
  Stage A: 5h, step_pct=0.05, max_changes=3        Stage A: 5h, step_pct=0.08, max_changes=4
  Strategy: conservative exploitation              Strategy: aggressive exploration
  Stage B/C: automatic after Stage A               Stage B/C: automatic after Stage A
       |                                                  |
       +-------------- ~5.5h wall-clock ----------------+
                              |
                     Take max(VM1_lcb95, VM2_lcb95)
                              |
                    Save as theo1_v4_promoted_YYYYMMDD.sol
```

### Cost
- 2× n2-highcpu-48 × 5.5h ≈ **$12.50** total (vs $9.70 for 8.5h single VM)
- ~$2.80 more for substantially higher exploration throughput

---

## Infrastructure State

```
GCP project:    ammopt
Zone:           europe-central2-b

VM1: amm-opt-1  — n2-highcpu-48, TERMINATED, disk preserved (repo + venv + Rust intact)
VM2: amm-opt-2  — n2-highcpu-48, DOES NOT EXIST YET — created from disk snapshot of amm-opt-1

External IPs: assigned on start — always re-read after starting (see steps below)

gcloud path:  C:\Program Files (x86)\Google\Cloud SDK\google-cloud-sdk\bin\gcloud.cmd
plink path:   C:\Program Files (x86)\Google\Cloud SDK\google-cloud-sdk\bin\sdk\plink.exe
pscp path:    C:\Program Files (x86)\Google\Cloud SDK\google-cloud-sdk\bin\sdk\pscp.exe
SSH key PPK:  $env:USERPROFILE\.ssh\google_compute_engine.ppk
SSH user:     $env:USERNAME (your Windows username)
Host key FP1: SHA256:dPd3B7F67qf4K+hqnD5T8QKsoWzHlv+/AQD2tMrESuw  (amm-opt-1, in PuTTY registry)
Host key FP2: (unknown — amm-opt-2 is new; omit -hostkey on first connect to accept and cache)
```

Standard SSH pattern:
```powershell
$plink = "C:\Program Files (x86)\Google\Cloud SDK\google-cloud-sdk\bin\sdk\plink.exe"
$ppk   = "$env:USERPROFILE\.ssh\google_compute_engine.ppk"
$fpr1  = "SHA256:dPd3B7F67qf4K+hqnD5T8QKsoWzHlv+/AQD2tMrESuw"
# For amm-opt-1:  & $plink -batch -hostkey $fpr1 -i $ppk -l $env:USERNAME $IP1 "CMD"
# For amm-opt-2 (first connect — accept key):
#   & $plink -batch -i $ppk -l $env:USERNAME $IP2 "CMD" 2>&1
# After first connect, PuTTY caches the key — add -hostkey $fpr2 once you know it
```

---

## VM Sizing — n2-highcpu-48 is the proven production machine

| Machine | vCPU | RAM | `--a-workers` | Notes |
|---------|------|-----|--------------|-------|
| n2-highcpu-48 | 48 | 47 GB | **48** | Proven stable — DO NOT exceed 48 workers |

> **CRITICAL — DO NOT exceed 48 workers on n2-highcpu-48.**
> A previous run with 60 workers caused CPU scheduling starvation: the kernel's
> network stack starved, SSH became unreachable after ~2.5h, and the VM had to be
> hard-reset. Load average with 48 workers holds exactly 48.x — perfectly stable.

---

## Step 0 — Restore production startup script on amm-opt-1

The startup script was replaced with a no-op during an emergency reset in the
previous session. Before starting amm-opt-1, restore the real production script:

```powershell
$g = "C:\Program Files (x86)\Google\Cloud SDK\google-cloud-sdk\bin\gcloud.cmd"
& $g compute instances add-metadata amm-opt-1 `
  --zone=europe-central2-b --project=ammopt `
  "--metadata-from-file=startup-script=C:\Users\azhel\PycharmProjects\amm-challenge\Cursor\gcp_production_startup.sh" 2>&1
```

Expected output: `Updated [https://...instances/amm-opt-1]`

---

## Step 0.5 — Create amm-opt-2 from disk snapshot of amm-opt-1

This clones the entire disk state (repo, venv, compiled Rust extension) so amm-opt-2
needs zero setup time. amm-opt-1 must be TERMINATED before snapshotting.

```powershell
$g = "C:\Program Files (x86)\Google\Cloud SDK\google-cloud-sdk\bin\gcloud.cmd"

# 1. Snapshot amm-opt-1's boot disk (VM must be stopped — it is TERMINATED)
& $g compute disks snapshot amm-opt-1 `
  --zone=europe-central2-b --project=ammopt `
  --snapshot-names=amm-opt-1-snap-$(Get-Date -Format "yyyyMMdd") 2>&1
# Wait ~2 min for snapshot to complete

# 2. Verify snapshot is READY
$snapName = "amm-opt-1-snap-$(Get-Date -Format "yyyyMMdd")"
& $g compute snapshots describe $snapName --project=ammopt --format="value(status)" 2>&1
# Expected: READY

# 3. Create new boot disk from snapshot
& $g compute disks create amm-opt-2-disk `
  --zone=europe-central2-b --project=ammopt `
  --source-snapshot=$snapName `
  --type=pd-ssd --size=50GB 2>&1

# 4. Create amm-opt-2 VM using that disk as boot disk
& $g compute instances create amm-opt-2 `
  --zone=europe-central2-b --project=ammopt `
  --machine-type=n2-highcpu-48 `
  --disk="name=amm-opt-2-disk,boot=yes,auto-delete=yes" `
  --network-interface="network=default,access-config-name=external-nat" `
  --no-restart-on-failure `
  --maintenance-policy=TERMINATE `
  --metadata=startup-script="#! /bin/bash`nexec > /tmp/startup.log 2>&1`necho startup_noop_$(date -u)" 2>&1
# Note: no-op startup script prevents production pipeline from auto-launching on boot
```

Expected: `Created [https://...instances/amm-opt-2]`

> **If snapshot creation fails** (e.g., amm-opt-1 disk not found):
> Check disk name with: `& $g compute disks list --project=ammopt --zones=europe-central2-b 2>&1`
> The disk is typically named `amm-opt-1` (same as VM).

---

## Step 1 — Start both VMs and get IPs

```powershell
$g = "C:\Program Files (x86)\Google\Cloud SDK\google-cloud-sdk\bin\gcloud.cmd"

# Start both VMs in parallel (background jobs)
$job1 = Start-Job { & "C:\Program Files (x86)\Google\Cloud SDK\google-cloud-sdk\bin\gcloud.cmd" `
  compute instances start amm-opt-1 --zone=europe-central2-b --project=ammopt 2>&1 }
$job2 = Start-Job { & "C:\Program Files (x86)\Google\Cloud SDK\google-cloud-sdk\bin\gcloud.cmd" `
  compute instances start amm-opt-2 --zone=europe-central2-b --project=ammopt 2>&1 }

Wait-Job $job1, $job2 | Out-Null
Receive-Job $job1; Receive-Job $job2

Start-Sleep -Seconds 90

# Get IPs
$IP1 = (& $g compute instances describe amm-opt-1 `
  --zone=europe-central2-b --project=ammopt `
  --format="value(networkInterfaces[0].accessConfigs[0].natIP)" 2>&1).Trim()
$IP2 = (& $g compute instances describe amm-opt-2 `
  --zone=europe-central2-b --project=ammopt `
  --format="value(networkInterfaces[0].accessConfigs[0].natIP)" 2>&1).Trim()

Write-Host "VM1 (amm-opt-1) IP: $IP1"
Write-Host "VM2 (amm-opt-2) IP: $IP2"
```

---

## Step 2 — Verify environment on both VMs

```powershell
$plink = "C:\Program Files (x86)\Google\Cloud SDK\google-cloud-sdk\bin\sdk\plink.exe"
$ppk   = "$env:USERPROFILE\.ssh\google_compute_engine.ppk"
$fpr1  = "SHA256:dPd3B7F67qf4K+hqnD5T8QKsoWzHlv+/AQD2tMrESuw"

# VM1 (known host key)
Write-Host "=== VM1 env check ==="
& $plink -batch -hostkey $fpr1 -i $ppk -l $env:USERNAME $IP1 `
  '/root/amm-challenge/.venv/bin/python -c "import amm_sim_rs; print(chr(79)+chr(75))" 2>/dev/null || echo ENV_MISSING' 2>&1

# VM2 (new VM — accept host key on first connect; -batch will auto-accept)
Write-Host "=== VM2 env check ==="
& $plink -batch -i $ppk -l $env:USERNAME $IP2 `
  '/root/amm-challenge/.venv/bin/python -c "import amm_sim_rs; print(chr(79)+chr(75))" 2>/dev/null || echo ENV_MISSING' 2>&1
```

Expected on both: `OK`

> VM2 is a disk clone — the environment should be identical. If `ENV_MISSING` on VM2,
> the snapshot may not have captured the venv correctly. Run:
> ```powershell
> & $plink -batch -i $ppk -l $env:USERNAME $IP2 `
>   'cd /root/amm-challenge && source .venv/bin/activate && python -c "import amm_sim_rs; print(chr(79)+chr(75))"' 2>&1
> ```

---

## Step 3 — Upload champion strategy to both VMs

```powershell
$pscp = "C:\Program Files (x86)\Google\Cloud SDK\google-cloud-sdk\bin\sdk\pscp.exe"
$LOCAL_SOL = "C:\Users\azhel\PycharmProjects\amm-challenge\Cursor\strategies\champions\theo1_v3_promoted_20260219.sol"

foreach ($vm in @(@{IP=$IP1; fpr=$fpr1; name="VM1"}, @{IP=$IP2; fpr=$null; name="VM2"})) {
    Write-Host "=== Uploading to $($vm.name) ($($vm.IP)) ==="
    & $pscp -batch -i $ppk `
      "$LOCAL_SOL" "${env:USERNAME}@$($vm.IP):/tmp/theo1_v3_promoted_20260219.sol" 2>&1
    & $plink -batch -i $ppk -l $env:USERNAME $($vm.IP) `
      'sudo cp /tmp/theo1_v3_promoted_20260219.sol /root/amm-challenge/Cursor/strategies/champions/theo1_v3_promoted_20260219.sol && echo UPLOADED' 2>&1
}
```

Expected on each: `UPLOADED`

---

## Step 4 — Kill any stale processes on both VMs

```powershell
foreach ($IP in @($IP1, $IP2)) {
    & $plink -batch -i $ppk -l $env:USERNAME $IP `
      'sudo pkill -f run_theo1_staged_pipeline || true; sleep 2; sudo ps aux | grep run_theo1 | grep -v grep | wc -l' 2>&1
}
```

Expected on each: `0`

---

## Step 5 — Launch pipelines on both VMs (simultaneously)

### Mutable constants list (same for both VMs — 30 parameters total)

```
BASE_FEE, MIN_GATE, GATE_SIGMA_MULT, RET_CAP, PHAT_ALPHA_RETAIL, PHAT_ALPHA,
SIGMA_COEF, LAMBDA_COEF, FLOW_SIZE_COEF, TOX_COEF, TOX_QUAD_COEF, TOX_CUBIC_COEF,
SHIELD_TRIGGER, SHIELD_BUFFER, DIR_TOX_COEF, SIGMA_TOX_COEF,
SIZE_SMALL_DECAY, TOX_BLEND_DECAY, GAP_PHAT_ALPHA_BOOST,
STALE_DIR_COEF, STALE_ATTRACT_FRAC, TRADE_TOX_BOOST,
DIR_DECAY, SIZE_BLEND_DECAY, TOX_DECAY,           ← NEW: EMA lifetime params
ARB_RET_MIN, ARB_TOX_MIN, GAP_GATE_PER_STEP,      ← NEW: arb classifier + gate
TAIL_SLOPE_PROTECT, TAIL_SLOPE_ATTRACT             ← NEW: asymmetric tail compression
```

### Why two different strategies?

| | VM1 (amm-opt-1) | VM2 (amm-opt-2) |
|--|--|--|
| `--a-step-pct` | **0.05** — proven step size from last run | **0.08** — larger jumps to escape local optima |
| `--a-max-changes` | **3** — proven setting | **4** — allows 4-parameter diagonal moves |
| `--a-hours` | **5.0** | **5.0** |
| `--a-workers` | **48** | **48** |
| Role | Exploitation (deep search near current optimum) | Exploration (wider jumps into uncharted territory) |

Previous run: the last 2h found only +0.1 improvement. VM2's larger steps can cover the same distance in far fewer iterations.

```powershell
$plink = "C:\Program Files (x86)\Google\Cloud SDK\google-cloud-sdk\bin\sdk\plink.exe"
$ppk   = "$env:USERPROFILE\.ssh\google_compute_engine.ppk"
$fpr1  = "SHA256:dPd3B7F67qf4K+hqnD5T8QKsoWzHlv+/AQD2tMrESuw"

$MUTABLE = (
  "BASE_FEE,MIN_GATE,GATE_SIGMA_MULT,RET_CAP,PHAT_ALPHA_RETAIL,PHAT_ALPHA," +
  "SIGMA_COEF,LAMBDA_COEF,FLOW_SIZE_COEF,TOX_COEF,TOX_QUAD_COEF,TOX_CUBIC_COEF," +
  "SHIELD_TRIGGER,SHIELD_BUFFER,DIR_TOX_COEF,SIGMA_TOX_COEF," +
  "SIZE_SMALL_DECAY,TOX_BLEND_DECAY,GAP_PHAT_ALPHA_BOOST," +
  "STALE_DIR_COEF,STALE_ATTRACT_FRAC,TRADE_TOX_BOOST," +
  "DIR_DECAY,SIZE_BLEND_DECAY,TOX_DECAY," +
  "ARB_RET_MIN,ARB_TOX_MIN,GAP_GATE_PER_STEP," +
  "TAIL_SLOPE_PROTECT,TAIL_SLOPE_ATTRACT"
)

$BASE = "/root/amm-challenge/Cursor/strategies/champions/theo1_v3_promoted_20260219.sol"

# VM1: Conservative exploitation (step_pct=0.05, max_changes=3)
$launchVM1 = "sudo bash -c 'cd /root/amm-challenge && source .venv/bin/activate && " +
  "nohup python Cursor/tools/run_theo1_staged_pipeline.py " +
  "--base-strategy $BASE " +
  "--a-workers 48 --a-max-safe-workers 56 --a-sim-workers 1 --a-hours 5.0 " +
  "--a-step-pct 0.05 --a-max-changes 3 " +
  "--a-mutable-constants $MUTABLE " +
  "> /tmp/pipeline_stdout.log 2>&1 & PID=\$!; echo \$PID > /tmp/pipeline.pid; echo STARTED_pid=\$PID'"

# VM2: Aggressive exploration (step_pct=0.08, max_changes=4)
$launchVM2 = "sudo bash -c 'cd /root/amm-challenge && source .venv/bin/activate && " +
  "nohup python Cursor/tools/run_theo1_staged_pipeline.py " +
  "--base-strategy $BASE " +
  "--a-workers 48 --a-max-safe-workers 56 --a-sim-workers 1 --a-hours 5.0 " +
  "--a-step-pct 0.08 --a-max-changes 4 " +
  "--a-mutable-constants $MUTABLE " +
  "> /tmp/pipeline_stdout.log 2>&1 & PID=\$!; echo \$PID > /tmp/pipeline.pid; echo STARTED_pid=\$PID'"

Write-Host "=== Launching VM1 (conservative) ==="
& $plink -batch -hostkey $fpr1 -i $ppk -l $env:USERNAME $IP1 $launchVM1 2>&1

Write-Host "=== Launching VM2 (aggressive) ==="
& $plink -batch -i $ppk -l $env:USERNAME $IP2 $launchVM2 2>&1
```

Expected output on each: `STARTED_pid=NNNNN`

> **Note on PID tracking bug**: `/tmp/pipeline.pid` may contain the local shell PID
> instead of the remote Python PID. Verify the actual PID with:
> ```powershell
> & $plink -batch -hostkey $fpr1 -i $ppk -l $env:USERNAME $IP1 `
>   'sudo ps aux | grep run_theo1_staged | grep -v grep' 2>&1
> & $plink -batch -i $ppk -l $env:USERNAME $IP2 `
>   'sudo ps aux | grep run_theo1_staged | grep -v grep' 2>&1
> ```

Verify workers are spawning after 60 seconds (expect 48 on each):
```powershell
Start-Sleep -Seconds 60
foreach ($vm in @(@{IP=$IP1; name="VM1"}, @{IP=$IP2; name="VM2"})) {
    $cnt = & $plink -batch -i $ppk -l $env:USERNAME $($vm.IP) `
      'RD=$(sudo ls -t /root/amm-challenge/Cursor/runs/ | head -1); sudo ls /root/amm-challenge/Cursor/runs/$RD/stage_a_search/workers/ 2>/dev/null | wc -l' 2>&1
    Write-Host "$($vm.name): $cnt workers"
}
```

Expected: `48` on each.

---

## Step 6 — Monitor (every ~60 minutes)

Both pipelines run for ~5h (Stage A) + ~30 min (Stage B + C eval). Total: ~5.5h.

```powershell
# Health check — run every hour on both VMs
foreach ($vm in @(@{IP=$IP1; name="VM1"}, @{IP=$IP2; name="VM2"})) {
    Write-Host "=== $($vm.name) health ==="
    & $plink -batch -i $ppk -l $env:USERNAME $($vm.IP) `
      'date -u; uptime; free -h | head -2; sudo ps aux | grep run_theo1_staged | grep -v grep | wc -l' 2>&1
}
# HEALTHY: load average ~48.x, memory ~20 GB used, process count = 2 (bash + python)

# Worker progress — use worker_2 as representative on each VM
foreach ($vm in @(@{IP=$IP1; name="VM1"}, @{IP=$IP2; name="VM2"})) {
    Write-Host "=== $($vm.name) worker_2 progress ==="
    & $plink -batch -i $ppk -l $env:USERNAME $($vm.IP) `
      'RD=$(sudo ls -t /root/amm-challenge/Cursor/runs/ | head -1); sudo tail -1 /root/amm-challenge/Cursor/runs/$RD/stage_a_search/workers/worker_2/stdout.log 2>/dev/null' 2>&1
}
# Format: [worker 2] it=NNN quick score=XXX.XX best=...:YYY.YY

# Done check — look for pipeline_result.json on each
foreach ($vm in @(@{IP=$IP1; name="VM1"}, @{IP=$IP2; name="VM2"})) {
    Write-Host "=== $($vm.name) done? ==="
    & $plink -batch -i $ppk -l $env:USERNAME $($vm.IP) `
      'RD=$(sudo ls -t /root/amm-challenge/Cursor/runs/ | head -1); sudo ls /root/amm-challenge/Cursor/runs/$RD/pipeline_result.json 2>/dev/null || echo NOT_DONE' 2>&1
}
```

### Expected progress trajectory (5h run, new 30-param search space)

| Elapsed | VM1 worker_2 iters | VM2 worker_2 iters | Best score (quick) | Notes |
|---------|---------------|---------------|-------------------|-------|
| 5 min | ~5 | ~4 | ~base | Workers starting |
| 1h | ~60 | ~55 | base+1.2 | Steady improvement |
| 2h | ~110 | ~100 | base+2.2 | Decelerating |
| 3h | ~165 | ~150 | base+2.7 | VM2 may find new territory via larger steps |
| 4h | ~220 | ~200 | base+3.0 | Approaching local optima |
| 5h | ~270 | ~245 | base+3.2 | Stage A ends → B/C begins |

> VM2's larger step size means fewer iterations but larger moves per iteration.
> If VM2 finds a qualitatively different basin, its best score may diverge significantly
> from VM1 by the 3h mark — this is the desired diversity effect.

### Check Stage B / C reports once Stage A ends
```powershell
foreach ($vm in @(@{IP=$IP1; name="VM1"}, @{IP=$IP2; name="VM2"})) {
    Write-Host "=== $($vm.name) run dir contents ==="
    & $plink -batch -i $ppk -l $env:USERNAME $($vm.IP) `
      'RD=$(sudo ls -t /root/amm-challenge/Cursor/runs/ | head -1); sudo ls /root/amm-challenge/Cursor/runs/$RD/' 2>&1
}
# Look for: stage_a_report.json, stage_b_report.json, stage_c_report.json, promoted_best.sol
```

---

## CRITICAL — Emergency: VM network hang

If SSH timeouts persist for >5 minutes on either VM:

```powershell
$g = "C:\Program Files (x86)\Google\Cloud SDK\google-cloud-sdk\bin\gcloud.cmd"
$HUNG_VM = "amm-opt-1"  # or "amm-opt-2"

# 1. Confirm VM is still RUNNING
& $g compute instances describe $HUNG_VM --zone=europe-central2-b --project=ammopt `
  --format="value(status)" 2>&1

# 2. Write no-op startup script to prevent pipeline auto-relaunch on reboot
$noop = @'
#!/bin/bash
exec > /tmp/recovery_startup.log 2>&1
echo "recovery boot: $(date -u)"
echo "RECOVERY_BOOT_DONE" > /tmp/recovery_boot_done
'@
$noop | Out-File -FilePath "$env:TEMP\startup_noop.sh" -Encoding UTF8 -NoNewline
& $g compute instances add-metadata $HUNG_VM --zone=europe-central2-b --project=ammopt `
  "--metadata-from-file=startup-script=$env:TEMP\startup_noop.sh" 2>&1

# 3. Hard reset (preserves disk, kills all processes)
& $g compute instances reset $HUNG_VM --zone=europe-central2-b --project=ammopt 2>&1

# 4. Wait ~90s then SSH in — the OTHER VM continues running normally
```

> **Key advantage of 2-VM setup**: if one VM hangs, the other continues running.
> You still get a full 5h Stage A result from the surviving VM.

---

## Step 7 — Collect results from both VMs

### 7a — Collect from each VM individually

```powershell
$pscp = "C:\Program Files (x86)\Google\Cloud SDK\google-cloud-sdk\bin\sdk\pscp.exe"
$plink = "C:\Program Files (x86)\Google\Cloud SDK\google-cloud-sdk\bin\sdk\plink.exe"
$ppk   = "$env:USERPROFILE\.ssh\google_compute_engine.ppk"

foreach ($vm in @(@{IP=$IP1; name="vm1"}, @{IP=$IP2; name="vm2"})) {
    # Get run directory
    $RUN_DIR = (& $plink -batch -i $ppk -l $env:USERNAME $($vm.IP) `
      'sudo ls -t /root/amm-challenge/Cursor/runs/ | head -1' 2>&1 |
      Where-Object { $_ -match "theo1" }) -replace "\s",""

    if (-not $RUN_DIR) {
        Write-Host "$($vm.name): No theo1 run dir found — skipping"
        continue
    }

    # Check if pipeline passed
    $status = (& $plink -batch -i $ppk -l $env:USERNAME $($vm.IP) `
      "sudo cat /root/amm-challenge/Cursor/runs/$RUN_DIR/pipeline_result.json 2>/dev/null | python3 -c `"import sys,json; d=json.load(sys.stdin); print(d.get('status','unknown'))`" 2>/dev/null || echo not_done" 2>&1).Trim()
    Write-Host "$($vm.name) status: $status"

    if ($status -ne "passed_all_stages") {
        Write-Host "$($vm.name): Did not pass all stages — skipping download"
        continue
    }

    # Fix permissions and copy files
    & $plink -batch -i $ppk -l $env:USERNAME $($vm.IP) (
      "sudo mkdir -p /tmp/results && " +
      "sudo cp /root/amm-challenge/Cursor/runs/$RUN_DIR/promoted_best.sol " +
          "/root/amm-challenge/Cursor/runs/$RUN_DIR/pipeline_result.json " +
          "/root/amm-challenge/Cursor/runs/$RUN_DIR/stage_a_report.json " +
          "/root/amm-challenge/Cursor/runs/$RUN_DIR/stage_b_report.json " +
          "/root/amm-challenge/Cursor/runs/$RUN_DIR/stage_c_report.json /tmp/results/ && " +
      "sudo chmod 644 /tmp/results/* && echo READY"
    ) 2>&1

    # Download
    $local = "C:\Users\azhel\PycharmProjects\amm-challenge\Strat\gcp_$($vm.name)_${RUN_DIR}"
    New-Item -ItemType Directory -Force -Path $local | Out-Null
    foreach ($f in @("promoted_best.sol","pipeline_result.json","stage_a_report.json","stage_b_report.json","stage_c_report.json")) {
        & $pscp -batch -i $ppk `
          "${env:USERNAME}@$($vm.IP):/tmp/results/$f" "${local}\$f" 2>&1
    }
    Write-Host "$($vm.name) files saved to: $local"
}
```

### 7b — Pick the best champion

```powershell
# Read Stage C lcb95 from each VM's report
$results = @()
foreach ($vm in @("vm1", "vm2")) {
    $dirs = Get-ChildItem "C:\Users\azhel\PycharmProjects\amm-challenge\Strat\" |
            Where-Object { $_.Name -like "gcp_${vm}_*" } |
            Sort-Object LastWriteTime -Descending
    if ($dirs.Count -eq 0) { continue }
    $local = $dirs[0].FullName
    $reportPath = "$local\stage_c_report.json"
    if (-not (Test-Path $reportPath)) { continue }
    $sc = Get-Content $reportPath | ConvertFrom-Json
    $lcb = $sc.evaluation.summary.lcb95_mean_delta
    $results += [PSCustomObject]@{VM=$vm; LCB95=$lcb; Dir=$local}
    Write-Host "$vm Stage C lcb95 = $lcb"
}

$best = $results | Sort-Object LCB95 -Descending | Select-Object -First 1
Write-Host ""
Write-Host "Best result: $($best.VM) with lcb95=$($best.LCB95)"
Write-Host "(Previous champion lcb95=2.203)"

if ($best.LCB95 -gt 2.203) {
    $stamp = (Get-Date -Format "yyyyMMdd")
    $dest = "C:\Users\azhel\PycharmProjects\amm-challenge\Cursor\strategies\champions\theo1_v4_promoted_$stamp.sol"
    Copy-Item "$($best.Dir)\promoted_best.sol" $dest
    Write-Host "New champion saved: $dest"
} else {
    Write-Host "Neither VM beat previous champion (lcb95=2.203) — keeping theo1_v3"
}
```

---

## Step 8 — Stop both VMs

```powershell
$g = "C:\Program Files (x86)\Google\Cloud SDK\google-cloud-sdk\bin\gcloud.cmd"

$job1 = Start-Job { & "C:\Program Files (x86)\Google\Cloud SDK\google-cloud-sdk\bin\gcloud.cmd" `
  compute instances stop amm-opt-1 --zone=europe-central2-b --project=ammopt 2>&1 }
$job2 = Start-Job { & "C:\Program Files (x86)\Google\Cloud SDK\google-cloud-sdk\bin\gcloud.cmd" `
  compute instances stop amm-opt-2 --zone=europe-central2-b --project=ammopt 2>&1 }
Wait-Job $job1, $job2 | Out-Null
Receive-Job $job1; Receive-Job $job2

# Verify both stopped
foreach ($vm in @("amm-opt-1", "amm-opt-2")) {
    $s = (& $g compute instances describe $vm `
      --zone=europe-central2-b --project=ammopt `
      --format="value(status)" 2>&1).Trim()
    Write-Host "$vm status: $s"
}
# Expected: TERMINATED on both
```

> A stopped VM accrues zero compute cost.
> amm-opt-2's disk (amm-opt-2-disk) also accrues a small cost (~$2/month for 50GB SSD).
> If you don't plan to reuse amm-opt-2, delete it:
> ```powershell
> & $g compute instances delete amm-opt-2 --zone=europe-central2-b --project=ammopt --quiet 2>&1
> ```

---

## Step 9 — Report to human

```
=== GCP Cloud Run Summary (2-VM Parallel) ===
VM1: amm-opt-1 (n2-highcpu-48, step_pct=0.05, max_changes=3)
VM2: amm-opt-2 (n2-highcpu-48, step_pct=0.08, max_changes=4)
Duration: ~5.5h wall-clock
VMs status: STOPPED ✓

VM1 results:
  Stage A: PASS/FAIL | mean_delta=X.XX | p10_delta=X.XX
  Stage B: PASS/FAIL | lcb95=X.XX | p10=X.XX (20 seeds)
  Stage C: PASS/FAIL | lcb95=X.XX | p10=X.XX (48 seeds)

VM2 results:
  Stage A: PASS/FAIL | mean_delta=X.XX | p10_delta=X.XX
  Stage B: PASS/FAIL | lcb95=X.XX | p10=X.XX (20 seeds)
  Stage C: PASS/FAIL | lcb95=X.XX | p10=X.XX (48 seeds)

Best result: VM1/VM2, lcb95=X.XXX
vs previous champion (theo1_v3, 2026-02-19): lcb95=2.203 → X.XXX (better/worse/same)

Promoted file: Cursor/strategies/champions/theo1_v4_promoted_YYYYMMDD.sol  OR  N/A
```

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| `ENV_MISSING` on Step 2 | Disk preserved but venv needs a sanity check; run `python -c "import amm_sim_rs"` after activating `.venv` |
| VM2 env missing after snapshot | Snapshot may not have captured full venv; run `sudo bash /root/amm-challenge/Cursor/gcp_production_startup.sh` on VM2 |
| Workers stuck at `it=0` after 10 min | Base eval running (normal — takes ~3 min/worker); wait |
| Stage A gate fails | Increase `--a-hours` to 7 or check base strategy path |
| Stage B/C gate fails (`lcb95 <= 0`) | Candidate overfit; try `--a-quick-sims 8` (more filtering) or `--a-refine-sims 16` |
| SSH timeout / connection refused | Check: `Test-NetConnection $IP -Port 22`; if closed, VM may be overloaded (see Emergency section) |
| `pscp` permission denied | Files owned by root; run the `sudo cp ... /tmp/results/` step first |
| IP changed after start | Always re-read IP from `gcloud compute instances describe --format="value(...natIP)"` |
| amm-opt-2 disk name wrong | Check with: `gcloud compute disks list --project=ammopt --zones=europe-central2-b` |
| VM2 host key unknown | On first SSH: omit `-hostkey` flag — PuTTY caches automatically with `-batch` |

---

## Pipeline CLI Reference (key flags)

```bash
python Cursor/tools/run_theo1_staged_pipeline.py \
  --base-strategy Cursor/strategies/champions/theo1_v3_promoted_20260219.sol \
  --a-workers 48 \                # MUST be 48 on n2-highcpu-48 (not 60, not 56)
  --a-max-safe-workers 56 \       # soft headroom cap
  --a-sim-workers 1 \             # sim threads per worker (keep 1)
  --a-hours 5.0 \                 # Stage A search duration (5h with 2 VMs = ~same as 8h single)
  --a-step-pct 0.05 \             # VM1: conservative (0.05); VM2: aggressive (0.08)
  --a-max-changes 3 \             # VM1: 3; VM2: 4 — diagonal moves in parameter space
  --a-restart-prob 0.20 \         # prob of restarting from base (keep)
  --a-global-parent-prob 0.40 \   # prob of using cross-worker elite (keep)
  --a-mutable-constants "..."     # 30-parameter list (see Step 5)
```

### New parameters added to this run (8 unexplored dimensions):
```
DIR_DECAY,           # direction EMA lifetime (currently 0.80) — faster memory = more responsive
SIZE_BLEND_DECAY,    # large-trade size update speed (currently 0.818) — pairs with SIZE_SMALL_DECAY
TOX_DECAY,           # underlying toxicity state decay (currently 0.903) — faster tox fades?
ARB_RET_MIN,         # arb classifier price-return threshold (currently 33 bps)
ARB_TOX_MIN,         # arb classifier toxicity threshold (currently 25 bps)
GAP_GATE_PER_STEP,   # gate widening rate after no-trade gaps (currently 0.25/step)
TAIL_SLOPE_PROTECT,  # tail fee compression on protected side (currently 0.799) — allow asymmetry
TAIL_SLOPE_ATTRACT   # tail fee compression on attract side (currently 0.799) — allow asymmetry
```

---

## Key file paths

```
Local (Windows):
  Champions:   C:\Users\azhel\PycharmProjects\amm-challenge\Cursor\strategies\champions\
  Results dir: C:\Users\azhel\PycharmProjects\amm-challenge\Strat\gcp_<vm1|vm2>_<RUN_DIR>\
  Runbook:     C:\Users\azhel\PycharmProjects\amm-challenge\Cursor\cloudrun.md
  Journal:     C:\Users\azhel\PycharmProjects\amm-challenge\Cursor\research_journal.md

On VM (/root/amm-challenge/):
  Cursor/tools/run_theo1_staged_pipeline.py      # pipeline entrypoint
  Cursor/tools/optimize_theo1_local_parallel.py  # worker search engine
  Cursor/strategies/champions/                    # base strategies
  Cursor/runs/<RUN_DIR>/
    pipeline.log                                  # master event log
    pipeline_result.json                          # full final result
    promoted_best.sol                             # OUTPUT if all gates passed
    stage_a_report.json / stage_b_report.json / stage_c_report.json
    stage_a_search/workers/worker_N/
      stdout.log                                  # iteration-by-iteration progress
      progress.jsonl                              # structured event log
      best.json / best.sol                        # worker's best candidate
    stage_a_search/shared_pool/worker_N.json      # cross-worker elite sharing
  /tmp/pipeline_stdout.log                        # nohup stdout (often buffered/empty)
  /tmp/pipeline.pid                               # pipeline PID (may be wrong — see note)
```

---

## Gate criteria summary

| Stage | Seeds | Pass condition |
|-------|-------|---------------|
| A trigger | 5 (train) | `mean_delta >= 5.0` **OR** `p10_delta >= -10.0` |
| B gate | 20 | `lcb95_mean_delta > 0` **AND** `p10_delta >= -10.0` |
| C final | 48 | `lcb95_mean_delta > 0` **AND** `p10_delta >= -10.0` |

> Stage A trigger is very permissive — almost any positive result passes via the `p10 >= -10` condition.
> Stages B and C are the real statistical gates.

---

## Champion history

| Date | File | Stage C lcb95 | Notes |
|------|------|---------------|-------|
| 2026-02-17 | theo1_islandga10h_expanded_worker0_best_20260217.sol | −0.016 | FAILED gate |
| 2026-02-18 | theo1_v2_promoted_20260218.sol | +1.037 | First passing structural fix run |
| 2026-02-19 | **theo1_v3_promoted_20260219.sol** | **+2.203** | GCP 8h run, 48w — current champion |
| next | theo1_v4_promoted_YYYYMMDD.sol | target >2.5 | 2-VM parallel, 5h, 30 params |

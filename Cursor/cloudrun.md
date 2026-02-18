# Cloud Optimization Run — Agent Runbook

## Purpose
This document is a self-contained prompt for an autonomous agent to execute a **complete GCP optimization cycle** for the AMM Challenge Theo1 strategy:
VM creation → environment setup → pipeline launch → monitoring → result download → analysis → VM teardown.

Read this entire file before taking any action. Execute every step in order.

---

## Context: What This Project Is

- **Competition**: AMM (Automated Market Maker) fee-strategy optimization challenge.
- **Goal**: Maximize `mean_edge` — the average per-trade edge earned by the strategy across simulation seeds.
- **Strategy file**: A Solidity `.sol` file with embedded integer constants that are optimized numerically.
- **Pipeline**: `Cursor/tools/run_theo1_staged_pipeline.py` — a Python script that:
  - **Stage A**: Runs N parallel local-search workers (each tries random parameter mutations, keeps improvements). Duration: ~1 hour.
  - **Stage B**: Evaluates the best candidate found by Stage A against 20 holdout seeds. Gate: `lcb95_mean_delta > 0` AND `p10_delta >= -10`.
  - **Stage C**: Evaluates the same candidate against 30 holdout seeds (wider confidence interval). Gate: same rule.
  - On full pass → writes `promoted_best.sol` to the run directory.
- **Baseline strategy** (current champion): `Cursor/strategies/champions/theo1_v2_fixed_20260218.sol`
- **Baseline scores** (from local run `20260218T205008Z` — passed all gates):
  - Stage A: mean_delta=1.08, p10_delta=0.76
  - Stage B: lcb95=1.03, p10=0.88 (20 seeds)
  - Stage C: lcb95=1.04, p10=0.89 (30 seeds)
  - Worker best refine scores ranged 511.09–511.78

---

## Prerequisites (already done by the human)

- `gcloud` CLI installed and authenticated: `gcloud auth login` done
- GCP project created: **`ammopt`**
- Billing linked to project `ammopt`
- Compute Engine API enabled: `gcloud services enable compute.googleapis.com --project=ammopt`
- Repo: `https://github.com/jaoz/amm-challenge` (may be private — see Step 3)
- Active branch: `Search-powell-style`

---

## Step 1 — Create the VM

Run from local machine (PowerShell or bash with gcloud on PATH):

```bash
gcloud compute instances create amm-opt-1 \
  --project=ammopt \
  --zone=europe-west1-b \
  --machine-type=c2-standard-16 \
  --image-family=ubuntu-2204-lts \
  --image-project=ubuntu-os-cloud \
  --boot-disk-size=50GB \
  --boot-disk-type=pd-ssd \
  --metadata=enable-oslogin=FALSE
```

Verify it's running:
```bash
gcloud compute instances list --project=ammopt
```

Expected: instance `amm-opt-1` with status `RUNNING`.

**VM spec rationale**: c2-standard-16 = 16 vCPU, 64 GB RAM. Runs 14 parallel workers safely.
For 20+ workers use `c2-standard-30` (~2× cost).

---

## Step 2 — SSH into the VM

```bash
gcloud compute ssh amm-opt-1 --zone=europe-west1-b --project=ammopt
```

All subsequent commands in Steps 3–7 are run **inside this SSH session**.

---

## Step 3 — Install system dependencies

```bash
sudo apt-get update && sudo apt-get install -y \
  build-essential pkg-config libssl-dev git curl \
  python3.10 python3.10-venv python3.10-dev

# Rust toolchain (required for amm_sim_rs Rust extension)
curl https://sh.rustup.rs -sSf | sh -s -- -y --profile minimal
source "$HOME/.cargo/env"

# Verify
python3.10 --version   # must be 3.10.x
rustc --version        # must succeed
```

---

## Step 4 — Clone the repository

The GitHub PAT is stored in **GCP Secret Manager** under the secret name `github-pat` in project `ammopt`.
Fetch it at clone time — it is never written to disk.

```bash
# Fetch token from Secret Manager (requires VM service account has secretmanager.secretAccessor role)
TOKEN=$(gcloud secrets versions access latest --secret=github-pat --project=ammopt)

git clone https://$TOKEN@github.com/jaoz/amm-challenge.git
cd amm-challenge
git checkout Search-powell-style

unset TOKEN   # wipe from shell memory immediately after clone
```

Verify:
```bash
git log --oneline -3   # should show recent commits
ls Cursor/tools/run_theo1_staged_pipeline.py   # must exist
```

> **If the VM service account lacks access**, grant it from local machine first:
> ```powershell
> $SA = gcloud iam service-accounts list --project=ammopt --format="value(email)" | Select-Object -First 1
> gcloud secrets add-iam-policy-binding github-pat `
>   --member="serviceAccount:$SA" `
>   --role="roles/secretmanager.secretAccessor" `
>   --project=ammopt
> ```

---

## Step 5 — Build Python environment

```bash
cd ~/amm-challenge

python3.10 -m venv .venv
source .venv/bin/activate

pip install -U pip setuptools wheel maturin

# Build Rust extension (takes 2-4 min)
cd amm_sim_rs
maturin develop --release
cd ..

# Install project
pip install -e .

# Smoke test
python -c "import amm_sim_rs; print('Rust sim OK')"
python -c "import amm_competition; print('Python package OK')"
```

Both lines must print OK. If `amm_sim_rs` import fails, the Rust build failed — rerun `maturin develop --release` with `--verbose` to debug.

---

## Step 6 — Launch the pipeline

```bash
cd ~/amm-challenge
source .venv/bin/activate

# Use nohup so the run survives SSH disconnection
nohup python Cursor/tools/run_theo1_staged_pipeline.py \
  --a-workers 14 \
  --max-safe-workers 16 \
  --sim-workers 1 \
  --a-hours 1.0 \
  > ~/pipeline_stdout.log 2>&1 &

PIPELINE_PID=$!
echo "Pipeline PID: $PIPELINE_PID"
echo $PIPELINE_PID > ~/pipeline.pid
```

Immediately verify it started:
```bash
sleep 10
# Check process is alive
kill -0 $PIPELINE_PID && echo "Running" || echo "DIED"

# Find the run directory (created within seconds of launch)
ls -t Cursor/runs/ | head -3

# Should show a directory like: 20260219T103045Z_theo1_staged_pipeline
```

Note the run directory name — you'll need it for monitoring and result gathering.

---

## Step 7 — Monitor progress

### Check pipeline log (master status)
```bash
RUN_DIR=$(ls -t Cursor/runs/ | head -1)
tail -f Cursor/runs/$RUN_DIR/pipeline.log
```

Key log events to watch for:
- `stage_a_native_launch workers=14` — Stage A started OK
- `stage_a_worker_spawned worker_id=N pid=NNN` — all 14 workers confirmed (×14)
- `stage_a_native_complete` — all workers finished (~60 min after launch)
- `stage_a_trigger_result pass=True` — Stage A gate passed → Stage B begins
- `stage_b_result pass=True` — Stage B gate passed → Stage C begins
- `stage_c_result pass=True` — Stage C gate passed
- `pipeline_complete promoted=...promoted_best.sol` — **SUCCESS**

### Check worker progress (mid-run)
```bash
RUN_DIR=$(ls -t Cursor/runs/ | head -1)
# Show last line of each worker's stdout
for w in Cursor/runs/$RUN_DIR/stage_a_search/workers/worker_*/stdout.log; do
  echo "$(basename $(dirname $w)): $(tail -1 $w)"
done
```

### Check best scores across workers (mid-run or after Stage A)
```bash
RUN_DIR=$(ls -t Cursor/runs/ | head -1)
for f in Cursor/runs/$RUN_DIR/stage_a_search/workers/*/best.json; do
  w=$(basename $(dirname $f))
  score=$(python3.10 -c "import json; d=json.load(open('$f')); print(round(d.get('refine_score',0),3))")
  echo "$w: $score"
done | sort -t: -k2 -rn | head -5
```

### Tail live output
```bash
tail -f ~/pipeline_stdout.log
```

### Estimated timeline
- Stage A: ~60 min (14 workers searching in parallel)
- Stage B eval: ~4 min
- Stage C eval: ~12 min
- **Total: ~76 min**

---

## Step 8 — Gather results

After `pipeline_complete` appears in the log:

```bash
RUN_DIR=$(ls -t Cursor/runs/ | head -1)
echo "Run: $RUN_DIR"

# Confirm promoted file exists
ls -lh Cursor/runs/$RUN_DIR/promoted_best.sol

# Show all stage reports
echo "=== Stage A ===" && cat Cursor/runs/$RUN_DIR/stage_a_report.json
echo "=== Stage B ===" && cat Cursor/runs/$RUN_DIR/stage_b_report.json
echo "=== Stage C ===" && cat Cursor/runs/$RUN_DIR/stage_c_report.json
echo "=== Pipeline result ===" && cat Cursor/runs/$RUN_DIR/pipeline_result.json
```

### Download to local machine

Run these from **local machine** (new PowerShell terminal, not SSH):

```powershell
$ZONE = "europe-west1-b"
$PROJECT = "ammopt"
$VM = "amm-opt-1"

# You need to know the RUN_DIR — get it from the SSH session output above
$RUN_DIR = "20260219TXXXXXXX_theo1_staged_pipeline"   # REPLACE with actual

# Download promoted best
gcloud compute scp --zone=$ZONE --project=$PROJECT `
  "${VM}:/root/amm-challenge/Cursor/runs/${RUN_DIR}/promoted_best.sol" `
  "C:\Users\azhel\PycharmProjects\amm-challenge\Strat\gcp_${RUN_DIR}_promoted_best.sol"

# Download pipeline result JSON
gcloud compute scp --zone=$ZONE --project=$PROJECT `
  "${VM}:/root/amm-challenge/Cursor/runs/${RUN_DIR}/pipeline_result.json" `
  "C:\Users\azhel\PycharmProjects\amm-challenge\Strat\gcp_${RUN_DIR}_result.json"

# Download all stage reports
gcloud compute scp --zone=$ZONE --project=$PROJECT `
  "${VM}:/root/amm-challenge/Cursor/runs/${RUN_DIR}/stage_a_report.json" `
  "C:\Users\azhel\PycharmProjects\amm-challenge\Strat\gcp_${RUN_DIR}_stage_a.json"
gcloud compute scp --zone=$ZONE --project=$PROJECT `
  "${VM}:/root/amm-challenge/Cursor/runs/${RUN_DIR}/stage_b_report.json" `
  "C:\Users\azhel\PycharmProjects\amm-challenge\Strat\gcp_${RUN_DIR}_stage_b.json"
gcloud compute scp --zone=$ZONE --project=$PROJECT `
  "${VM}:/root/amm-challenge/Cursor/runs/${RUN_DIR}/stage_c_report.json" `
  "C:\Users\azhel\PycharmProjects\amm-challenge\Strat\gcp_${RUN_DIR}_stage_c.json"
```

---

## Step 9 — Analyze results

Parse and report the following from downloaded JSON files:

### Stage A summary
From `stage_a_report.json`:
- `evaluation.summary.mean_delta` — mean edge improvement over baseline
- `evaluation.summary.lcb95_mean_delta` — lower confidence bound
- `evaluation.summary.p10_delta` — 10th percentile improvement
- Gate: passed if `mean_delta >= 5.0 OR p10_delta >= -10.0`

### Stage B summary
From `stage_b_report.json` (20 holdout seeds):
- `evaluation.summary.mean_delta`
- `evaluation.summary.lcb95_mean_delta` — **primary gate metric** (must be > 0)
- `evaluation.summary.p10_delta` — must be >= -10
- Report min/max delta across all seeds

### Stage C summary
From `stage_c_report.json` (30 holdout seeds):
- Same metrics as Stage B
- This is the final confidence check before promotion

### Comparison to baseline (local run `20260218T205008Z`):
| Metric | Baseline local run | GCP run | Delta |
|--------|-------------------|---------|-------|
| Stage A mean_delta | 1.08 | ? | ? |
| Stage B lcb95 | 1.03 | ? | ? |
| Stage C lcb95 | 1.04 | ? | ? |

### Pass/Fail determination
- If all three stages show `"pass": true` → **PROMOTED**, `promoted_best.sol` is the new champion candidate.
- If any stage fails → report exact gate metric that failed and recommend whether to retry with different seed or longer Stage A hours.

---

## Step 10 — Stop the VM (IMPORTANT — do this immediately after results are downloaded)

```bash
# Option A: from SSH session (instant)
sudo poweroff
```

Or from local machine:
```powershell
gcloud compute instances stop amm-opt-1 --zone=europe-west1-b --project=ammopt
```

Verify stopped:
```powershell
gcloud compute instances list --project=ammopt
# Status must be TERMINATED, not RUNNING
```

A stopped VM accrues **zero compute cost**. Only resume if running another experiment.

---

## Step 11 — Report to human

After completing all steps, provide a structured summary:

```
=== GCP Cloud Run Summary ===
VM: amm-opt-1 (c2-standard-16, europe-west1-b)
Run directory: <RUN_DIR>
Duration: ~XX min
VM status: STOPPED ✓

Stage A: PASS/FAIL | mean_delta=X.XX | p10_delta=X.XX
Stage B: PASS/FAIL | lcb95=X.XX | p10=X.XX (20 seeds)
Stage C: PASS/FAIL | lcb95=X.XX | p10=X.XX (30 seeds)

Promoted file: <path to local .sol> or N/A

vs baseline (local 20260218T205008Z):
  Stage B lcb95: 1.03 → X.XX (better/worse/same)
  Stage C lcb95: 1.04 → X.XX (better/worse/same)

Recommendation: [one of]
  - "Use GCP promoted_best.sol as new champion — copy to Cursor/strategies/champions/"
  - "GCP result worse than local — keep current champion"
  - "Stage X failed (metric=X.XX) — retry with --a-hours 2.0 or more workers"
```

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| `maturin develop` fails | Check `rustc --version` works; try `cargo clean` in `amm_sim_rs/` then retry |
| `import amm_sim_rs` fails | Run `maturin develop --release --verbose` to see Rust compile errors |
| Pipeline dies immediately | Check `~/pipeline_stdout.log` for Python traceback |
| Workers all show `it=0` after 10 min | Base eval is running (normal — takes ~3.5 min per worker) |
| Stage A gate fails (`mean_delta < 5 AND p10 < -10`) | Very rare; retry with `--a-hours 2.0` or check base strategy path |
| Stage B/C gate fails (`lcb95 <= 0`) | Candidate overfit to train seeds; increase Stage A refine_sims or report to human |
| gcloud scp fails with permission error | VM might use `/home/USER/` not `/root/` — check actual home: `echo $HOME` in SSH |

---

## Key File Paths (on VM)

```
~/amm-challenge/
├── Cursor/
│   ├── tools/run_theo1_staged_pipeline.py   # pipeline entrypoint
│   ├── strategies/champions/                # base strategies
│   └── runs/<RUN_DIR>/
│       ├── pipeline.log                     # master log
│       ├── pipeline_result.json             # full final result
│       ├── promoted_best.sol                # OUTPUT if all gates passed
│       ├── stage_a_report.json
│       ├── stage_b_report.json
│       └── stage_c_report.json
├── ~/pipeline_stdout.log                    # nohup stdout
└── ~/pipeline.pid                           # pipeline PID
```

---

## Pipeline CLI Reference

```bash
python Cursor/tools/run_theo1_staged_pipeline.py \
  --a-workers 14 \          # parallel search workers (use cores-2 for safety)
  --max-safe-workers 16 \   # hard cap (set to VM vCPU count)
  --sim-workers 1 \         # sim threads per worker (keep 1)
  --a-hours 1.0 \           # Stage A search duration (increase for more iterations)
  --a-spawn-stagger-seconds 0.5   # delay between worker spawns
```

To maximize search depth: increase `--a-hours` to 2.0 (costs 2× time/money but ~2× iterations per worker).

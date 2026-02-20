#!/bin/bash
# Production startup script for n2-highcpu-48 (europe-central2-b)
# Runs after resize from smoke VM:
#   1. Skip install if env already exists (disk preserved from smoke phase)
#   2. Benchmark workers grid including oversubscription (workers > vCPUs)
#   3. Launch 8h pipeline with best worker count
#   4. CPU usage check at 5-min mark → recommendation
#   5. Wait for pipeline → write done file
set -e
exec > /tmp/production.log 2>&1
echo "=== production startup begin: $(date -u) ==="
export HOME=/root
export PATH="$HOME/.cargo/bin:$PATH"
VCPUS=$(nproc)
echo "vCPUs: $VCPUS"

# ── Install only if env missing (handles both fresh VM and post-resize) ────────
if [ ! -f "/root/amm-challenge/.venv/bin/activate" ]; then
  echo "=== env not found — building from scratch ==="
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -q
  apt-get install -y -q \
    build-essential pkg-config libssl-dev git curl jq \
    python3.10 python3.10-venv python3.10-dev

  curl https://sh.rustup.rs -sSf | sh -s -- -y --profile minimal
  source "$HOME/.cargo/env"

  ACCESS_TOKEN=$(curl -sf -H "Metadata-Flavor: Google" \
    "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token" \
    | jq -r '.access_token')
  [ -z "$ACCESS_TOKEN" ] || [ "$ACCESS_TOKEN" = "null" ] && { echo "ERROR: no access token"; exit 1; }

  PAT=$(curl -sf -H "Authorization: Bearer $ACCESS_TOKEN" \
    "https://secretmanager.googleapis.com/v1/projects/ammopt/secrets/github-pat/versions/latest:access" \
    | jq -r '.payload.data' | base64 -d | tr -d '\n\r')
  [ -z "$PAT" ] || [ "$PAT" = "null" ] && { echo "ERROR: no PAT"; exit 1; }

  git config --global credential.helper store
  printf 'https://oauth2:%s@github.com\n' "$PAT" > /root/.git-credentials
  git clone https://github.com/jaoz/amm-challenge.git /root/amm-challenge
  cd /root/amm-challenge
  git checkout Search-powell-style
  rm -f /root/.git-credentials
  git config --global --unset credential.helper
  unset PAT ACCESS_TOKEN

  python3.10 -m venv .venv
  source .venv/bin/activate
  pip install -U pip setuptools wheel maturin --quiet
  cd amm_sim_rs && maturin develop --release && cd ..
  pip install -e . --quiet
  echo "=== env build complete: $(date -u) ==="
else
  echo "=== env exists — skipping install ==="
  source "$HOME/.cargo/env" 2>/dev/null || true
fi

cd /root/amm-challenge
source .venv/bin/activate

# Sanity check
python -c "import amm_sim_rs; print('Rust sim OK')"
python -c "import amm_competition; print('Python package OK')"

# ── Benchmark: test worker counts INCLUDING oversubscription ─────────────────
# We deliberately test workers > vCPUs because Python workers have idle gaps
# between Rust sim calls — oversubscription can fill those gaps.
if   [ "$VCPUS" -le 8  ]; then GRID="4,5,6,7,8,10,12"
elif [ "$VCPUS" -le 16 ]; then GRID="10,12,14,16,18,20,22"
elif [ "$VCPUS" -le 32 ]; then GRID="20,24,28,32,36,40,44"
else                            GRID="32,40,44,48,52,56,60"   # 48 vCPU
fi
echo "=== Benchmark: vCPUs=$VCPUS grid=$GRID: $(date -u) ==="

python Cursor/tools/benchmark_theo1_worker_scaling.py \
  --base-strategy /root/amm-challenge/Cursor/strategies/champions/theo1_v2_fixed_20260218.sol \
  --workers-grid "$GRID" \
  --sim-workers-grid 1 \
  --hours 0.025 \
  --out-dir /tmp/bench_production \
  2>&1 | tee /tmp/bench_production.log

BEST_WORKERS=$(jq '.best.workers' /tmp/bench_production/benchmark_report.json)
echo "=== Benchmark complete. Best workers: $BEST_WORKERS ==="
jq '[.ranked[] | {workers, estimated_sims_per_min}]' /tmp/bench_production/benchmark_report.json \
  | tee /tmp/bench_ranked.txt

echo "BENCH_DONE workers=$BEST_WORKERS" > /tmp/bench_production_done

# ── Launch 8h pipeline ────────────────────────────────────────────────────────
MAX_SAFE=$((VCPUS + 20))   # allow headroom for oversubscription if benchmark finds it optimal
echo "=== Launching 8h pipeline: workers=$BEST_WORKERS max_safe=$MAX_SAFE: $(date -u) ==="

nohup python Cursor/tools/run_theo1_staged_pipeline.py \
  --base-strategy /root/amm-challenge/Cursor/strategies/champions/theo1_v2_promoted_20260218.sol \
  --a-workers "$BEST_WORKERS" \
  --a-max-safe-workers "$MAX_SAFE" \
  --a-sim-workers 1 \
  --a-hours 8.0 \
  > /tmp/pipeline_stdout.log 2>&1 &

PIPELINE_PID=$!
echo $PIPELINE_PID > /tmp/pipeline.pid
echo "PIPELINE_STARTED pid=$PIPELINE_PID" > /tmp/pipeline_started
echo "Pipeline PID: $PIPELINE_PID"

# ── CPU utilization check at 5-minute mark ───────────────────────────────────
echo "Sleeping 5 min before CPU check..."
sleep 300
echo "=== CPU check: $(date -u) ===" | tee /tmp/cpu_check.log
vmstat -w 1 10 | tee -a /tmp/cpu_check.log
echo "" | tee -a /tmp/cpu_check.log

# vmstat column 15 = idle %
CPU_IDLE=$(vmstat 1 6 | tail -5 | awk '{sum+=$15; n++} END {if(n>0) printf "%.1f", sum/n; else print 50}')
CPU_USED=$(echo "100 - $CPU_IDLE" | bc)
echo "CPU utilization: ${CPU_USED}%  (idle: ${CPU_IDLE}%)" | tee -a /tmp/cpu_check.log
echo "Running workers: $BEST_WORKERS on $VCPUS vCPUs" | tee -a /tmp/cpu_check.log

if awk "BEGIN {exit !($CPU_IDLE > 15)}"; then
  SUGGESTED=$((BEST_WORKERS + 8))
  echo "RECOMMENDATION: CPU underutilized (idle ${CPU_IDLE}%) — next run try --a-workers $SUGGESTED" \
    | tee -a /tmp/cpu_check.log
else
  echo "RECOMMENDATION: CPU well-utilized (idle ${CPU_IDLE}%) — workers=$BEST_WORKERS is optimal" \
    | tee -a /tmp/cpu_check.log
fi

echo "CPU_CHECK_DONE" > /tmp/cpu_check_done
echo "=== cpu check done: $(date -u) ==="

# ── Wait for pipeline to finish ───────────────────────────────────────────────
echo "Waiting for pipeline PID=$PIPELINE_PID ..."
wait $PIPELINE_PID || true
EXIT_CODE=$?
echo "=== pipeline exited: code=$EXIT_CODE: $(date -u) ===" | tee /tmp/production_done.txt

RUN_DIR=$(ls -t /root/amm-challenge/Cursor/runs/ | head -1)
echo "RUN_DIR=$RUN_DIR" | tee -a /tmp/production_done.txt

if [ -f "/root/amm-challenge/Cursor/runs/$RUN_DIR/promoted_best.sol" ]; then
  echo "STATUS=PROMOTED" | tee -a /tmp/production_done.txt
else
  echo "STATUS=NO_PROMOTION" | tee -a /tmp/production_done.txt
fi

# Print final stage reports inline so they're readable from the log
for stage in stage_a stage_b stage_c; do
  F="/root/amm-challenge/Cursor/runs/$RUN_DIR/${stage}_report.json"
  [ -f "$F" ] && echo "=== $stage ===" && jq '{pass,evaluation:.evaluation.summary}' "$F" || true
done

echo "PRODUCTION_DONE" | tee -a /tmp/production_done.txt
echo "=== all done: $(date -u) ==="

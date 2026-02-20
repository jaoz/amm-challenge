#!/bin/bash
# GCP startup script: set up environment and run worker-scaling benchmark
# Region: europe-central2-b  |  Default machine: n2-highcpu-4
set -e
exec > /tmp/startup.log 2>&1
echo "=== startup begin: $(date -u) ==="

export DEBIAN_FRONTEND=noninteractive
export HOME=/root

# --- System dependencies (jq required for reliable JSON/PAT parsing) ---
apt-get update -q
apt-get install -y -q \
  build-essential pkg-config libssl-dev git curl jq \
  python3.10 python3.10-venv python3.10-dev

echo "=== apt done: $(date -u) ==="

# --- Rust toolchain ---
curl https://sh.rustup.rs -sSf | sh -s -- -y --profile minimal
export PATH="$HOME/.cargo/bin:$PATH"
source "$HOME/.cargo/env"
echo "rustc: $(rustc --version)"
echo "python3.10: $(python3.10 --version)"
echo "jq: $(jq --version)"

echo "=== rust done: $(date -u) ==="

# --- Fetch GitHub PAT from Secret Manager via GCE metadata server ---
ACCESS_TOKEN=$(curl -sf -H "Metadata-Flavor: Google" \
  "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token" \
  | jq -r '.access_token')

echo "Access token length: ${#ACCESS_TOKEN}"
if [ -z "$ACCESS_TOKEN" ] || [ "$ACCESS_TOKEN" = "null" ]; then
  echo "ERROR: failed to fetch access token from metadata server"
  exit 1
fi

PAT=$(curl -sf -H "Authorization: Bearer $ACCESS_TOKEN" \
  "https://secretmanager.googleapis.com/v1/projects/ammopt/secrets/github-pat/versions/latest:access" \
  | jq -r '.payload.data' | base64 -d | tr -d '\n\r')

echo "PAT length: ${#PAT}"
if [ -z "$PAT" ] || [ "$PAT" = "null" ]; then
  echo "ERROR: failed to fetch PAT from Secret Manager"
  exit 1
fi

# --- Clone repo (use git credential store to avoid URL-encoding issues) ---
git config --global credential.helper store
printf 'https://oauth2:%s@github.com\n' "$PAT" > /root/.git-credentials
git clone https://github.com/jaoz/amm-challenge.git /root/amm-challenge
cd /root/amm-challenge
git checkout Search-powell-style
# Clean up credentials immediately
rm -f /root/.git-credentials
git config --global --unset credential.helper
unset PAT ACCESS_TOKEN

echo "=== repo cloned: $(date -u) ==="
git log --oneline -3

# --- Build Python environment ---
python3.10 -m venv .venv
source .venv/bin/activate
pip install -U pip setuptools wheel maturin --quiet

cd amm_sim_rs
maturin develop --release
cd ..

pip install -e . --quiet

echo "=== python env built: $(date -u) ==="

# --- Smoke test ---
python -c "import amm_sim_rs; print('Rust sim OK')"
python -c "import amm_competition; print('Python package OK')"

# --- Worker-scaling benchmark ---
# Adjust --workers-grid to match actual vCPU count of this VM:
#   n2-highcpu-4  → 2,3,4
#   n2-highcpu-8  → 4,5,6,7,8
#   n2-highcpu-16 → 8,10,12,14,16
#   n2-highcpu-32 → 14,16,20,24,28,32
VCPUS=$(nproc)
echo "vCPUs detected: $VCPUS"

if   [ "$VCPUS" -le 4  ]; then GRID="2,3,4"
elif [ "$VCPUS" -le 8  ]; then GRID="4,5,6,7,8"
elif [ "$VCPUS" -le 16 ]; then GRID="8,10,12,14,16"
else                            GRID="14,16,20,24,28,32"
fi
echo "Workers grid: $GRID"

# --hours 0.025 ≈ 90s per trial
echo "=== benchmark start: $(date -u) ==="

python Cursor/tools/benchmark_theo1_worker_scaling.py \
  --base-strategy /root/amm-challenge/Cursor/strategies/champions/theo1_v2_fixed_20260218.sol \
  --workers-grid "$GRID" \
  --sim-workers-grid 1 \
  --hours 0.025 \
  --out-dir /tmp/bench_results \
  2>&1 | tee /tmp/benchmark.log

echo "=== benchmark complete: $(date -u) ==="

# --- Print summary ---
BENCH_JSON="/tmp/bench_results/benchmark_report.json"
if [ -f "$BENCH_JSON" ]; then
  echo "=== BENCHMARK RESULTS ==="
  jq '{best: (.best | {workers, sim_workers, estimated_sims_per_min}),
       ranked: [.ranked[] | {workers, sim_workers, estimated_sims_per_min}]}' "$BENCH_JSON" \
    | tee /tmp/bench_summary.txt
fi

echo "STARTUP_DONE" > /tmp/bench_done
echo "=== all done: $(date -u) ==="

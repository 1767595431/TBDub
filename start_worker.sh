#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
# shellcheck source=server/procs.sh
source ./server/procs.sh
source /usr/local/miniconda3/etc/profile.d/conda.sh
conda activate tbdub
mkdir -p data
existing="$(py_module_pids server.worker | head -n 1 || true)"
if [[ -n "${existing}" ]]; then
  echo "worker already running pid=$existing"
  exit 0
fi
nohup python -m server.worker >> data/worker.log 2>&1 &
pid=$!
sleep 1
if [[ ! -d "/proc/$pid" ]]; then
  echo "worker failed to stay up pid=$pid" >&2
  exit 1
fi
echo "worker started pid=$pid"

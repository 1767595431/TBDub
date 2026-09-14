#!/usr/bin/env bash
# 只重启合成 Worker，不杀正在跑的 inference.py
set -euo pipefail
cd "$(dirname "$0")"
# shellcheck source=server/procs.sh
source ./server/procs.sh
while read -r pid; do
  kill -TERM "$pid" 2>/dev/null || true
done < <(py_module_pids server.worker)
for _ in $(seq 1 25); do
  if [[ -z "$(py_module_pids server.worker)" ]]; then
    break
  fi
  sleep 0.2
done
bash ./start_worker.sh

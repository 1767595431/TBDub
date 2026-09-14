#!/usr/bin/env bash
# 只重启接口，不动合成 Worker / 正在跑的 inference.py
set -euo pipefail
cd "$(dirname "$0")"
# shellcheck source=server/procs.sh
source ./server/procs.sh
while read -r pid; do
  kill -TERM "$pid" 2>/dev/null || true
done < <(api_pids)
sleep 1
source /usr/local/miniconda3/etc/profile.d/conda.sh
conda activate tbdub
exec python -m server

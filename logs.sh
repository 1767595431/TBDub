#!/usr/bin/env bash
# 实时日志。Ctrl+C 只退出查看，不停服务。
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p data
touch data/api.log data/worker.log
case "${1:-all}" in
  api)
    exec tail -n 100 -F data/api.log
    ;;
  worker)
    exec tail -n 100 -F data/worker.log
    ;;
  all|*)
    echo "=== data/api.log + data/worker.log  (Ctrl+C 退出) ==="
    exec tail -n 80 -F data/api.log data/worker.log
    ;;
esac

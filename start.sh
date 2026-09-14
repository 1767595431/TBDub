#!/usr/bin/env bash
# 后台启动接口 + Worker。关终端也继续跑。不杀正在合成的 inference.py。
set -euo pipefail
cd "$(dirname "$0")"
# shellcheck source=server/procs.sh
source ./server/procs.sh
mkdir -p data

api="$(api_pids | head -n 1 || true)"
if [[ -n "${api}" ]]; then
  echo "already running api=${api} worker=$(py_module_pids server.worker | head -n 1 || echo none)"
  echo "logs: bash logs.sh"
  echo "stop: bash stop.sh"
  exit 0
fi

setsid nohup bash ./start_server.sh >> data/api.log 2>&1 < /dev/null &
disown || true

for _ in $(seq 1 40); do
  if [[ -n "$(api_pids | head -n 1 || true)" ]]; then
    break
  fi
  sleep 0.25
done

api="$(api_pids | head -n 1 || true)"
worker="$(py_module_pids server.worker | head -n 1 || true)"
if [[ -z "${api}" ]]; then
  echo "api failed to start, see data/api.log" >&2
  exit 1
fi

echo "started api=${api} worker=${worker:-pending}"
echo "console: http://127.0.0.1:8811/"
echo "logs:    bash logs.sh"
echo "stop:    bash stop.sh"

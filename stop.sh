#!/usr/bin/env bash
# 关闭接口和 Worker。正在跑的 inference.py 留给 GPU 跑完，不要 pkill python。
set -euo pipefail
cd "$(dirname "$0")"
# shellcheck source=server/procs.sh
source ./server/procs.sh

term_list() {
  while read -r pid; do
    [[ -n "${pid}" ]] || continue
    kill -TERM "${pid}" 2>/dev/null || true
  done
}

infer="$(inference_pids | tr '\n' ' ')"
echo "stopping api + worker"
term_list < <(api_pids)
term_list < <(py_module_pids server.worker)

for _ in $(seq 1 30); do
  if [[ -z "$(api_pids)" && -z "$(py_module_pids server.worker)" ]]; then
    break
  fi
  sleep 0.2
done

api="$(api_pids | tr '\n' ' ')"
worker="$(py_module_pids server.worker | tr '\n' ' ')"
if [[ -n "${api}${worker}" ]]; then
  echo "still alive api=${api} worker=${worker}" >&2
  exit 1
fi

echo "stopped"
if [[ -n "${infer}" ]]; then
  echo "left inference.py running: ${infer}"
fi
echo "start: bash start.sh"
echo "logs:  bash logs.sh"

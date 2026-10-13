#!/usr/bin/env bash
# Download the ORIGINAL V1.0 weight set from the Hugging Face repo tag "v1.0"
# (via hf-mirror.com) into checkpoints/v1.0/.
set -uo pipefail

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib_shard.sh
source "$HERE/lib_shard.sh"

CKPT=/AI/TBDub-master/checkpoints
OUT="$CKPT/v1.0"
mkdir -p "$OUT"
cd "$OUT"

HFM=https://hf-mirror.com/TaoLiveAIGC/TBDub/resolve/v1.0

echo "===== V1.0 original weights (HF tag v1.0 via hf-mirror) ====="
# digests = the LFS oids reported by the HF tree API for tag v1.0
shard_retry_verified "$HFM/tbdub_base.safetensors"     tbdub_base.safetensors     12585231496 \
  78d0e9062fcae5f274b4f7a799283c6bed741d77c313f54093dcd90bd0ea2a78 8
shard_retry_verified "$HFM/tbdub_finetune.safetensors" tbdub_finetune.safetensors 10279804520 \
  65731a07b7f0daa9191c657df747b6aa4002ff03f586803df163508af39fdf60 8
shard_retry_verified "$HFM/tbdub_student.safetensors"  tbdub_student.safetensors  25181688152 \
  27d6933f0e3651e3d2485cd274a7736c0eb1d406de15855f7347e0e5e8d120ae 8
curl -sL --retry 10 --retry-delay 5 -C - -o config.json "$HFM/config.json"
echo "[get ] config.json $(stat -c%s config.json) bytes"

echo "===== done ====="
ls -l
echo "### V10_DONE"

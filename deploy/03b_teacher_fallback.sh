#!/usr/bin/env bash
# Fetch v1.1 tbdub_teacher.safetensors from a second source, verifying sha256.
# Used when the ModelScope copy cannot be reassembled to the published digest
# (observed: right SIZE, wrong CONTENT - a CDN serving bytes from the wrong offset).
set -uo pipefail
HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib_shard.sh
source "$HERE/lib_shard.sh"

CKPT=/AI/TBDub-master/checkpoints
cd "$CKPT"

SIZE=12591523040
SHA=e73483dfde3b960d3d22f85e356c378067ccd3cff81f3b668d92167b8de605d0

if [ -f tbdub_teacher.safetensors ] && [ "$(sha256_of tbdub_teacher.safetensors)" = "$SHA" ]; then
  echo "[skip] tbdub_teacher.safetensors already verified"
  exit 0
fi
rm -f tbdub_teacher.safetensors

echo "===== teacher from hf-mirror ====="
shard_retry_verified "https://hf-mirror.com/TaoLiveAIGC/TBDub/resolve/main/tbdub_teacher.safetensors" \
  tbdub_teacher.safetensors "$SIZE" "$SHA" 8 && exit 0

echo "===== hf-mirror failed, falling back to huggingface.co ====="
shard_retry_verified "https://huggingface.co/TaoLiveAIGC/TBDub/resolve/main/tbdub_teacher.safetensors" \
  tbdub_teacher.safetensors "$SIZE" "$SHA" 8

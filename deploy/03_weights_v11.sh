#!/usr/bin/env bash
# Download TBDub V1.1 weights + aux weights + DWPose weights.
# Sources: ModelScope (domestic) for the TBDub V1.1 DiT + hubert, the shared /model store
# for the Wan aux weights, openmmlab for the DWPose weights.
set -uo pipefail

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib_shard.sh
source "$HERE/lib_shard.sh"

PY=/usr/local/miniconda3/envs/tbdub/bin/python

CKPT=/AI/TBDub-master/checkpoints
DWPOSE=/AI/TBDub-master/dwpose_tools/models
LOGDIR=/var/log/tbdub
mkdir -p "$CKPT" "$DWPOSE" "$LOGDIR"
cd "$CKPT"

MS() { printf 'https://modelscope.cn/api/v1/models/%s/repo?Revision=master&FilePath=%s' "$1" "$2"; }

simple() {  # simple <url> <out>
  local url="$1" out="$2"
  if [ -f "$out" ] && [ -s "$out" ]; then echo "[skip] $out ($(stat -c%s "$out") bytes)"; return 0; fi
  curl -sL --retry 10 --retry-delay 5 --retry-all-errors -C - -o "$out" "$url"
  echo "[get ] $out $(stat -c%s "$out") bytes"
}

echo "===== 1. TBDub DiT checkpoints (ModelScope V1.1) ====="
# official digests, taken from the repo's config.json / README
shard_retry_verified "$(MS TaoLiveAIGC/TBDub tbdub_student.safetensors)" tbdub_student.safetensors 12591523048 \
  4e86900f9744b3650bfe58451ae89b3afba758233a3edf5947516268c1ea11e6 8
shard_retry_verified "$(MS TaoLiveAIGC/TBDub tbdub_teacher.safetensors)" tbdub_teacher.safetensors 12591523040 \
  e73483dfde3b960d3d22f85e356c378067ccd3cff81f3b668d92167b8de605d0 8

echo "===== 2. shared small files ====="
simple "$(MS TaoLiveAIGC/TBDub null_prompt_emb.pt)" null_prompt_emb.pt
simple "$(MS TaoLiveAIGC/TBDub config.json)" config.json

echo "===== 3. hubert-large-ll60k (ModelScope facebook) ====="
mkdir -p hubert-large-ll60k
simple "$(MS facebook/hubert-large-ll60k config.json)" hubert-large-ll60k/config.json
simple "$(MS facebook/hubert-large-ll60k preprocessor_config.json)" hubert-large-ll60k/preprocessor_config.json
shard_retry "$(MS facebook/hubert-large-ll60k pytorch_model.bin)" hubert-large-ll60k/pytorch_model.bin 1261920069 4

# transformers 5.x refuses to load .bin via torch.load unless torch >= 2.6 (CVE-2025-32434):
#   ValueError: ... we now require users to upgrade torch to at least v2.6 ...
# safetensors is exempt, and the production machine's hubert dir also holds model.safetensors.
if [ ! -f hubert-large-ll60k/model.safetensors ]; then
  echo "--- converting hubert pytorch_model.bin -> model.safetensors ---"
  "$PY" - <<'PY'
import os, torch
from safetensors.torch import save_file
src = "hubert-large-ll60k/pytorch_model.bin"
dst = "hubert-large-ll60k/model.safetensors"
sd = torch.load(src, map_location="cpu", weights_only=True)
save_file({k: v.clone().contiguous() for k, v in sd.items()}, dst)
print("wrote", dst, os.path.getsize(dst), "bytes,", len(sd), "tensors")
PY
fi

echo "===== 4. Wan aux weights from shared /model store ====="
WAN=/model/HuggingFace/Wan-AI
copy_if_needed() {  # <src> <dst>
  if [ -f "$2" ] && [ "$(stat -c%s "$1")" = "$(stat -c%s "$2")" ]; then
    echo "[skip] $2 (same size as source)"
  else
    cp -f "$1" "$2"
    echo "[copy] $2"
  fi
}
copy_if_needed "$WAN/Wan2.2-TI2V-5B/Wan2.2_VAE.pth" Wan2.2_VAE.pth
copy_if_needed "$WAN/Wan2.1-T2V-1.3B/models_t5_umt5-xxl-enc-bf16.pth" models_t5_umt5-xxl-enc-bf16.pth
if [ -f umt5-xxl/spiece.model ] && [ -f umt5-xxl/tokenizer.json ]; then
  echo "[skip] umt5-xxl/ already present"
else
  rm -rf umt5-xxl
  cp -r "$WAN/Wan2.1-T2V-1.3B/google/umt5-xxl" umt5-xxl
fi
ls -l Wan2.2_VAE.pth models_t5_umt5-xxl-enc-bf16.pth umt5-xxl

echo "===== 5. DWPose weights (openmmlab) ====="
cd "$DWPOSE"
curl -sL --retry 10 --retry-delay 5 -C - -o yolox_l_8x8_300e_coco_20211126_140236-d3bd2b23.pth \
  https://download.openmmlab.com/mmdetection/v2.0/yolox/yolox_l_8x8_300e_coco/yolox_l_8x8_300e_coco_20211126_140236-d3bd2b23.pth
curl -sL --retry 10 --retry-delay 5 -C - -o rtmw-x_simcc-cocktail14_pt-ucoco_270e-384x288-f840f204_20231122.pth \
  https://download.openmmlab.com/mmpose/v1/projects/rtmw/rtmw-x_simcc-cocktail14_pt-ucoco_270e-384x288-f840f204_20231122.pth
ls -l

echo "===== 6. hardlink to the filenames the code expects ====="
cd "$CKPT"
ln -f tbdub_student.safetensors tbdub_base.safetensors
ln -f tbdub_teacher.safetensors tbdub_finetune.safetensors
ls -l tbdub_base.safetensors tbdub_finetune.safetensors tbdub_student.safetensors tbdub_teacher.safetensors

echo "===== 7. layout ====="
du -sh "$CKPT"
ls -l "$CKPT"
echo "### MODELS_DONE"

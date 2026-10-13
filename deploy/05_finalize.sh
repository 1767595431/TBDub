#!/usr/bin/env bash
# Step 5: put the weights in their final layout, wire up the filenames the code
# hardcodes, and write config.yaml.
#
#   checkpoints/
#     v1.1/   tbdub_teacher.safetensors  tbdub_student.safetensors  config.json
#     v1.0/   tbdub_base.safetensors     tbdub_finetune.safetensors  tbdub_student.safetensors  config.json
#     tbdub_base.safetensors      hardlink -> the active version
#     tbdub_finetune.safetensors  hardlink -> the active version
#     tbdub_student.safetensors   hardlink -> the active version
#
# Usage: bash 05_finalize.sh [v11|v10]
set -uo pipefail

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

REPO=/AI/TBDub-master
CKPT="$REPO/checkpoints"
ACTIVE="${1:-v11}"

cd "$CKPT"
mkdir -p v1.1 v1.0

# V1.1 files land at the top level first; tuck them into v1.1/
[ -f tbdub_teacher.safetensors ] && [ ! -f v1.1/tbdub_teacher.safetensors ] && mv tbdub_teacher.safetensors v1.1/
[ -f tbdub_student.safetensors ] && [ ! -f v1.1/tbdub_student.safetensors ] && mv tbdub_student.safetensors v1.1/
[ -f config.json ] && [ ! -f v1.1/config.json ] && cp config.json v1.1/config.json

echo "--- weight sets present ---"
ls -l v1.1 v1.0 2>/dev/null

bash "$HERE/switch_weights.sh" "$ACTIVE"

echo "--- shared auxiliary files ---"
ls -l null_prompt_emb.pt hubert-large-ll60k 2>/dev/null | head -6

echo "--- config.yaml ---"
if [ -f "$REPO/config.yaml" ]; then
  echo "config.yaml already exists, left untouched"
else
  cp "$REPO/config.example.yaml" "$REPO/config.yaml"
  sed -i "s|^admin_key:.*|admin_key: King|" "$REPO/config.yaml"
  sed -i "s|^python_bin:.*|python_bin: /usr/local/miniconda3/envs/tbdub/bin/python|" "$REPO/config.yaml"
  echo "config.yaml written from config.example.yaml (admin_key=King, python_bin=conda abspath)"
fi
grep -E "^(admin_key|python_bin|device|port|mock_synthesis|default_steps):" "$REPO/config.yaml"

echo "--- code-visible checkpoint paths ---"
ls -l tbdub_base.safetensors tbdub_finetune.safetensors tbdub_student.safetensors
echo "### FINALIZE_DONE active=$ACTIVE"

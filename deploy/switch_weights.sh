#!/usr/bin/env bash
# Point the three filenames the code hardcodes (tbdub_base / tbdub_finetune /
# tbdub_student) at either the V1.1 or the V1.0 weight set. Hardlinks: no extra
# disk, instant switch.
#
# Why this works with zero code changes:
#   diffsynth loads --dit-checkpoint as a LIST, merges the state dicts in order
#   (later file wins) and then load_state_dict(strict=True). server/worker.py passes
#     student mode:  [tbdub_base, tbdub_student]   -> student model wins
#     teacher mode:  [tbdub_base, tbdub_finetune]  -> teacher model wins
#   V1.1 ships COMPLETE models with the same key set as V1.0, so a complete
#   checkpoint can fill either slot; the base slot just gets re-read.
#
# Usage: bash switch_weights.sh [v11|v10]
set -euo pipefail

CKPT="${TBDUB_CHECKPOINT_DIR:-/AI/TBDub-master/checkpoints}"
VER="${1:-v11}"
cd "$CKPT"

case "$VER" in
  v11|1.1|v1.1)
    SRC=v1.1
    BASE="$SRC/tbdub_student.safetensors"
    FINETUNE="$SRC/tbdub_teacher.safetensors"
    STUDENT="$SRC/tbdub_student.safetensors"
    ;;
  v10|1.0|v1.0)
    SRC=v1.0
    BASE="$SRC/tbdub_base.safetensors"
    FINETUNE="$SRC/tbdub_finetune.safetensors"
    STUDENT="$SRC/tbdub_student.safetensors"
    ;;
  *)
    echo "usage: bash switch_weights.sh [v11|v10]" >&2
    exit 2
    ;;
esac

for f in "$BASE" "$FINETUNE" "$STUDENT"; do
  if [ ! -f "$f" ]; then
    echo "missing $CKPT/$f - download the $SRC weight set first" >&2
    exit 1
  fi
done

ln -f "$BASE"     tbdub_base.safetensors
ln -f "$FINETUNE" tbdub_finetune.safetensors
ln -f "$STUDENT"  tbdub_student.safetensors

echo "active weight set: $SRC"
ls -l tbdub_base.safetensors tbdub_finetune.safetensors tbdub_student.safetensors

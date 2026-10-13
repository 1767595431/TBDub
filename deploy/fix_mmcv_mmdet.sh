#!/usr/bin/env bash
# Make the DWPose stack importable on this box (torch 2.4.1+cu121, but only nvcc 13.2).
#
# Why this is needed
# ------------------
# * mmcv==2.1.0 (the version requirements.txt / 部署安装文档.md pin) has prebuilt wheels
#   only up to torch 2.1. The cu121/torch2.1 wheel *installs* fine but its compiled
#   extension cannot load under torch 2.4:
#       ImportError: mmcv/_ext...so: undefined symbol: _ZN2at4_ops10zeros_like4call...
#   and there is no torch2.4 build of 2.1.0 on the OpenMMLab index.
# * Building mmcv 2.1.0 from source needs nvcc 12.1 to match torch cu121; this image
#   only ships CUDA 13.2, and torch.utils.cpp_extension refuses the version mismatch.
# * mmcv 2.2.0 *does* ship a cu121/torch2.4 wheel.
#
# So: install mmcv 2.2.0 and relax mmdet's version guard, which is stricter than the
# documented OpenMMLab compatibility range (mmdet 3.3.0 supports mmcv 2.0.0rc4 - 2.2.x;
# mmdet 3.2.0/mmdet 3.3.0 both hard-code mmcv_maximum_version = '2.2.0' with a `<` test,
# which rejects 2.2.0 itself). mmdet 3.3.0 is the newest release on PyPI.
#
# If you need the exact pinned pair (mmcv 2.1.0 + mmdet 3.2.0), install a CUDA 12.1
# toolkit and build mmcv from source instead:
#     CUDA_HOME=/usr/local/cuda-12.1 pip install --no-build-isolation mmcv==2.1.0
set -uo pipefail

source /usr/local/miniconda3/etc/profile.d/conda.sh
conda activate tbdub
export PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple

MMDET_INIT=/usr/local/miniconda3/envs/tbdub/lib/python3.10/site-packages/mmdet/__init__.py

echo "--- mmcv with working ops ---"
if python -c "import mmcv.ops" 2>/dev/null; then
  echo "mmcv.ops already OK: $(python -c 'import mmcv;print(mmcv.__version__)')"
else
  pip install "mmcv==2.2.0" -f https://download.openmmlab.com/mmcv/dist/cu121/torch2.4/index.html
fi

echo "--- mmdet released against mmcv 2.2.0 ---"
if python -c "import mmdet" 2>/dev/null; then
  echo "mmdet already imports: $(python -c 'import mmdet;print(mmdet.__version__)')"
else
  pip install "mmdet==3.3.0"
  sed -i "s/^mmcv_maximum_version = '2.2.0'/mmcv_maximum_version = '2.3.0'  # relaxed for mmcv 2.2.0 (cu121\/torch2.4 wheel); see deploy\/README.md/" "$MMDET_INIT"
  echo "relaxed mmdet ceiling:"; grep -n "^mmcv_maximum_version" "$MMDET_INIT"
fi

echo "--- verify DWPose stack ---"
python - <<'PY'
import mmcv, mmcv.ops, mmdet, mmpose
from mmdet.apis import init_detector
from mmpose.apis import init_model, inference_topdown
import torch
print("mmcv", mmcv.__version__, "| mmdet", mmdet.__version__, "| mmpose", mmpose.__version__)
print("cuda", torch.cuda.is_available(), torch.cuda.get_device_name(0))
print("DWPose stack OK")
PY
echo "### MMCV_FIX_DONE"

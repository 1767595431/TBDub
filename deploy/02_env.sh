#!/usr/bin/env bash
# Build conda env "tbdub" for TBDub: python 3.10 + torch 2.4.1+cu121 + project deps + mmcv.
# All pip traffic goes through domestic mirrors.
set -uo pipefail

export PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
export PIP_DEFAULT_TIMEOUT=120
export PIP_DISABLE_PIP_VERSION_CHECK=1
# Domestic mirrors must NOT go through the cluster HTTP proxy; foreign hosts (pytorch.org) still do.
export no_proxy="${no_proxy},mirrors.aliyun.com,mirrors.tuna.tsinghua.edu.cn,pypi.tuna.tsinghua.edu.cn,modelscope.cn,download.openmmlab.com"
export NO_PROXY="$no_proxy"

source /usr/local/miniconda3/etc/profile.d/conda.sh
REPO=/AI/TBDub-master

step() { echo; echo "########## $* ##########"; date -u +%H:%M:%S; }

step "1/6 conda create -n tbdub python=3.10"
if conda env list | awk '{print $1}' | grep -qx tbdub; then
  echo "conda env tbdub already exists"
else
  conda create -n tbdub python=3.10 -y || { echo "### ENV_FAIL conda-create"; exit 1; }
fi
conda activate tbdub
python -V

step "2/6 PyTorch 2.4.1 + torchvision 0.19.1 (cu121)"
if python -c "import torch,sys;sys.exit(0 if torch.__version__.startswith('2.4.1') and torch.version.cuda=='12.1' else 1)" 2>/dev/null; then
  echo "torch 2.4.1+cu121 already installed"
else
  pip install --upgrade pip
  # The linux wheel for torch 2.4.1 on PyPI is the cu121 build (it pulls nvidia-*-cu12 12.1 deps),
  # so the domestic PyPI mirror can serve the whole thing without touching the HTTP proxy.
  # The pytorch.org index stays as a fallback.
  pip install torch==2.4.1 torchvision==0.19.1 \
    || pip install torch==2.4.1 torchvision==0.19.1 --index-url https://download.pytorch.org/whl/cu121 \
    || { echo "### ENV_FAIL torch"; exit 1; }
fi

step "3/6 verify torch vs GPU"
python -c "import torch;print('torch',torch.__version__,'cuda',torch.version.cuda,'avail',torch.cuda.is_available(),torch.cuda.get_device_name(0))" \
  || echo "### WARN torch cuda check failed"

step "4/6 project requirements"
cd "$REPO"
# mmpose depends on chumpy, whose setup.py does `import pip`. Build isolation hides pip,
# so chumpy must be built without isolation (numpy + setuptools are already in the env).
python -c "import chumpy" 2>/dev/null || pip install --no-build-isolation chumpy || echo "### WARN chumpy"
pip install -r requirements.txt || { echo "### ENV_FAIL requirements"; exit 1; }

step "5/6 server requirements"
pip install -r requirements-server.txt || { echo "### ENV_FAIL requirements-server"; exit 1; }

step "6/6 mmcv + mmdet for the DWPose stack"
# mmcv 2.1.0 has no torch2.4 wheel; its torch2.1 wheel loads but is ABI-broken under
# torch 2.4. See fix_mmcv_mmdet.sh for the full explanation.
bash "$(dirname -- "$0")/fix_mmcv_mmdet.sh"

step "extra: openmim (kept for parity with the deployment doc)"
pip install -U openmim || true

step "VERIFY imports"
python - <<'PY'
import importlib, torch
for m in ["torch","torchvision","fastapi","uvicorn","yaml","cv2","diffusers","transformers",
          "safetensors","einops","scipy","skimage","ftfy","modelscope","sentencepiece",
          "mmengine","mmcv","mmdet","mmpose","imageio","imageio_ffmpeg","ffmpeg"]:
    try:
        mod = importlib.import_module(m)
        print("OK  ", m, getattr(mod, "__version__", ""))
    except Exception as e:
        print("FAIL", m, repr(e)[:140])
print("cuda:", torch.cuda.is_available(), torch.cuda.get_device_name(0))
PY

echo "===== project import smoke test ====="
cd "$REPO"
python -c "import sys; sys.path.insert(0,'.'); import diffsynth; print('diffsynth ok')" || echo "### WARN diffsynth import"
echo "### ENV_DONE"

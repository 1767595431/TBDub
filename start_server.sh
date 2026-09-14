#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
source /usr/local/miniconda3/etc/profile.d/conda.sh
conda activate tbdub
bash ./start_worker.sh
exec python -m server

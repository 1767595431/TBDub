#!/usr/bin/env bash
# Overwrite the TBDub code from a downloaded GitHub zip, keeping runtime state intact.
#
#   bash /root/tbdub-deploy/06_update_code.sh /root/TBDub-master.zip
#
# What it protects (these live in .gitignore and must survive an update):
#   checkpoints/   ~60GB of verified weights (incl. the v1.0/v1.1 sets + hardlinks)
#   data/          SQLite library, avatars, finished clips
#   config.yaml    this box's admin_key / python_bin / device
#
# It stops the service first (a half-updated tree under a running uvicorn is how you get
# "ready=true but every task fails"), snapshots the old code, moves the three protected
# items out of the way (instant rename, same filesystem), copies the new tree in, moves
# them back, then restarts and re-checks /api/system/ready.
set -euo pipefail

ZIP="${1:-}"
[ -n "$ZIP" ] || { echo "usage: bash 06_update_code.sh <path-to-zip>" >&2; exit 2; }
[ -f "$ZIP" ] || { echo "no such file: $ZIP" >&2; exit 2; }

REPO=/AI/TBDub-master
STAMP=$(date -u +%Y%m%d-%H%M%S)
WORK=/root/tbdub-update-$STAMP
KEEP=/root/tbdub-keep-$STAMP

cd "$REPO"
WAS_RUNNING=0
if pgrep -f -- "-m server" >/dev/null 2>&1; then WAS_RUNNING=1; fi

echo "--- 1/6 stop service (inference.py is left alone, by design) ---"
bash stop.sh 2>&1 | tail -2 || true
sleep 1

echo "--- 2/6 snapshot current code ---"
tar czf "/root/tbdub-code-backup-$STAMP.tar.gz" -C /AI \
  --exclude="TBDub-master/checkpoints" --exclude="TBDub-master/data" \
  --exclude="TBDub-master/dwpose_tools/models/*.pth" --exclude="__pycache__" TBDub-master
ls -lh "/root/tbdub-code-backup-$STAMP.tar.gz"

echo "--- 3/6 unzip ---"
mkdir -p "$WORK"
unzip -q "$ZIP" -d "$WORK"
SRC="$WORK"
if [ "$(find "$WORK" -maxdepth 1 -mindepth 1 | wc -l)" = "1" ] && [ -d "$(find "$WORK" -maxdepth 1 -mindepth 1 | head -1)" ]; then
  SRC="$(find "$WORK" -maxdepth 1 -mindepth 1 | head -1)"   # TBDub-master/ or TBDub-main/
fi
echo "unpacked from: $SRC"
ls "$SRC" | head -20

echo "--- 4/6 protect weights / data / config, then overwrite ---"
mkdir -p "$KEEP"
for p in checkpoints data config.yaml; do
  [ -e "$REPO/$p" ] && mv "$REPO/$p" "$KEEP/$p"
done
# copy the new tree over the old one (no deletes: stale files are reported at the end)
cp -a "$SRC"/. "$REPO"/
for p in checkpoints data config.yaml; do
  [ -e "$KEEP/$p" ] && mv "$KEEP/$p" "$REPO/$p"
done
rm -rf "$KEEP"
echo "protected items restored:"; ls -ld checkpoints data config.yaml

echo "--- 5/6 what the new code expects for checkpoints ---"
grep -rhoE "tbdub_(base|finetune|student|teacher)[a-z0-9_.]*\.safetensors" \
  inference.py server/*.py 2>/dev/null | sort -u
echo "--- what is actually on disk ---"
ls -l checkpoints/tbdub_*.safetensors 2>/dev/null | awk '{print $NF}'
echo "--- archived versions present ---"
ls -d checkpoints/v1.0 checkpoints/v1.1 2>/dev/null

echo "--- 6/6 restart + ready check ---"
if [ "$WAS_RUNNING" = "1" ]; then
  bash start.sh 2>&1 | tail -3
  sleep 10
  curl -s --max-time 20 http://127.0.0.1:8811/api/system/ready || echo "(ready check failed - see data/api.log)"
else
  echo "service was not running; start it with: cd $REPO && bash start.sh"
fi

echo
echo "backup of the previous code: /root/tbdub-code-backup-$STAMP.tar.gz"
echo "unpacked copy:               $WORK"
echo "### UPDATE_DONE"

#!/usr/bin/env bash
# Last-resort fetcher for mirrors that sometimes ignore `Range` and start streaming the
# whole object (which yields a file of the right SIZE and wrong CONTENT when you shard).
#
# aria2c validates the Content-Range of every parallel request and verifies the whole-file
# sha256 at the end, so a bad mirror surfaces as a failed download instead of silent
# corruption. Use it when shard_retry_verified keeps failing on a single object.
#
#   bash 03c_fetch_aria2.sh teacher     # v1.1 tbdub_teacher.safetensors (hf-mirror)
#   bash 03c_fetch_aria2.sh v10student  # v1.0 tbdub_student.safetensors (hf-mirror)
set -uo pipefail

which aria2c >/dev/null || { echo "aria2c missing: apt-get install -y aria2" >&2; exit 1; }

HFM=https://hf-mirror.com/TaoLiveAIGC/TBDub/resolve
V11=main
V10=v1.0

case "${1:-}" in
  teacher)
    cd /AI/TBDub-master/checkpoints
    url="$HFM/$V11/tbdub_teacher.safetensors"
    out=tbdub_teacher.safetensors
    sha=e73483dfde3b960d3d22f85e356c378067ccd3cff81f3b668d92167b8de605d0
    ;;
  v10student)
    cd /AI/TBDub-master/checkpoints/v1.0
    url="$HFM/$V10/tbdub_student.safetensors"
    out=tbdub_student.safetensors
    sha=27d6933f0e3651e3d2485cd274a7736c0eb1d406de15855f7347e0e5e8d120ae
    ;;
  *)
    echo "usage: bash 03c_fetch_aria2.sh [teacher|v10student]" >&2; exit 2 ;;
esac

if [ -f "$out" ] && [ "$(sha256sum "$out" | awk '{print $1}')" = "$sha" ]; then
  echo "[skip] $out size+sha256 verified"; exit 0
fi

rm -f "$out" "$out".aria2
rm -f "$out".part* 2>/dev/null

CONNS="${CONNS:-8}"
echo "[get ] $out via aria2c  x$CONNS  $(date -u +%H:%M:%S)"
aria2c -x"$CONNS" -s"$CONNS" -k1M -c \
  --file-allocation=none \
  --auto-file-renaming=false --allow-overwrite=true \
  --max-tries=20 --retry-wait=5 --timeout=60 --connect-timeout=20 \
  --check-integrity=true --checksum="sha-256=$sha" \
  --summary-interval=60 \
  -o "$out" "$url"
rc=$?
echo "[done] rc=$rc  $(date -u +%H:%M:%S)"
ls -l "$out" 2>/dev/null
exit $rc

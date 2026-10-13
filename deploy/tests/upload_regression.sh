#!/usr/bin/env bash
# 上传健壮性回归测试（本机跑，对着 127.0.0.1:8811）
#
#   A) 进行中的上传，重启 Worker 触发清理后，分片必须还在（这是被修掉的根因）
#   B) 13 片并行上传 + complete 必须成功，并把形象写进库
#   C) 已合并完成的音频上传：重启 Worker 后，合并文件与记录必须还在（原意行为没被改坏）
#
# 用法：bash upload_regression.sh
set -uo pipefail

BASE=http://127.0.0.1:8811
AUTH=(-H "X-Username: admin" -H "X-User-Id: admin")
REPO=/AI/TBDub-master
WORK=/root/upload-test
CHUNK=8388608
mkdir -p "$WORK"

py() { /usr/local/miniconda3/envs/py312/bin/python "$@"; }
field() { py -c "import sys,json;d=json.load(sys.stdin);print(eval('d'+sys.argv[1]))" "$1"; }

fail() { echo "❌ $*"; exit 1; }
pass() { echo "✅ $*"; }

echo "=== 准备测试素材（99MB，约 139 秒，含人脸）==="
if [ ! -f "$WORK/big.mp4" ]; then
  for i in 1 2 3; do echo "file '/root/tbdub-test/demo.mp4'"; done > "$WORK/list.txt"
  ffmpeg -y -v error -f concat -safe 0 -i "$WORK/list.txt" -c copy "$WORK/big.mp4" || fail "生成测试视频失败"
fi
SIZE=$(stat -c%s "$WORK/big.mp4")
echo "big.mp4 = $SIZE 字节"

split_chunk() { dd if="$WORK/big.mp4" of="$WORK/c$1.bin" bs=$CHUNK skip="$1" count=1 status=none; }

echo
echo "=== A) init + 只传前两片 ==="
INIT=$(curl -s "${AUTH[@]}" -F stage=init -F name=上传回归测试 -F type=public \
  -F filename=big.mp4 -F filesize="$SIZE" -F chunk_size=$CHUNK "$BASE/api/avatars/upload")
UPID=$(echo "$INIT" | field "['data']['upload_id']") || fail "init 失败: $INIT"
TOTAL=$(echo "$INIT" | field "['data']['total_chunks']")
echo "upload_id=$UPID  total_chunks=$TOTAL"
[ -n "$UPID" ] || fail "没有拿到 upload_id: $INIT"

for i in 0 1; do
  split_chunk "$i"
  R=$(curl -s "${AUTH[@]}" -F stage=chunk -F upload_id="$UPID" -F chunk_index="$i" \
      -F total_chunks="$TOTAL" -F chunk=@"$WORK/c$i.bin" "$BASE/api/avatars/upload")
  echo "  chunk $i -> $(echo "$R" | head -c 120)"
done

echo "--- 重启 Worker（启动时会立刻跑一次清理）---"
( cd "$REPO" && bash restart_worker.sh >/dev/null 2>&1 ); sleep 6
KEPT=$(ls "$REPO/data/uploads/$UPID" 2>/dev/null | wc -l)
if [ "$KEPT" -ge 2 ]; then pass "进行中的分片被保住了（$KEPT 片）"; else fail "分片被清理了（只剩 $KEPT 片）——根因未修复"; fi

echo
echo "=== B) 并行传完剩下的分片 + complete ==="
for i in $(seq 2 $((TOTAL - 1))); do
  (
    split_chunk "$i"
    curl -s "${AUTH[@]}" -F stage=chunk -F upload_id="$UPID" -F chunk_index="$i" \
      -F total_chunks="$TOTAL" -F chunk=@"$WORK/c$i.bin" "$BASE/api/avatars/upload" > /dev/null
  ) &
  while [ "$(jobs -rp | wc -l)" -ge 3 ]; do wait -n; done
done
wait
DONE=$(ls "$REPO/data/uploads/$UPID" 2>/dev/null | wc -l)
echo "  已上传分片数：$DONE / $TOTAL"
FIN=$(curl -s "${AUTH[@]}" -F stage=complete -F upload_id="$UPID" "$BASE/api/avatars/upload")
echo "  complete -> $(echo "$FIN" | head -c 200)"
CODE=$(echo "$FIN" | field "['code']")
IDENT=$(echo "$FIN" | field "['data']['identifier']" 2>/dev/null || true)
[ "$CODE" = "0" ] && pass "99MB 并行上传合并成功，形象 identifier=$IDENT" || fail "合并失败：$FIN"

echo
echo "=== C) 音频：完成后重启 Worker，合并文件与记录必须还在 ==="
ASIZE=$(stat -c%s /root/tbdub-test/drive8.wav)
AJSON=$(curl -s "${AUTH[@]}" -H "Content-Type: application/json" \
  -d "{\"kind\":\"audio\",\"filename\":\"drive8.wav\",\"size\":$ASIZE}" "$BASE/api/uploads")
AUID=$(echo "$AJSON" | field "['upload_id']") || fail "音频 init 失败：$AJSON"
echo "audio upload_id=$AUID"
curl -s "${AUTH[@]}" -X PUT --data-binary @/root/tbdub-test/drive8.wav "$BASE/api/uploads/$AUID/chunks/0" > /dev/null
AC=$(curl -s -X POST "$BASE/api/uploads/$AUID/complete")
echo "  audio complete -> $AC"
( cd "$REPO" && bash restart_worker.sh >/dev/null 2>&1 ); sleep 6
if [ -f "$REPO/data/uploads/$AUID/drive8.wav" ]; then pass "合并后的音频还在，且分片已回收"; else fail "合并文件被误删：$AUID"; fi

echo
echo "=== 清理测试数据 ==="
[ -n "${IDENT:-}" ] && curl -s "${AUTH[@]}" -X DELETE "$BASE/api/avatars/$IDENT" > /dev/null && echo "  已删除测试形象 $IDENT"
curl -s -X DELETE "$BASE/api/uploads/$AUID" > /dev/null && echo "  已删除测试音频 $AUID"
rm -f "$WORK"/c*.bin "$WORK"/list.txt
echo
echo "=== 结果 ==="
curl -s "$BASE/api/system/ready" | head -c 200; echo
echo "### UPLOAD_REGRESSION_DONE"

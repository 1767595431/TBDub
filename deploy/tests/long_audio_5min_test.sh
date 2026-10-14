#!/usr/bin/env bash
# 5 分钟音频验证：造 300 秒音频 → 走服务接口上传 → 用形象 58002866 建一条 Student 任务
# （Student 档生成很快，10 分钟左右就能验证"解码成片那一步是否还会 OOM"）
set -uo pipefail
BASE=http://127.0.0.1:8811
REPO=/AI/TBDub-master
SRC="$REPO/data/tasks/749dfb7a29f0a22e2de58a3d9d0cf0a5/gen_20261014_112812_476851.wav"   # 批量测试3 的音频(196.19s)
WORK=/root/longtest
CHUNK=8388608
mkdir -p "$WORK"
py() { /usr/local/miniconda3/envs/tbdub/bin/python "$@"; }
AUTH=(-H "X-Username: King" -H "X-User-Id: King")

[ -f "$SRC" ] || { echo "找不到源音频 $SRC"; exit 1; }

echo "=== 1) 造 300 秒音频 ==="
ffmpeg -y -v error -stream_loop 2 -i "$SRC" -t 300 -ar 16000 -ac 1 "$WORK/long300.wav"
SIZE=$(stat -c%s "$WORK/long300.wav"); echo "long300.wav = $SIZE 字节"
echo -n "时长: "; ffprobe -v error -show_entries format=duration -of csv=p=0 "$WORK/long300.wav"

echo "=== 2) 上传 ==="
INIT=$(curl -s "${AUTH[@]}" -H "Content-Type: application/json" \
  -d "{\"kind\":\"audio\",\"filename\":\"long300.wav\",\"size\":$SIZE}" "$BASE/api/uploads")
UPID=$(echo "$INIT" | py -c "import sys,json;print(json.load(sys.stdin).get('upload_id',''))")
[ -n "$UPID" ] || { echo "init 失败: $INIT"; exit 1; }
CHUNK=$(echo "$INIT" | py -c "import sys,json;print(json.load(sys.stdin).get('chunk_size') or 8388608)")
TOTAL=$(echo "$INIT" | py -c "import sys,json;print(json.load(sys.stdin).get('total_chunks') or 1)")
echo "upload_id=$UPID  分片大小=$CHUNK  分片数=$TOTAL"
rm -f "$WORK"/c*.bin
for i in $(seq 0 $((TOTAL - 1))); do
  dd if="$WORK/long300.wav" of="$WORK/c$i.bin" bs=$CHUNK skip="$i" count=1 status=none
  curl -s "${AUTH[@]}" -X PUT --data-binary @"$WORK/c$i.bin" "$BASE/api/uploads/$UPID/chunks/$i" > /dev/null
done
DONE=$(curl -s -X POST "$BASE/api/uploads/$UPID/complete"); echo "complete -> $DONE"

echo "=== 3) 建任务（Student 档 steps=50）==="
CREATE=$(curl -s "${AUTH[@]}" -F "user_id=King" -F "task_name=5分钟验证-Student" \
  -F "avatar_identifier=58002866" -F "steps=50" -F "audio_upload_id=$UPID" "$BASE/api/tasks/create")
echo "$CREATE" | head -c 300; echo
TID=$(echo "$CREATE" | py -c "import sys,json;print(json.load(sys.stdin).get('data',{}).get('task_id',''))" 2>/dev/null || true)
[ -n "$TID" ] || { echo "建任务失败"; exit 1; }
echo "$TID" > "$WORK/task_id.txt"
echo "task_id=$TID"
echo "=== 4) 等 40 秒看 worker 实际用的命令（关键：有没有 --cpu-offload）==="
sleep 40
LOG="$REPO/data/tasks/$TID/task.log"
if [ -f "$LOG" ]; then grep -m1 -A 2 "run-start" "$LOG" | tr -d '\r'; else echo "(日志还没生成，稍后再看)"; fi
echo "=== 5) 当前显存 ==="
nvidia-smi --query-gpu=utilization.gpu,memory.used,memory.total --format=csv,noheader
echo "### LONG_AUDIO_TEST_STARTED task=$TID"

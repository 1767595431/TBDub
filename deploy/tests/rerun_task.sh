#!/usr/bin/env bash
# 用指定的音频 + 形象重新建一条任务（等价于"重跑某条历史任务"）
# 用法: bash rerun_task.sh <音频文件> <steps:50|80> <形象id> <任务名>
set -uo pipefail
BASE=http://127.0.0.1:8811
AUDIO="${1:?音频文件}"; STEPS="${2:?50 或 80}"; AVATAR="${3:?形象 id}"; NAME="${4:?任务名}"
WORK=/root/longtest; mkdir -p "$WORK"
py() { /usr/local/miniconda3/envs/tbdub/bin/python "$@"; }
AUTH=(-H "X-Username: King" -H "X-User-Id: King")
SIZE=$(stat -c%s "$AUDIO")

INIT=$(curl -s "${AUTH[@]}" -H "Content-Type: application/json" \
  -d "{\"kind\":\"audio\",\"filename\":\"$(basename "$AUDIO")\",\"size\":$SIZE}" "$BASE/api/uploads")
UPID=$(echo "$INIT" | py -c "import sys,json;print(json.load(sys.stdin).get('upload_id',''))")
CH=$(echo "$INIT" | py -c "import sys,json;print(json.load(sys.stdin).get('chunk_size') or 2097152)")
TOTAL=$(echo "$INIT" | py -c "import sys,json;print(json.load(sys.stdin).get('total_chunks') or 1)")
[ -n "$UPID" ] || { echo "init 失败: $INIT"; exit 1; }
echo "上传: $(basename "$AUDIO")  $SIZE 字节  $TOTAL 片 x $CH"
rm -f "$WORK"/r*.bin
for i in $(seq 0 $((TOTAL - 1))); do
  dd if="$AUDIO" of="$WORK/r$i.bin" bs=$CH skip="$i" count=1 status=none
  curl -s "${AUTH[@]}" -X PUT --data-binary @"$WORK/r$i.bin" "$BASE/api/uploads/$UPID/chunks/$i" > /dev/null
done
curl -s -X POST "$BASE/api/uploads/$UPID/complete" | head -c 200; echo

CREATE=$(curl -s "${AUTH[@]}" -F "user_id=King" -F "task_name=$NAME" \
  -F "avatar_identifier=$AVATAR" -F "steps=$STEPS" -F "audio_upload_id=$UPID" "$BASE/api/tasks/create")
echo "$CREATE" | head -c 250; echo
py -c "
import json,sys
d=json.loads('''$CREATE''')
print('task_id =', d.get('data',{}).get('task_id'))
" 2>/dev/null || true

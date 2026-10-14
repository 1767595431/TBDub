#!/usr/bin/env bash
# 对比官方引擎树与本地引擎树（忽略 CRLF/LF），只列真正有改动的文件
# 用法: bash compare_engine.sh [官方树] [本地树]
NEW="${1:-/root/official-v11/TBDub-master}"
OLD="${2:-/AI/TBDub-master}"

norm() { tr -d '\r' < "$1" 2>/dev/null | md5sum | cut -d' ' -f1; }
same=0; changed=0; added=0
for pat in diffsynth inference.py preprocessing.py video_utils.py dwpose_tools requirements.txt; do
  while IFS= read -r f; do
    rel="${f#"$NEW"/}"
    if [ ! -e "$OLD/$rel" ]; then
      echo "【新增】 $rel"; added=$((added + 1)); continue
    fi
    if [ "$(norm "$f")" = "$(norm "$OLD/$rel")" ]; then
      same=$((same + 1))
    else
      echo "【改动】 $rel"; changed=$((changed + 1))
    fi
  done < <(find "$NEW/$pat" -type f \( -name "*.py" -o -name "*.yaml" -o -name "*.txt" \) 2>/dev/null)
done
echo "---- 内容相同: $same   真正改动: $changed   新增: $added ----"

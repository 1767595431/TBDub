#!/usr/bin/env python
"""任务体检：最近的任务、失败原因、task.log 尾部、整机是否重启过。只读。"""
import sqlite3
import subprocess
import time
from pathlib import Path

REPO = Path("/AI/TBDub-master")
DATA = REPO / "data"


def main() -> None:
    db = sqlite3.connect(str(DATA / "app.sqlite"))
    db.row_factory = sqlite3.Row
    rows = list(db.execute("SELECT * FROM tasks ORDER BY created_at DESC LIMIT 8"))

    print("=== 最近任务 ===")
    for r in rows:
        d = dict(r)
        print(f"- {d['task_id'][:8]}  {d.get('task_name')!r}  status={d['status']}  "
              f"mode={d.get('mode')}  steps={d.get('steps')}  progress={d.get('progress')}  "
              f"audio={d.get('audio_duration')}")
        print(f"    created={d.get('created_at')}  started={d.get('started_at')}  "
              f"finished={d.get('finished_at')}  pid={d.get('pid')}")
        if d.get("error_message"):
            print(f"    ERROR: {d['error_message']}")

    if rows:
        tid = rows[0]["task_id"]
        log = DATA / "tasks" / tid / "task.log"
        print(f"\n=== 最新任务 {tid[:8]} 的 task.log ===")
        if log.exists():
            st = log.stat()
            text = log.read_text(encoding="utf-8", errors="ignore")
            lines = text.splitlines()
            runs = [l for l in lines if "run-start" in l]
            print(f"大小={st.st_size}B  mtime={time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(st.st_mtime))} UTC")
            print(f"run-start 共 {len(runs)} 次: {runs[-2:]}")
            clips = [l for l in lines if l.startswith("[TBDub] clip")]
            print(f"片段日志: {clips[:3]} ... {clips[-2:] if len(clips) > 3 else ''}")
            print("--- 尾 20 行 ---")
            for l in lines[-20:]:
                print("   ", l[:150])
        else:
            print("   (无 task.log)")

    print("\n=== 整机是否重启过 ===")
    print("   开机时间:", subprocess.run(["uptime", "-s"], capture_output=True, text=True).stdout.strip())
    print("   ", subprocess.run(["uptime"], capture_output=True, text=True).stdout.strip())


if __name__ == "__main__":
    main()

from __future__ import annotations

import fcntl
import os
import re
import signal
import subprocess
import sys
import threading
import time
from datetime import timedelta
from pathlib import Path
from typing import Callable

from . import media
from .config import load_config
from .db import Store
from .util import (
    current_run_log,
    eta_seconds,
    iso,
    last_step_tqdm,
    parse_iso,
    remaining_from_log,
    remaining_from_stage,
    remaining_text,
    utc_now,
    audio_duration_limit_message,
    video_duration_limit_message,
)

CLIP_RE = re.compile(r"\[TBDub\]\s+clip\s+(\d+)\s*/\s*(\d+)", re.I)
CLIP_MSG_RE = re.compile(r"片段\s*(\d+)\s*/\s*(\d+)")
DWPOSE_RE = re.compile(r"dwpose\s+(\d+)/(\d+)\s+frames", re.I)
HEARTBEAT_NAME = "worker.heartbeat"
PIDFILE_NAME = "worker.pid"
HEARTBEAT_TTL = 12.0


def heartbeat_path(data_dir: Path) -> Path:
    return Path(data_dir) / HEARTBEAT_NAME


def pidfile_path(data_dir: Path) -> Path:
    return Path(data_dir) / PIDFILE_NAME


def worker_alive(data_dir: Path) -> bool:
    path = heartbeat_path(data_dir)
    if not path.exists():
        return False
    try:
        return (time.time() - path.stat().st_mtime) < HEARTBEAT_TTL
    except OSError:
        return False


def running_task(store: Store) -> dict | None:
    rows = store.query("SELECT * FROM tasks WHERE status='run' ORDER BY started_at ASC")
    if not rows:
        return None
    lives = {pid: inference_sample_name(pid) for pid in iter_inference_pids()}
    for row in rows:
        if is_inference_pid(row.get("pid"), row["task_id"]):
            return row
        if row["task_id"] in lives.values():
            return row
    return rows[0]


def is_worker_pid(pid: int | None) -> bool:
    if not pid_alive(pid):
        return False
    comm = process_comm(int(pid))
    if not (comm.startswith("python") or comm.startswith("pt_")):
        return False
    cmd = process_cmdline(int(pid))
    return "-m server.worker" in cmd


def worker_process_alive(data_dir: Path) -> bool:
    path = pidfile_path(data_dir)
    if path.exists():
        try:
            pid = int((path.read_text(encoding="utf-8") or "0").strip() or 0)
        except ValueError:
            pid = 0
        if is_worker_pid(pid):
            return True
    return any(is_worker_pid(int(entry.name)) for entry in Path("/proc").iterdir() if entry.name.isdigit())


def remaining_for(
    task: dict,
    has_preprocess: bool | None = None,
    avatar_duration: float | None = None,
    data_dir: Path | None = None,
) -> float | None:
    if task["status"] != "run":
        return None
    message = str(task.get("progress_message") or "")
    progress = float(task.get("progress") or 0)
    ready = has_preprocess
    if ready is None:
        ready = (
            progress >= 18
            or "复用形象预处理" in message
            or "预处理已保存" in message
            or CLIP_MSG_RE.search(message) is not None
        )
    tid = task.get("task_id")
    root = Path(data_dir) if data_dir else Path(__file__).resolve().parent.parent / "data"
    if tid:
        live = remaining_from_log(current_run_log(root / "tasks" / tid / "task.log"), task)
        if live is not None:
            return live
    return remaining_from_stage(task, has_preprocess=bool(ready), avatar_duration=avatar_duration)


def duration_text(task: dict) -> str:
    remain = remaining_for(task)
    return remaining_text(remain, task.get("progress")) if remain is not None else ""


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        stat = Path(f"/proc/{int(pid)}/stat").read_text(encoding="utf-8", errors="ignore")
        state = stat.split(")")[-1].split()[0]
        if state == "Z":
            return False
        os.kill(int(pid), 0)
        return True
    except (OSError, IndexError):
        return False


def process_cmdline(pid: int) -> str:
    try:
        return Path(f"/proc/{int(pid)}/cmdline").read_bytes().replace(b"\x00", b" ").decode("utf-8", "ignore")
    except OSError:
        return ""


def process_comm(pid: int) -> str:
    try:
        return Path(f"/proc/{int(pid)}/comm").read_text(encoding="utf-8", errors="ignore").strip()
    except OSError:
        return ""


def inference_sample_name(pid: int) -> str | None:
    parts = process_cmdline(int(pid)).split()
    if "--sample-name" in parts:
        index = parts.index("--sample-name")
        if index + 1 < len(parts):
            return parts[index + 1]
    return None


def is_inference_pid(pid: int | None, task_id: str | None = None) -> bool:
    if not pid_alive(pid):
        return False
    # PyTorch 起来后 comm 会变成 pt_main_thread，不能只看进程名
    parts = process_cmdline(int(pid)).split()
    if not parts:
        return False
    exe = Path(parts[0]).name
    if not (exe.startswith("python") or process_comm(int(pid)).startswith(("python", "pt_"))):
        return False
    if not any(part == "inference.py" or part.endswith("/inference.py") for part in parts):
        return False
    if task_id and task_id not in parts:
        return False
    return True


def iter_inference_pids() -> list[int]:
    proc_root = Path("/proc")
    if not proc_root.exists():
        return []
    pids: list[int] = []
    for entry in proc_root.iterdir():
        if entry.name.isdigit() and is_inference_pid(int(entry.name)):
            pids.append(int(entry.name))
    return pids


def kill_pid(pid: int | None, timeout: float = 8.0) -> None:
    if not pid or not pid_alive(pid) or int(pid) == os.getpid():
        return
    pid = int(pid)
    try:
        pgid = os.getpgid(pid)
    except OSError:
        return
    use_group = pgid == pid and pgid != os.getpgrp()
    try:
        if use_group:
            os.killpg(pid, signal.SIGTERM)
        else:
            os.kill(pid, signal.SIGTERM)
    except OSError:
        return
    deadline = time.time() + timeout
    while time.time() < deadline and pid_alive(pid):
        time.sleep(0.2)
    if not pid_alive(pid):
        return
    try:
        if use_group:
            os.killpg(pid, signal.SIGKILL)
        else:
            os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


def purge_task(store: Store, data_dir: Path, task: dict) -> None:
    folder = Path(data_dir) / "tasks" / task["task_id"]
    _rmtree(folder)
    store.delete("tasks", "task_id", task["task_id"])


def purge_upload(store: Store, data_dir: Path, upload_id: str) -> None:
    folder = Path(data_dir) / "uploads" / upload_id
    _rmtree(folder)
    store.delete("uploads", "upload_id", upload_id)


def _rmtree(folder: Path) -> None:
    if not folder.exists():
        return
    for child in folder.rglob("*"):
        if child.is_file():
            child.unlink(missing_ok=True)
    for child in sorted(folder.rglob("*"), reverse=True):
        if child.is_dir():
            child.rmdir()
    if folder.exists():
        folder.rmdir()


class Workers:
    def __init__(self, cfg: dict, store: Store, data_dir: Path, log: Callable[[str], None] | None = None):
        self.cfg = cfg
        self.store = store
        self.data_dir = Path(data_dir)
        self.log = log or (lambda _msg: None)
        self._stop = threading.Event()
        self._gpu_lock = threading.Lock()
        self._current_task: str | None = None
        self._proc: subprocess.Popen[str] | None = None
        self._threads: list[threading.Thread] = []
        self._pid_fd = None
        self._clips: dict[str, tuple[int, int]] = {}

    def start(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._lock_pidfile()
        self._reclaim_live_inference()
        self._reap_dead_runs()
        self._kill_orphan_inference()
        self.store.execute(
            "UPDATE avatars SET bake_status='processing', bake_message='等待转码' "
            "WHERE bake_status='processing'"
        )
        for target in (self._heartbeat_loop, self._bake_loop, self._synth_loop, self._cleanup_loop):
            thread = threading.Thread(target=target, name=target.__name__, daemon=True)
            thread.start()
            self._threads.append(thread)
        self.log(f"worker started pid={os.getpid()}")

    def run_forever(self) -> None:
        self.start()

        def _handle(signum, _frame) -> None:
            self.log(f"worker received signal {signum}, leaving synthesis process running")
            self._stop.set()

        signal.signal(signal.SIGTERM, _handle)
        signal.signal(signal.SIGINT, _handle)
        while not self._stop.wait(1.0):
            pass

    def stop(self, kill_synthesis: bool = False) -> None:
        self._stop.set()
        if kill_synthesis:
            proc = self._proc
            if proc and proc.poll() is None:
                kill_pid(proc.pid)
        # 默认不杀合成进程：接口或 Worker 退出后由新 Worker 接管

    @property
    def gpu_busy(self) -> bool:
        return running_task(self.store) is not None

    @property
    def current_task(self) -> str | None:
        row = running_task(self.store)
        return row["task_id"] if row else None

    @property
    def alive(self) -> bool:
        return worker_alive(self.data_dir)

    def cancel_task(self, task_id: str) -> None:
        row = self.store.one("SELECT pid FROM tasks WHERE task_id=?", (task_id,))
        pid = row["pid"] if row else None
        if self._current_task == task_id and self._proc and self._proc.poll() is None:
            kill_pid(self._proc.pid)
        elif pid:
            kill_pid(int(pid))

    def remaining_for(self, task: dict) -> float | None:
        return remaining_for(task)

    def duration_text(self, task: dict) -> str:
        return duration_text(task)

    def _purge_task(self, task: dict) -> None:
        purge_task(self.store, self.data_dir, task)

    def _purge_upload(self, upload_id: str) -> None:
        purge_upload(self.store, self.data_dir, upload_id)

    def _lock_pidfile(self) -> None:
        path = pidfile_path(self.data_dir)
        self._pid_fd = path.open("w")
        try:
            fcntl.flock(self._pid_fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise SystemExit("合成 Worker 已在运行") from exc
        self._pid_fd.write(str(os.getpid()))
        self._pid_fd.flush()

    def _heartbeat_loop(self) -> None:
        path = heartbeat_path(self.data_dir)
        while not self._stop.is_set():
            path.write_text(str(int(time.time())), encoding="utf-8")
            self._stop.wait(2.0)

    def _reclaim_live_inference(self) -> None:
        for pid in iter_inference_pids():
            sample = inference_sample_name(pid)
            if not sample:
                continue
            row = self.store.one("SELECT * FROM tasks WHERE task_id=?", (sample,))
            if not row:
                continue
            result = self.data_dir / "tasks" / sample / f"{sample}_tbdub.mp4"
            if result.exists():
                continue
            if row["status"] in {"run", "error", "wait"}:
                self.store.update("tasks", "task_id", sample, {
                    "status": "run",
                    "pid": pid,
                    "error_message": "",
                    "finished_at": None,
                    "progress_message": row.get("progress_message") or "正在合成",
                })
                if row["status"] != "run" or int(row.get("pid") or 0) != pid:
                    self.log(f"reclaimed synthesis pid={pid} task={sample}")

    def _reap_dead_runs(self) -> None:
        for task in self.store.query("SELECT * FROM tasks WHERE status='run'"):
            if is_inference_pid(task.get("pid"), task["task_id"]):
                continue
            live = next(
                (pid for pid in iter_inference_pids() if inference_sample_name(pid) == task["task_id"]),
                None,
            )
            if live:
                self.store.update("tasks", "task_id", task["task_id"], {"pid": live})
                continue
            try:
                self._recover_run(task)
            except Exception as exc:
                now = iso(utc_now())
                self.store.update("tasks", "task_id", task["task_id"], {
                    "status": "error",
                    "progress_message": "合成失败",
                    "error_message": str(exc),
                    "finished_at": now,
                    "retain_from": now,
                    "pid": None,
                })
                self._append_log(task["task_id"], f"ERROR {exc}\n")

    def _result_file(self, task_id: str) -> Path:
        return self.data_dir / "tasks" / task_id / f"{task_id}_tbdub.mp4"

    def _live_pid_for_task(self, task_id: str) -> int | None:
        for pid in iter_inference_pids():
            if inference_sample_name(pid) == task_id:
                return pid
        return None

    def _protect_inference(self, pid: int) -> bool:
        sample = inference_sample_name(pid)
        if not sample:
            return False
        row = self.store.one("SELECT status FROM tasks WHERE task_id=?", (sample,))
        if not row or row["status"] == "cancelled":
            return False
        if row["status"] == "done" and self._result_file(sample).exists():
            return False
        return True

    def _kill_orphan_inference(self) -> None:
        for pid in iter_inference_pids():
            if pid == os.getpid() or self._protect_inference(pid):
                continue
            self.log(f"killing orphan inference pid={pid}")
            kill_pid(pid)

    def _bake_loop(self) -> None:
        while not self._stop.is_set():
            row = self.store.one(
                "SELECT * FROM avatars WHERE bake_status='processing' ORDER BY created_at ASC LIMIT 1"
            )
            if not row:
                self._stop.wait(1.0)
                continue
            try:
                self._bake(row)
            except Exception as exc:
                self.store.update("avatars", "identifier", row["identifier"], {
                    "bake_status": "error",
                    "status": "failed",
                    "bake_progress": 0,
                    "bake_message": str(exc),
                    "error": str(exc),
                })

    def _synth_loop(self) -> None:
        while not self._stop.is_set():
            self._reclaim_live_inference()
            running = running_task(self.store)
            if running:
                try:
                    live = running.get("pid")
                    if not is_inference_pid(live, running["task_id"]):
                        found = self._live_pid_for_task(running["task_id"])
                        if found:
                            self.store.update("tasks", "task_id", running["task_id"], {"pid": found})
                            running = {**running, "pid": found}
                    if is_inference_pid(running.get("pid"), running["task_id"]):
                        self._adopt(running)
                    else:
                        self._recover_run(running)
                except Exception as exc:
                    now = iso(utc_now())
                    self.store.update("tasks", "task_id", running["task_id"], {
                        "status": "error",
                        "progress_message": "合成失败",
                        "error_message": str(exc),
                        "finished_at": now,
                        "retain_from": now,
                        "pid": None,
                    })
                    self._append_log(running["task_id"], f"ERROR {exc}\n")
                continue
            live_pids = iter_inference_pids()
            if live_pids:
                if any(self._protect_inference(pid) for pid in live_pids):
                    self._stop.wait(1.0)
                    continue
                self._kill_orphan_inference()
                self._stop.wait(1.0)
                continue
            row = self.store.one(
                "SELECT * FROM tasks WHERE status='wait' ORDER BY created_at ASC, task_id ASC LIMIT 1"
            )
            if not row:
                self._stop.wait(1.0)
                continue
            try:
                self._synthesize(row)
            except Exception as exc:
                now = iso(utc_now())
                self.store.update("tasks", "task_id", row["task_id"], {
                    "status": "error",
                    "progress_message": "合成失败",
                    "error_message": str(exc),
                    "finished_at": now,
                    "retain_from": now,
                    "pid": None,
                })
                self._append_log(row["task_id"], f"ERROR {exc}\n")

    def _cleanup_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._cleanup_once()
            except Exception as exc:
                self.log(f"cleanup failed: {exc}")
            self._stop.wait(3600)

    def _cleanup_once(self) -> None:
        now = utc_now()
        retain_days = int(self.cfg["task_retain_days"])
        expire_hours = int(self.cfg["upload_expire_hours"])
        cutoff = iso(now - timedelta(days=retain_days))
        expired = self.store.query(
            "SELECT * FROM tasks WHERE status NOT IN ('run','wait') AND retain_from < ?",
            (cutoff,),
        )
        for task in expired:
            purge_task(self.store, self.data_dir, task)
        stale = self.store.query(
            "SELECT * FROM uploads WHERE updated_at < ?",
            (iso(now - timedelta(hours=expire_hours)),),
        )
        for item in stale:
            purge_upload(self.store, self.data_dir, item["upload_id"])
        known = {item["upload_id"] for item in self.store.query("SELECT upload_id FROM uploads")}
        uploads_root = self.data_dir / "uploads"
        if uploads_root.exists():
            for child in uploads_root.iterdir():
                if child.is_dir() and child.name not in known:
                    purge_upload(self.store, self.data_dir, child.name)

    def _bake(self, row: dict) -> None:
        ident = row["identifier"]
        source = Path(row["source_path"])
        try:
            info = media.probe(source)
        except Exception as exc:
            raise RuntimeError(f"无法读取形象视频时长：{exc}") from exc
        limit_msg = video_duration_limit_message(info["duration"], self.cfg.get("max_video_duration_seconds"))
        if limit_msg:
            self.store.update("avatars", "identifier", ident, {
                "bake_status": "error",
                "status": "failed",
                "bake_progress": 0,
                "bake_message": limit_msg,
                "error": limit_msg,
                "duration": info["duration"],
                "width": info["width"],
                "height": info["height"],
            })
            return
        self.store.update("avatars", "identifier", ident, {
            "bake_progress": 10,
            "bake_message": "正在提取封面",
            "status": "preparing",
        })
        poster = self.data_dir / "avatars" / ident / "poster.jpg"
        preview = self.data_dir / "avatars" / ident / "preview.mp4"
        media.extract_poster(source, poster)
        self.store.update("avatars", "identifier", ident, {
            "bake_progress": 50,
            "bake_message": "正在生成预览",
            "poster_path": str(poster),
        })
        media.make_silent_preview(source, preview, self.cfg["preview_bitrate"])
        self.store.update("avatars", "identifier", ident, {
            "bake_status": "ready",
            "status": "ready",
            "bake_progress": 100,
            "bake_message": "",
            "error": None,
            "duration": info["duration"],
            "width": info["width"],
            "height": info["height"],
            "preview_path": str(preview),
        })

    def _usable_cache(self, path: Path) -> Path | None:
        limit = int(self.cfg.get("max_preprocess_cache_bytes") or 2_500_000_000)
        if not path.exists():
            return path
        try:
            size = path.stat().st_size
        except OSError:
            return None
        if size <= 0:
            path.unlink(missing_ok=True)
            return path
        if size > limit:
            self.log(f"discarding oversized preprocess cache {path} ({size} bytes)")
            path.unlink(missing_ok=True)
            return path
        return path

    def _prepare_inference_video(self, avatar: dict) -> Path:
        source = Path(avatar["source_path"])
        dest = self.data_dir / "avatars" / avatar["identifier"] / "work.mp4"
        return media.prepare_work_video(
            source,
            dest,
            clip_seconds=float(self.cfg.get("work_clip_seconds") or 12),
            max_width=int(self.cfg.get("max_work_width") or 1920),
            max_height=int(self.cfg.get("max_work_height") or 1080),
        )

    def _synthesize(self, row: dict) -> None:
        task_id = row["task_id"]
        avatar = self.store.one("SELECT * FROM avatars WHERE identifier=?", (row["avatar_identifier"],))
        if not avatar:
            now = iso(utc_now())
            self.store.update("tasks", "task_id", task_id, {
                "status": "cancelled",
                "progress_message": "已取消",
                "error_message": "",
                "finished_at": now,
                "retain_from": now,
            })
            return
        if avatar["bake_status"] != "ready":
            self._stop.wait(2.0)
            return
        audio = Path(row["audio_path"])
        if not audio.exists():
            raise RuntimeError("音频文件已丢失，无法重试")
        limit_msg = audio_duration_limit_message(row.get("audio_duration"), self.cfg.get("max_audio_duration_seconds"))
        if limit_msg:
            raise RuntimeError(limit_msg)
        started = iso(utc_now())
        if not self.store.claim_wait_task(task_id, {
            "status": "run",
            "progress": 2,
            "progress_message": "正在启动合成",
            "error_message": "",
            "started_at": started,
        }):
            return
        out_dir = self.data_dir / "tasks" / task_id
        out_dir.mkdir(parents=True, exist_ok=True)
        log_path = out_dir / "task.log"
        root = Path(self.cfg["root"])
        checkpoints = root / "checkpoints"
        mode = row["mode"]
        video = self._prepare_inference_video(avatar)
        cache_name = "work.pkl" if video.name == "work.mp4" else "preprocess.pkl"
        cache = self.data_dir / "avatars" / avatar["identifier"] / cache_name
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache = self._usable_cache(cache) or cache
        eta = eta_seconds(
            row.get("audio_duration"),
            mode,
            cache.exists(),
            12.0 if video.name == "work.mp4" else avatar.get("duration"),
        )
        self.store.update("tasks", "task_id", task_id, {"eta_seconds": eta})
        cmd = [
            self.cfg["python_bin"],
            str(root / "inference.py"),
            "--video", str(video),
            "--audio", str(audio),
            "--output-dir", str(out_dir),
            "--sample-name", task_id,
            "--device", self.cfg["device"],
            "--preprocess-cache", str(cache),
        ]
        if mode == "student":
            cmd += [
                "--dit-checkpoint",
                str(checkpoints / "tbdub_base.safetensors"),
                str(checkpoints / "tbdub_student.safetensors"),
                "--inference-mode", "student",
                "--num-student-steps", "2",
                "--sigma-shift", "1.0",
                "--motion-from-latents",
            ]
        else:
            cmd += [
                "--dit-checkpoint",
                str(checkpoints / "tbdub_base.safetensors"),
                str(checkpoints / "tbdub_finetune.safetensors"),
                "--inference-mode", "teacher",
                "--num-inference-steps", "30",
                "--ref-cfg-scale", "2.0",
                "--audio-cfg-scale", "6.0",
            ]

        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        env.setdefault("MALLOC_ARENA_MAX", "2")
        self._clips.pop(task_id, None)
        self._append_log(task_id, f"[TBDub] run-start {started}\n$ {' '.join(cmd)}\n")
        with self._gpu_lock:
            latest = self.store.one("SELECT status FROM tasks WHERE task_id=?", (task_id,))
            if not latest or latest["status"] != "run":
                return
            others = [pid for pid in iter_inference_pids() if inference_sample_name(pid) != task_id]
            if any(self._protect_inference(pid) for pid in others):
                self.store.update("tasks", "task_id", task_id, {
                    "status": "wait",
                    "progress": 0,
                    "progress_message": "排队中",
                    "started_at": None,
                    "pid": None,
                })
                return
            self._current_task = task_id
            stop_tick = threading.Event()
            started_ts = time.time()
            self.store.update("tasks", "task_id", task_id, {
                "progress": 8,
                "progress_message": "正在加载模型与环境",
            })

            def tick() -> None:
                while not stop_tick.wait(2.0):
                    self._update_run_progress(task_id, "", started_ts, eta, mode)
                    self._kill_if_cancelled(task_id)

            ticker = threading.Thread(target=tick, name=f"eta-{task_id[:8]}", daemon=True)
            handed_off = False
            code = 1
            try:
                ticker.start()
                with log_path.open("ab") as log_file:
                    proc = subprocess.Popen(
                        cmd,
                        cwd=str(root),
                        stdout=log_file,
                        stderr=subprocess.STDOUT,
                        start_new_session=True,
                        env=env,
                    )
                self._proc = proc
                self.store.update("tasks", "task_id", task_id, {"pid": proc.pid})
                state = self._watch_inference(task_id, proc.pid, log_path, started_ts, eta, mode)
                if state == "handed_off":
                    handed_off = True
                    return
                code = proc.wait()
            finally:
                stop_tick.set()
                if not handed_off:
                    self._proc = None
                    self._current_task = None

        self._finish_synthesis(task_id, row, avatar, cache, code)

    def _recover_run(self, row: dict) -> None:
        task_id = row["task_id"]
        live = self._live_pid_for_task(task_id)
        if live:
            self.store.update("tasks", "task_id", task_id, {"pid": live, "status": "run"})
            self._adopt({**row, "pid": live, "status": "run"})
            return
        raw = self.data_dir / "tasks" / task_id / f"{task_id}_tbdub.mp4"
        if raw.exists():
            avatar = self.store.one("SELECT * FROM avatars WHERE identifier=?", (row["avatar_identifier"],)) or {}
            cache = Path(avatar["preprocess_path"] or "") if avatar.get("preprocess_path") else (
                self.data_dir / "avatars" / row["avatar_identifier"] / "preprocess.pkl"
            )
            self._finish_synthesis(task_id, row, avatar, cache, 0)
            return
        raise RuntimeError("合成进程已退出，任务被 Worker 回收")

    def _adopt(self, row: dict) -> None:
        task_id = row["task_id"]
        pid = int(row["pid"])
        avatar = self.store.one("SELECT * FROM avatars WHERE identifier=?", (row["avatar_identifier"],))
        cache = Path(avatar["preprocess_path"] or "") if avatar and avatar.get("preprocess_path") else (
            self.data_dir / "avatars" / (row["avatar_identifier"] or "") / "preprocess.pkl"
        )
        log_path = self.data_dir / "tasks" / task_id / "task.log"
        offset = log_path.stat().st_size if log_path.exists() else 0
        started = parse_iso(row.get("started_at"))
        started_ts = started.timestamp() if started else time.time()
        eta = float(row.get("eta_seconds") or 0) or eta_seconds(row.get("audio_duration"), row.get("mode") or "student")
        mode = row.get("mode") or "student"
        self._current_task = task_id
        self.log(f"adopting synthesis pid={pid} task={task_id}")
        try:
            state = self._watch_inference(task_id, pid, log_path, started_ts, eta, mode, offset=offset)
            if state == "handed_off":
                return
        finally:
            if self._current_task == task_id:
                self._current_task = None
        latest = self.store.one("SELECT status FROM tasks WHERE task_id=?", (task_id,))
        if latest and latest["status"] == "cancelled":
            return
        raw = self.data_dir / "tasks" / task_id / f"{task_id}_tbdub.mp4"
        self._finish_synthesis(task_id, row, avatar or {}, cache, 0 if raw.exists() else 1)

    def _finish_synthesis(self, task_id: str, row: dict, avatar: dict, cache: Path, code: int) -> None:
        self._clips.pop(task_id, None)
        if cache.exists() and avatar.get("identifier") and avatar.get("preprocess_path") != str(cache):
            self.store.update("avatars", "identifier", avatar["identifier"], {
                "preprocess_path": str(cache),
            })
        latest = self.store.one("SELECT status FROM tasks WHERE task_id=?", (task_id,))
        if latest and latest["status"] == "cancelled":
            self.store.update("tasks", "task_id", task_id, {"pid": None})
            return
        if code != 0:
            raise RuntimeError(self._synthesis_error(task_id))
        out_dir = self.data_dir / "tasks" / task_id
        raw = out_dir / f"{task_id}_tbdub.mp4"
        if not raw.exists():
            raise RuntimeError("成片文件不存在")
        preview = out_dir / "preview.mp4"
        self.store.update("tasks", "task_id", task_id, {
            "progress": 96,
            "progress_message": "正在导出预览（阶段 5/5）",
        })
        media.make_audio_preview(raw, preview, self.cfg["preview_bitrate"])
        now = iso(utc_now())
        self.store.update("tasks", "task_id", task_id, {
            "status": "done",
            "progress": 100,
            "progress_message": "已完成",
            "error_message": "",
            "result_path": str(raw),
            "preview_path": str(preview),
            "finished_at": now,
            "retain_from": now,
            "pid": None,
        })

    def _drain_log(self, task_id: str, log_path: Path, offset: int, started_ts: float, eta: float, mode: str) -> int:
        if not log_path.exists():
            return offset
        size = log_path.stat().st_size
        if size <= offset:
            return offset
        text = log_path.read_text(encoding="utf-8", errors="ignore")[offset:]
        for line in text.splitlines():
            self._update_run_progress(task_id, line, started_ts, eta, mode)
        return size

    def _watch_inference(
        self,
        task_id: str,
        pid: int,
        log_path: Path,
        started_ts: float,
        eta: float,
        mode: str,
        offset: int | None = None,
    ) -> str:
        cursor = log_path.stat().st_size if offset is None and log_path.exists() else int(offset or 0)
        while pid_alive(pid) and not self._stop.is_set():
            if self._kill_if_cancelled(task_id):
                return "cancelled"
            cursor = self._drain_log(task_id, log_path, cursor, started_ts, eta, mode)
            self._update_run_progress(task_id, "", started_ts, eta, mode)
            time.sleep(0.5)
        if self._stop.is_set() and pid_alive(pid):
            self.log(f"handing off synthesis pid={pid} task={task_id}")
            return "handed_off"
        while pid_alive(pid):
            cursor = self._drain_log(task_id, log_path, cursor, started_ts, eta, mode)
            time.sleep(0.2)
        self._drain_log(task_id, log_path, cursor, started_ts, eta, mode)
        latest = self.store.one("SELECT status FROM tasks WHERE task_id=?", (task_id,))
        if latest and latest["status"] == "cancelled":
            return "cancelled"
        return "exited"

    def _synthesis_error(self, task_id: str) -> str:
        path = self.data_dir / "tasks" / task_id / "task.log"
        if not path.exists():
            return "合成进程异常退出"
        lines = [
            line.strip()
            for line in path.read_text(encoding="utf-8", errors="ignore").splitlines()
            if line.strip()
        ]
        if len(lines) <= 1:
            return "合成进程被中断（重启 Worker 时旧日志管道断开）。已改为日志直写文件，重试即可。"
        for line in reversed(lines[-40:]):
            lower = line.lower()
            if "error" in lower or "traceback" in lower or "killed" in lower or "out of memory" in lower:
                return line[:240]
        return f"合成进程异常退出：{lines[-1][:240]}"

    def _kill_if_cancelled(self, task_id: str) -> bool:
        latest = self.store.one("SELECT status, pid FROM tasks WHERE task_id=?", (task_id,))
        if not latest or latest["status"] != "cancelled":
            return False
        if self._proc and self._proc.poll() is None:
            kill_pid(self._proc.pid)
        elif latest.get("pid"):
            kill_pid(int(latest["pid"]))
        return True

    def _current_run_log(self, task_id: str) -> str:
        return current_run_log(self.data_dir / "tasks" / task_id / "task.log")

    def _progress_from_run(self, task_id: str, mode: str, started_ts: float) -> tuple[float, str]:
        data = self._current_run_log(task_id)
        lower = data.lower()
        infer = "超高质量推理中" if mode == "teacher" else "高质量推理中"
        clip = None
        for item in CLIP_RE.finditer(data):
            clip = (int(item.group(1)), int(item.group(2)))
        dwpose = None
        for item in DWPOSE_RE.finditer(data):
            dwpose = (int(item.group(1)), int(item.group(2)))

        # 5 段：加载 0-10  预处理 10-22  片段 22-88  解码贴回 88-96  导出 96-100
        # clip N/N 是最后一段开始，不是已经贴回
        if "[save_video_with_audio]" in lower or "output saved" in lower:
            return 90.0, "正在解码并贴回成片（阶段 4/5）"
        if clip:
            current, total = clip
            total = max(1, total)
            current = min(max(1, current), total)
            steps = 30 if mode == "teacher" else 2
            tqdm = last_step_tqdm(data, steps)
            done = tqdm[0] if tqdm else 0
            pct = 22.0 + (current - 1) / total * 66.0 + (done / max(1, steps)) * (66.0 / total)
            step_txt = f"，第 {done}/{steps} 步" if done else ""
            return round(min(pct, 87.9), 1), f"{infer}（片段 {current}/{total}{step_txt}）（阶段 3/5）"
        if "loaded preprocess cache" in lower:
            return 18.0, "复用形象预处理（阶段 2/5）"
        if "saved preprocess cache" in lower or "preprocessing complete" in lower:
            return 22.0, "形象预处理完成（阶段 2/5）"
        if dwpose:
            current, total = dwpose[0], max(1, dwpose[1])
            return round(10 + current / total * 12, 1), f"首次合成，正在检测人脸（{current}/{total}）（阶段 2/5）"
        if "dwpose" in lower and "frames" in lower:
            return 14.0, "首次合成，正在检测人脸（阶段 2/5）"
        elapsed = max(0.0, time.time() - started_ts)
        return round(min(9.0, 3.0 + elapsed / 15.0), 1), "正在加载模型与环境（阶段 1/5）"

    def _update_run_progress(self, task_id: str, line: str, started_ts: float, eta: float, mode: str) -> None:
        latest = self.store.one(
            "SELECT status, audio_duration, mode FROM tasks WHERE task_id=?",
            (task_id,),
        )
        if not latest or latest["status"] != "run":
            return
        progress, message = self._progress_from_run(task_id, mode, started_ts)
        remain = remaining_from_log(self._current_run_log(task_id), latest)
        payload = {
            "progress": progress,
            "progress_message": message,
        }
        if remain is not None:
            payload["eta_seconds"] = remain
        self.store.update("tasks", "task_id", task_id, payload)

    def _append_log(self, task_id: str, text: str) -> None:
        path = self.data_dir / "tasks" / task_id / "task.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as file:
            file.write(text)


def main() -> None:
    cfg = load_config()
    data_dir = cfg["data_dir"]
    data_dir.mkdir(parents=True, exist_ok=True)
    log_path = data_dir / "worker.log"

    def log(msg: str) -> None:
        sys.stdout.write(f"{iso(utc_now())} {msg}\n")
        sys.stdout.flush()

    store = Store(data_dir / "app.sqlite")
    workers = Workers(cfg, store, data_dir, log=log)
    workers.run_forever()


if __name__ == "__main__":
    main()

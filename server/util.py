from __future__ import annotations

import math
import re
import secrets
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

USER_FORBIDDEN = re.compile(r'[\\/:*?"<>|]')
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".mxf"}
AUDIO_EXTS = {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg"}
MAX_VIDEO_BYTES = 2 * 1024 * 1024 * 1024
MAX_AUDIO_BYTES = 300 * 1024 * 1024
MIN_CHUNK = 64 * 1024
MAX_CHUNK = 16 * 1024 * 1024
DEFAULT_CHUNK = 2 * 1024 * 1024
AVATAR_CHUNK = 8 * 1024 * 1024

QUALITY_MAP = {
    30: {"label": "标准", "mode": "student"},
    50: {"label": "高质量", "mode": "student"},
    80: {"label": "超高质量", "mode": "teacher"},
}

STATUS_ALIASES = {
    "queued": "wait",
    "running": "run",
    "failed": "error",
    "canceled": "cancelled",
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt: datetime | str | None) -> str | None:
    if dt is None:
        return None
    if isinstance(dt, str):
        return dt
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def envelope(data: Any = None, msg: str = "ok", code: int = 0) -> dict[str, Any]:
    return {
        "code": code,
        "msg": msg,
        "message": msg,
        "success": code == 0,
        "data": data if code == 0 else None,
    }


def fail(msg: str, code: int = 1) -> dict[str, Any]:
    return envelope(None, msg, code)


def clean_user_id(raw: str | None) -> str | None:
    if raw is None:
        return None
    # 归一化：去首尾空格 + Unicode NFC。中文/全角/组合字符统一成同一写法，
    # 避免「松江」「松江 」「全角写法」被当成不同用户。
    value = unicodedata.normalize("NFC", str(raw)).strip()
    if not value:
        return None
    if len(value) > 64:
        raise ValueError("用户ID不能超过 64 个字符")
    if USER_FORBIDDEN.search(value):
        raise ValueError("用户ID无效")
    return value


def first_user(*values: str | None) -> str | None:
    for value in values:
        cleaned = clean_user_id(value) if value else None
        if cleaned:
            return cleaned
    return None


def new_avatar_id() -> str:
    return secrets.token_hex(4)


def new_id() -> str:
    return secrets.token_hex(16)


def normalize_status(raw: str | None) -> str | None:
    if not raw:
        return None
    key = raw.strip().lower()
    if key == "active":
        return "active"
    return STATUS_ALIASES.get(key, key)


def quality_of(steps: int) -> dict[str, str]:
    if steps not in QUALITY_MAP:
        raise ValueError("步数只能是 30, 50, 80")
    return QUALITY_MAP[steps]


def format_duration_cn(seconds: float | int | None) -> str:
    sec = max(0, int(round(float(seconds or 0))))
    if sec < 60:
        return f"{sec}秒"
    minutes, rest = divmod(sec, 60)
    if rest:
        return f"{minutes}分{rest}秒"
    return f"{minutes}分钟"


def duration_limit_seconds(raw: Any, default: int) -> int:
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def video_duration_limit_seconds(raw: Any, default: int = 300) -> int:
    return duration_limit_seconds(raw, default)


def audio_duration_limit_seconds(raw: Any, default: int = 300) -> int:
    return duration_limit_seconds(raw, default)


def _duration_limit_message(label: str, duration: float | None, max_seconds: float | int | None, default: int) -> str | None:
    limit = duration_limit_seconds(max_seconds, default)
    if limit <= 0:
        return None
    if round(float(duration or 0)) > limit:
        return (
            f"{label}不能超过 {format_duration_cn(limit)}"
            f"（当前 {format_duration_cn(duration)}）"
        )
    return None


def video_duration_limit_message(duration: float | None, max_seconds: float | int | None) -> str | None:
    return _duration_limit_message("形象视频", duration, max_seconds, 300)


def audio_duration_limit_message(duration: float | None, max_seconds: float | int | None) -> str | None:
    return _duration_limit_message("音频", duration, max_seconds, 300)


def remaining_text(seconds: float | None, progress: float | None = None) -> str:
    if seconds is None:
        return ""
    sec = max(0, int(round(float(seconds))))
    if sec < 60:
        return f"约 {max(sec, 8)} 秒"
    if sec < 600:
        return f"约 {sec // 60} 分 {sec % 60:02d} 秒"
    return f"约 {max(1, round(sec / 60))} 分钟"


WORK_CLIP_SECONDS = 12.0
CLIP_MSG_RE = re.compile(r"片段\s*(\d+)\s*/\s*(\d+)")
LOG_CLIP_RE = re.compile(r"\[TBDub\]\s+clip\s+(\d+)\s*/\s*(\d+)", re.I)
LOG_META_RE = re.compile(
    r"\[TBDub\] mode=(\w+).*?output_frames=(\d+).*?clips=(\d+).*?steps=(\d+)",
    re.I,
)
LOG_TQDM_RE = re.compile(r"(\d+)\s*/\s*(\d+)\s+\[[^\]\n]*?,\s*([\d.]+)\s*(s/it|it/s)\]")
LOG_DWPOSE_RE = re.compile(r"dwpose\s+(\d+)/(\d+)\s+frames", re.I)
TEACHER_SEC_PER_STEP = 3.2
STUDENT_SEC_PER_STEP = 0.9


def estimate_clips(audio_duration: float | None) -> int:
    frames = max(1.0, float(audio_duration or 8.0) * 25.0)
    if frames <= 77:
        return 1
    return 1 + max(0, math.ceil((frames - 77.0) / 72.0))


def current_run_log(path: Path) -> str:
    if not path.exists():
        return ""
    try:
        data = path.read_text(encoding="utf-8", errors="ignore").replace("\r", "\n")
    except OSError:
        return ""
    start = -1
    for item in re.finditer(r"(?m)^(?:\$ .*inference\.py.*|\[TBDub\] run-start.*)$", data):
        start = item.start()
    return data[start:] if start >= 0 else data[-65536:]


def last_step_tqdm(text: str, steps: int) -> tuple[int, float] | None:
    samples: list[tuple[int, float]] = []
    for item in LOG_TQDM_RE.finditer(text):
        current, total, rate, unit = int(item.group(1)), int(item.group(2)), float(item.group(3)), item.group(4)
        if total != steps or current > total:
            continue
        sec = rate if unit == "s/it" else (1.0 / rate if rate else 0.0)
        if sec <= 0:
            continue
        samples.append((current, sec))
    if not samples:
        return None
    recent = samples[-8:]
    sit = sum(item[1] for item in recent) / len(recent)
    return samples[-1][0], sit


def decode_save_times(frames: float, teacher: bool) -> tuple[float, float, float]:
    """VAE 解码、贴回编码、预览导出的墙钟秒数。按 4090 实测成片帧数估算。"""
    count = max(1.0, float(frames))
    vae = max(45.0, count * (0.26 if teacher else 0.18))
    paste = max(75.0, 25.0 + count * (0.28 if teacher else 0.20))
    preview = 20.0
    return vae, paste, preview


def _log_elapsed(log_mtime: float | None, now: float | None) -> float:
    if not log_mtime:
        return 0.0
    return max(0.0, (now if now is not None else time.time()) - float(log_mtime))


def log_mtime_or_none(path: Path) -> float | None:
    try:
        if path.exists():
            return path.stat().st_mtime
    except OSError:
        return None
    return None


def remaining_from_log_path(log_path: Path, task: dict) -> float | None:
    return remaining_from_log(current_run_log(log_path), task, log_mtime=log_mtime_or_none(log_path))


def stage4_from_log(
    text: str,
    task: dict,
    log_mtime: float | None = None,
    now: float | None = None,
) -> tuple[float, str] | None:
    parsed = _parse_run_log(text, task)
    if parsed is None:
        return None
    teacher, frames, _clips, steps, _sit, tqdm, lower = parsed
    clip = None
    for item in LOG_CLIP_RE.finditer(text):
        clip = (int(item.group(1)), int(item.group(2)))
    last_done = bool(clip and clip[0] >= clip[1] and tqdm and tqdm[0] >= steps)
    if "[save_video_with_audio]" not in lower and "output saved" not in lower and not last_done:
        return None
    vae, paste, _preview = decode_save_times(frames, teacher)
    elapsed = _log_elapsed(log_mtime, now)
    if "output saved" in lower:
        frac = 0.95
    elif "[save_video_with_audio]" in lower:
        frac = min(0.92, 0.42 + 0.50 * min(1.0, elapsed / max(1.0, paste)))
    else:
        frac = min(0.42, elapsed / max(1.0, vae))
    return round(88.0 + frac * 8.0, 1), "正在解码并贴回成片（阶段 4/5）"


def stage_times(
    audio_duration: float | None,
    mode: str,
    has_preprocess: bool = False,
    avatar_duration: float | None = None,
) -> dict[str, float]:
    duration = max(1.0, float(audio_duration or 8.0))
    teacher = mode == "teacher"
    clips = estimate_clips(duration)
    steps = 30 if teacher else 2
    sit = TEACHER_SEC_PER_STEP if teacher else STUDENT_SEC_PER_STEP
    frames = duration * 25.0
    boot = 80.0
    if has_preprocess:
        prep = 0.0
    else:
        work = min(WORK_CLIP_SECONDS, max(1.0, float(avatar_duration or WORK_CLIP_SECONDS)))
        prep = max(25.0, min(90.0, work * 5.0))
    infer = clips * steps * sit
    vae, paste, preview = decode_save_times(frames, teacher)
    return {"boot": boot, "prep": prep, "infer": infer, "save": vae + paste, "preview": preview}


def _parse_run_log(text: str, task: dict) -> tuple[bool, float, int, int, float, tuple[int, float] | None, str] | None:
    if not text.strip():
        return None
    mode = task.get("mode") or "student"
    teacher = mode == "teacher"
    audio = max(1.0, float(task.get("audio_duration") or 8.0))
    steps = 30 if teacher else 2
    sit = TEACHER_SEC_PER_STEP if teacher else STUDENT_SEC_PER_STEP
    clips = estimate_clips(audio)
    frames = audio * 25.0
    meta = LOG_META_RE.search(text)
    if meta:
        teacher = meta.group(1).lower() == "teacher"
        frames = float(meta.group(2))
        clips = max(1, int(meta.group(3)))
        steps = max(1, int(meta.group(4)))
        sit = TEACHER_SEC_PER_STEP if teacher else STUDENT_SEC_PER_STEP
    tqdm = last_step_tqdm(text, steps)
    if tqdm:
        sit = min(8.0, max(0.3, tqdm[1]))
    return teacher, frames, clips, steps, sit, tqdm, text.lower()


def remaining_from_log(
    text: str,
    task: dict,
    log_mtime: float | None = None,
    now: float | None = None,
) -> float | None:
    parsed = _parse_run_log(text, task)
    if parsed is None:
        return None
    teacher, frames, clips, steps, sit, tqdm, lower = parsed
    vae, paste, preview = decode_save_times(frames, teacher)
    save = vae + paste
    elapsed = _log_elapsed(log_mtime, now)
    if "output saved" in lower:
        return max(8.0, preview - elapsed)
    if "[save_video_with_audio]" in lower:
        return max(12.0, paste + preview - elapsed)
    clip = None
    for item in LOG_CLIP_RE.finditer(text):
        clip = (int(item.group(1)), int(item.group(2)))
    if clip:
        current, total = clip[0], max(1, clip[1], clips)
        done = tqdm[0] if tqdm else 0
        if current >= total and done >= steps:
            # VAE 解码期间日志不刷；贴回还没开始，剩余不能低于贴回段
            return max(paste + preview, vae + paste + preview - elapsed)
        remain = (steps - min(done, steps)) * sit + max(0, total - current) * steps * sit
        return max(20.0, remain + save + preview)
    dwpose = None
    for item in LOG_DWPOSE_RE.finditer(text):
        dwpose = (int(item.group(1)), int(item.group(2)))
    infer = clips * steps * sit
    if "loaded preprocess cache" in lower or "saved preprocess cache" in lower or "preprocessing complete" in lower:
        return max(20.0, infer + save + preview)
    if dwpose:
        frac = dwpose[0] / max(1, dwpose[1])
        prep = max(0.0, (1.0 - frac) * 40.0)
        return max(20.0, prep + infer + save + preview)
    return max(20.0, 50.0 + infer + save + preview)


def eta_seconds(
    audio_duration: float | None,
    mode: str,
    has_preprocess: bool = False,
    avatar_duration: float | None = None,
) -> float:
    stages = stage_times(audio_duration, mode, has_preprocess, avatar_duration)
    return sum(stages.values())


def remaining_from_stage(
    task: dict,
    has_preprocess: bool = False,
    avatar_duration: float | None = None,
) -> float:
    stages = stage_times(
        task.get("audio_duration"),
        task.get("mode") or "student",
        has_preprocess,
        avatar_duration,
    )
    progress = float(task.get("progress") or 0)
    message = str(task.get("progress_message") or "")
    clip = CLIP_MSG_RE.search(message)
    if "导出" in message or progress >= 96:
        remain = stages["preview"]
    elif "解码" in message or "贴回" in message or "保存成片" in message:
        frac = min(1.0, max(0.0, (progress - 88.0) / 8.0))
        remain = (1.0 - frac) * stages["save"] + stages["preview"]
    elif clip:
        current, total = int(clip.group(1)), max(1, int(clip.group(2)))
        remain = (total - current + 1) / total * stages["infer"] + stages["save"] + stages["preview"]
    elif progress >= 22:
        frac = min(1.0, max(0.0, (progress - 22.0) / 66.0))
        remain = (1.0 - frac) * stages["infer"] + stages["save"] + stages["preview"]
    elif progress >= 10:
        frac = min(1.0, max(0.0, (progress - 10.0) / 12.0))
        remain = (1.0 - frac) * stages["prep"] + stages["infer"] + stages["save"] + stages["preview"]
    else:
        frac = min(1.0, max(0.0, progress / 10.0))
        remain = (1.0 - frac) * stages["boot"] + stages["prep"] + stages["infer"] + stages["save"] + stages["preview"]
    return max(15.0, remain)

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path


def have_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, check=True, capture_output=True, text=True)


def probe(path: Path) -> dict:
    result = run([
        "ffprobe", "-v", "error", "-show_entries",
        "format=duration:stream=width,height,codec_type",
        "-of", "json", str(path),
    ])
    info = json.loads(result.stdout or "{}")
    streams = info.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), {})
    duration = float((info.get("format") or {}).get("duration") or 0)
    return {
        "duration": duration,
        "width": int(video.get("width") or 0),
        "height": int(video.get("height") or 0),
    }


def probe_audio(path: Path) -> float:
    result = run([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "json", str(path),
    ])
    info = json.loads(result.stdout or "{}")
    return float((info.get("format") or {}).get("duration") or 0)


def extract_poster(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(src), "-frames:v", "1", "-q:v", "3", str(dest),
    ])


def make_silent_preview(src: Path, dest: Path, bitrate: str = "2M") -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(src), "-an",
        "-c:v", "libx264", "-preset", "veryfast", "-b:v", bitrate,
        "-pix_fmt", "yuv420p", "-movflags", "+faststart",
        str(dest),
    ])


def prepare_work_video(
    source: Path,
    dest: Path,
    clip_seconds: float = 12.0,
    max_width: int = 1920,
    max_height: int = 1080,
) -> Path:
    """Cut/scale a short working copy. Official inference ping-pongs it to match audio."""
    info = probe(source)
    duration = float(info["duration"] or 0)
    width = int(info["width"] or 0)
    height = int(info["height"] or 0)
    need_trim = duration > clip_seconds + 0.4
    need_scale = width * height > max_width * max_height
    if not need_trim and not need_scale:
        return source
    if dest.exists() and dest.stat().st_size > 0:
        try:
            if dest.stat().st_mtime >= source.stat().st_mtime:
                return dest
        except OSError:
            pass
    dest.parent.mkdir(parents=True, exist_ok=True)
    start = 0.0
    if need_trim:
        start = max(0.0, min(duration - clip_seconds, max(0.8, duration * 0.25)))
    filters: list[str] = []
    if need_scale:
        filters.append(
            f"scale='min({max_width},iw)':'min({max_height},ih)':force_original_aspect_ratio=decrease"
        )
        filters.append("scale=trunc(iw/2)*2:trunc(ih/2)*2")
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
    if need_trim:
        cmd += ["-ss", f"{start:.3f}", "-t", f"{clip_seconds:.3f}"]
    cmd += ["-i", str(source), "-an"]
    if filters:
        cmd += ["-vf", ",".join(filters)]
    cmd += ["-c:v", "libx264", "-preset", "fast", "-crf", "14", "-pix_fmt", "yuv420p", str(dest)]
    run(cmd)
    return dest


def make_audio_preview(src: Path, dest: Path, bitrate: str = "2M") -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(src),
        "-c:v", "libx264", "-preset", "veryfast", "-b:v", bitrate,
        "-c:a", "aac", "-b:a", "128k",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart",
        str(dest),
    ])


def make_looped_result(video: Path, audio: Path, dest: Path, bitrate: str = "2M") -> None:
    """Loop a silent avatar clip to the driving audio length and mux sound. Used by mock synthesis."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    duration = probe_audio(audio)
    if duration <= 0:
        raise RuntimeError("无法读取音频时长")
    run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-stream_loop", "-1", "-i", str(video),
        "-i", str(audio),
        "-map", "0:v:0", "-map", "1:a:0",
        "-t", f"{duration:.3f}",
        "-c:v", "libx264", "-preset", "veryfast", "-b:v", bitrate,
        "-c:a", "aac", "-b:a", "128k",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart",
        "-shortest",
        str(dest),
    ])

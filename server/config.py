from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "config.yaml"


def load_config(path: Path | None = None) -> dict[str, Any]:
    cfg_path = path or DEFAULT_CONFIG
    data: dict[str, Any] = {}
    if cfg_path.exists():
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    data_dir = Path(data.get("data_dir") or "data")
    if not data_dir.is_absolute():
        data_dir = ROOT / data_dir
    return {
        "admin_key": str(data.get("admin_key") or "King"),
        "idle_seconds": int(data.get("idle_seconds") or 1800),
        "host": str(data.get("host") or "0.0.0.0"),
        "port": int(data.get("port") or 8811),
        "app_prefix": str(data.get("app_prefix") or "").rstrip("/"),
        "data_dir": data_dir,
        "device": str(data.get("device") or "cuda:0"),
        "python_bin": str(data.get("python_bin") or "python"),
        "task_retain_days": int(data.get("task_retain_days") or 15),
        "upload_expire_hours": int(data.get("upload_expire_hours") or 24),
        "preview_bitrate": str(data.get("preview_bitrate") or "2M"),
        "default_steps": int(data.get("default_steps") or 50),
        "work_clip_seconds": float(data.get("work_clip_seconds") or 12),
        "max_work_width": int(data.get("max_work_width") or 1920),
        "max_work_height": int(data.get("max_work_height") or 1080),
        "max_preprocess_cache_bytes": int(data.get("max_preprocess_cache_bytes") or 2_500_000_000),
        "root": ROOT,
    }

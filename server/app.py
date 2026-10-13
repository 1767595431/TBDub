from __future__ import annotations

import hashlib
import hmac
import math
import atexit
import os
import shutil
import subprocess
import sys
import time
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import load_config
from .db import Store
from . import media
from .util import (
    AUDIO_EXTS,
    DEFAULT_CHUNK,
    MAX_AUDIO_BYTES,
    MAX_CHUNK,
    MAX_VIDEO_BYTES,
    MIN_CHUNK,
    VIDEO_EXTS,
    envelope,
    fail,
    first_user,
    iso,
    new_avatar_id,
    new_id,
    normalize_status,
    parse_iso,
    quality_of,
    remaining_text,
    utc_now,
    audio_duration_limit_message,
    audio_duration_limit_seconds,
    video_duration_limit_message,
    video_duration_limit_seconds,
)
from .worker import (
    drop_upload_parts,
    forget_audio_staging,
    purge_task,
    purge_upload,
    remaining_for,
    running_task,
    worker_alive,
    worker_process_alive,
)

CFG = load_config()
DATA_DIR: Path = CFG["data_dir"]
ROOT: Path = CFG["root"]
STORE = Store(DATA_DIR / "app.sqlite")
COOKIE = "qemix_gate"


def _render_html(path: Path) -> HTMLResponse:
    text = path.read_text(encoding="utf-8")
    script = '<script src="admin/assets/base.js"></script>\n'
    if "<!--APP_BASE-->" in text:
        text = text.replace("<!--APP_BASE-->", script, 1)
    elif "admin/assets/base.js" not in text:
        text = text.replace("<head>", "<head>\n  " + script, 1)
    return HTMLResponse(text)


# The create_app above got messy. Rewrite the module cleanly below via a single builder.
def build_app() -> FastAPI:
    app = FastAPI(title="TBDub Digital Human", docs_url=None, redoc_url=None)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.on_event("startup")
    def _startup() -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        for name in ("avatars", "tasks", "uploads"):
            (DATA_DIR / name).mkdir(exist_ok=True)
        # 合成 Worker 是独立进程。接口只负责在它不在时拉起，启停不影响正在跑的任务。
        _ensure_worker()

    @app.on_event("shutdown")
    def _shutdown() -> None:
        if CFG.get("mock_synthesis"):
            _stop_spawned_worker()

    @app.middleware("http")
    async def _touch_session(request: Request, call_next):
        response = await call_next(request)
        token = request.cookies.get(COOKIE)
        if token and _gate_ok(token) and "/api/" in request.url.path:
            _set_cookie(response, _make_token())
        return response

    @app.get("/", response_class=HTMLResponse)
    @app.get("/admin", response_class=HTMLResponse)
    @app.get("/admin/", response_class=HTMLResponse)
    def console() -> HTMLResponse:
        return _render_html(ROOT / "admin" / "index.html")

    @app.get("/login.html", response_class=HTMLResponse)
    def login_page() -> HTMLResponse:
        return _render_html(ROOT / "admin" / "login.html")

    @app.api_route("/favicon.svg", methods=["GET", "HEAD"])
    def favicon():
        path = ROOT / "admin" / "assets" / "favicon.svg"
        return FileResponse(path, media_type="image/svg+xml")

    @app.api_route("/favicon.png", methods=["GET", "HEAD"])
    def favicon_png():
        path = ROOT / "admin" / "assets" / "favicon.png"
        return FileResponse(path, media_type="image/png")

    if (ROOT / "admin" / "assets").exists():
        app.mount("/admin/assets", StaticFiles(directory=str(ROOT / "admin" / "assets")), name="assets")

    # ---------- admin ----------
    @app.post("/api/admin/login")
    async def admin_login(request: Request):
        body = await request.json()
        key = str(body.get("key") or "").strip()
        if key != CFG["admin_key"]:
            return JSONResponse(fail("密钥错误"))
        resp = JSONResponse(envelope({"ok": True}))
        _set_cookie(resp, _make_token())
        return resp

    @app.get("/api/admin/session")
    def admin_session(request: Request):
        ok = _gate_ok(request.cookies.get(COOKIE))
        return envelope({"enabled": True, "ok": ok, "idle_seconds": CFG["idle_seconds"]})

    @app.post("/api/admin/heartbeat")
    def admin_heartbeat(request: Request):
        if not _gate_ok(request.cookies.get(COOKIE)):
            return fail("未登录")
        resp = JSONResponse(envelope({"ok": True}))
        _set_cookie(resp, _make_token())
        return resp

    @app.post("/api/admin/logout")
    def admin_logout():
        resp = JSONResponse(envelope({"ok": True}))
        resp.delete_cookie(COOKIE, path="/")
        return resp

    # ---------- system ----------
    @app.api_route("/api/system/ready", methods=["GET", "HEAD"])
    def system_ready(request: Request):
        data = _ready_payload()
        if request.method == "HEAD":
            return JSONResponse(envelope(data), headers={"Content-Length": "0"})
        return envelope(data)

    @app.get("/api/system/status")
    def system_status():
        return envelope(_status_payload())

    @app.get("/api/summary")
    def summary(username: str | None = None, user_id: str | None = None):
        try:
            uid = first_user(username, user_id)
        except ValueError as exc:
            return fail(str(exc))
        if not uid:
            return fail("请填写用户ID")
        return envelope(_user_summary(uid))

    # ---------- avatars ----------
    @app.get("/api/avatars")
    def list_avatars(
        type: str = "all",
        username: str | None = None,
        user_id: str | None = None,
        bake_status: str | None = None,
        page: int = 1,
        page_size: int = 12,
    ):
        try:
            uid = first_user(username, user_id)
        except ValueError as exc:
            return fail(str(exc))
        kind = (type or "all").lower()
        if kind not in {"public", "private", "all"}:
            kind = "all"
        if kind == "private" and not uid:
            return fail("查询个人形象必须填写用户ID")
        if kind == "all" and not uid:
            kind = "public"
        page = max(1, page)
        page_size = min(100, max(1, page_size))
        items = [_avatar_public(row) for row in STORE.query("SELECT * FROM avatars ORDER BY created_at DESC")]
        scoped = []
        for item in items:
            if item["type"] == "public":
                if kind in {"public", "all"}:
                    scoped.append(item)
            elif uid and item["user_id"] == uid and kind in {"private", "all"}:
                scoped.append(item)
        if bake_status:
            scoped = [x for x in scoped if x["bake_status"] == bake_status]
        total = len(scoped)
        pages = max(1, math.ceil(total / page_size)) if total else 1
        start = (page - 1) * page_size
        public_all = [x for x in items if x["type"] == "public"]
        private_all = [x for x in items if x["type"] == "private" and uid and x["user_id"] == uid]
        return envelope({
            "items": scoped[start:start + page_size],
            "total": total,
            "page": page,
            "pages": pages,
            "page_size": page_size,
            "counts": {"public": len(public_all), "private": len(private_all)},
            "ready_counts": {
                "public": sum(1 for x in public_all if x["bake_status"] == "ready"),
                "private": sum(1 for x in private_all if x["bake_status"] == "ready"),
            },
        })

    @app.api_route("/api/avatars/upload", methods=["GET", "POST"])
    async def avatar_upload(request: Request):
        form = None
        if request.method != "GET":
            try:
                form = await request.form()
            except Exception:
                form = None
        stage = ""
        upload_id = ""
        if form is not None:
            stage = str(form.get("stage") or "")
            upload_id = str(form.get("upload_id") or "")
        stage = stage or (request.query_params.get("stage") or "")
        upload_id = upload_id or (request.query_params.get("upload_id") or "")
        if stage == "abort":
            return _abort_avatar_upload(upload_id)
        if request.method == "GET":
            return fail("未知的上传阶段")
        if form is None:
            return fail("未知的上传阶段")
        if stage == "init":
            return await _avatar_init(form)
        if stage == "chunk":
            return await _avatar_chunk(form)
        if stage == "complete":
            return _avatar_complete(upload_id)
        return fail("未知的上传阶段")

    @app.post("/api/avatars/{identifier}/rebake")
    def rebake(identifier: str, request: Request, username: str | None = None, user_id: str | None = None):
        try:
            row = _get_avatar(identifier, first_user(username, user_id), admin=_is_admin(request))
        except ValueError as exc:
            return fail(str(exc))
        if not row:
            return fail("形象不存在")
        STORE.update("avatars", "identifier", identifier, {
            "bake_status": "processing",
            "status": "queued",
            "bake_progress": 0,
            "bake_message": "已重新排队转码",
            "error": None,
        })
        return envelope({"identifier": identifier}, "已重新排队转码")

    @app.delete("/api/avatars/{identifier}")
    def delete_avatar(identifier: str, request: Request, username: str | None = None, user_id: str | None = None):
        try:
            uid = first_user(username, user_id)
            row = _get_avatar(identifier, uid, admin=_is_admin(request))
        except ValueError as exc:
            return fail(str(exc))
        if not row:
            return fail("形象不存在")
        busy = STORE.query(
            "SELECT task_id FROM tasks WHERE avatar_identifier=? AND status IN ('wait','run')",
            (identifier,),
        )
        now = iso(utc_now())
        for task in busy:
            STORE.update("tasks", "task_id", task["task_id"], {
                "status": "cancelled",
                "progress_message": "已取消",
                "error_message": "",
                "finished_at": now,
                "retain_from": now,
            })
        shutil.rmtree(DATA_DIR / "avatars" / identifier, ignore_errors=True)
        STORE.delete("avatars", "identifier", identifier)
        msg = "已删除" if not busy else f"已删除，并取消 {len(busy)} 条排队或合成中的作品"
        return envelope({"identifier": identifier, "cancelled_tasks": len(busy)}, msg)

    @app.api_route("/api/characters/{ident}/poster", methods=["GET", "HEAD"])
    def character_poster(ident: str, request: Request, x_user_id: str | None = Header(default=None), x_username: str | None = Header(default=None)):
        row = STORE.one("SELECT * FROM avatars WHERE identifier=?", (ident,))
        if not row:
            raise HTTPException(404, detail="形象不存在")
        if not _can_access_avatar(request, row, x_user_id, x_username):
            raise HTTPException(404, detail="形象不存在")
        poster = Path(row["poster_path"] or "")
        if not poster.exists():
            raise HTTPException(404, detail="暂无封面")
        headers = {
            "Cache-Control": "public, max-age=31536000, immutable" if row["type"] == "public"
            else "private, max-age=31536000, immutable"
        }
        return FileResponse(poster, media_type="image/jpeg", headers=headers)

    @app.api_route("/api/characters/{ident}/video", methods=["GET", "HEAD"])
    def character_video(ident: str, request: Request, x_user_id: str | None = Header(default=None), x_username: str | None = Header(default=None)):
        row = STORE.one("SELECT * FROM avatars WHERE identifier=?", (ident,))
        if not row:
            raise HTTPException(404, detail="形象不存在")
        if not _can_access_avatar(request, row, x_user_id, x_username):
            raise HTTPException(404, detail="形象不存在")
        preview = Path(row["preview_path"] or "")
        if not preview.exists():
            raise HTTPException(404, detail="形象视频不存在")
        return FileResponse(preview, media_type="video/mp4", headers={"Cache-Control": "no-store"})

    # ---------- tasks ----------
    @app.get("/api/tasks")
    def list_tasks(
        request: Request,
        page: int = 1,
        page_size: int = 12,
        username: str | None = None,
        user_id: str | None = None,
        status: str | None = None,
        keyword: str | None = None,
    ):
        try:
            uid = first_user(username, user_id)
        except ValueError as exc:
            return fail(str(exc))
        admin = _is_admin(request)
        page = max(1, page)
        page_size = min(100, max(1, page_size))
        rows = STORE.query("SELECT * FROM tasks ORDER BY created_at DESC")
        items = []
        for row in rows:
            avatar = STORE.one("SELECT * FROM avatars WHERE identifier=?", (row["avatar_identifier"],))
            if uid:
                if row["user_id"] != uid:
                    continue
            elif not admin and _task_avatar_kind(row, avatar) != "public":
                continue
            item = _task_public(row, avatar)
            want = normalize_status(status)
            if want == "active" and item["status"] not in {"wait", "run"}:
                continue
            if want and want != "active" and item["status"] != want:
                continue
            if keyword:
                blob = f"{item['task_name']} {item.get('audio_name') or ''} {item.get('avatar_name') or ''}"
                if keyword.lower() not in blob.lower():
                    continue
            items.append(item)
        total = len(items)
        pages = max(1, math.ceil(total / page_size)) if total else 1
        start = (page - 1) * page_size
        data: dict[str, Any] = {
            "tasks": items[start:start + page_size],
            "total": total,
            "page": page,
            "pages": pages,
            "page_size": page_size,
        }
        if uid:
            data["summary"] = _user_summary(uid)
        return envelope(data)

    @app.post("/api/tasks/create")
    async def create_task(request: Request):
        form = await request.form()
        try:
            uid = first_user(str(form.get("username") or ""), str(form.get("user_id") or ""))
        except ValueError as exc:
            return fail(str(exc))
        if not uid:
            return fail("请填写用户ID")
        task_name = str(form.get("task_name") or form.get("title") or "").strip()
        if not task_name:
            return fail("请填写作品名称")
        avatar_id = str(form.get("avatar_identifier") or form.get("character_id") or "").strip()
        avatar = _get_avatar(avatar_id, uid)
        if not avatar:
            return fail("形象不存在")
        if avatar["bake_status"] != "ready":
            return fail("形象还在转码，请稍后再提交")
        try:
            steps = int(form.get("steps") or CFG["default_steps"])
        except (TypeError, ValueError):
            return fail("步数只能是 30, 50, 80")
        try:
            quality = quality_of(steps)
        except ValueError as exc:
            return fail(str(exc))
        audio_path, audio_name = await _resolve_audio(form)
        if audio_path is None:
            return fail(audio_name)
        upload_id = str(form.get("audio_upload_id") or "").strip()
        try:
            try:
                duration = media.probe_audio(audio_path)
            except Exception:
                return fail("无法读取音频时长")
            limit_msg = audio_duration_limit_message(duration, CFG.get("max_audio_duration_seconds"))
            if limit_msg:
                return fail(limit_msg)
            task_id = new_id()
            dest_dir = DATA_DIR / "tasks" / task_id
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / audio_name
            shutil.move(str(audio_path), dest)
            now = iso(utc_now())
            STORE.insert("tasks", {
                "task_id": task_id,
                "task_name": task_name,
                "user_id": uid,
                "avatar_identifier": avatar_id,
                "status": "wait",
                "progress": 0,
                "progress_message": "排队中",
                "error_message": "",
                "steps": steps,
                "quality_label": quality["label"],
                "mode": quality["mode"],
                "audio_name": audio_name,
                "audio_path": str(dest),
                "audio_duration": duration,
                "avatar_type": avatar["type"],
                "created_at": now,
                "retain_from": now,
            })
            row = STORE.one("SELECT * FROM tasks WHERE task_id=?", (task_id,))
            return envelope(_task_public(row, avatar), "已加入合成队列")
        finally:
            forget_audio_staging(STORE, DATA_DIR, upload_id, audio_path)

    @app.get("/api/tasks/{task_id}")
    def get_task(task_id: str, request: Request, username: str | None = None, user_id: str | None = None):
        try:
            row, avatar = _get_task(task_id, first_user(username, user_id), admin=_is_admin(request))
        except ValueError as exc:
            return fail(str(exc))
        if not row:
            return fail("任务不存在")
        return envelope(_task_public(row, avatar))

    @app.post("/api/tasks/{task_id}/cancel")
    def cancel_task(task_id: str, request: Request, username: str | None = None, user_id: str | None = None):
        try:
            row, avatar = _get_task(task_id, first_user(username, user_id), admin=_is_admin(request))
        except ValueError as exc:
            return fail(str(exc))
        if not row:
            return fail("任务不存在")
        if row["status"] == "cancelled":
            return envelope(_task_public(row, avatar), "已取消")
        if row["status"] == "done":
            return fail("已完成的任务不能取消")
        if row["status"] == "error":
            return fail("已结束的任务不能取消")
        now = iso(utc_now())
        STORE.update("tasks", "task_id", task_id, {
            "status": "cancelled",
            "progress_message": "已取消",
            "error_message": "",
            "finished_at": now,
            "retain_from": now,
        })
        row = STORE.one("SELECT * FROM tasks WHERE task_id=?", (task_id,))
        return envelope(_task_public(row, avatar), "已取消")

    @app.post("/api/tasks/{task_id}/retry")
    def retry_task(task_id: str, request: Request, username: str | None = None, user_id: str | None = None):
        try:
            row, avatar = _get_task(task_id, first_user(username, user_id), admin=_is_admin(request))
        except ValueError as exc:
            return fail(str(exc))
        if not row:
            return fail("任务不存在")
        if row["status"] == "run":
            return fail("正在合成的任务不能重试")
        if not row.get("audio_path") or not Path(row["audio_path"]).exists():
            return fail("音频文件已丢失，无法重试")
        if not avatar:
            return fail("形象不存在")
        if avatar.get("bake_status") != "ready":
            return fail("形象还在转码，请稍后再提交")
        limit_msg = audio_duration_limit_message(row.get("audio_duration"), CFG.get("max_audio_duration_seconds"))
        if limit_msg:
            return fail(limit_msg)
        now = iso(utc_now())
        STORE.update("tasks", "task_id", task_id, {
            "status": "wait",
            "progress": 0,
            "progress_message": "排队中",
            "error_message": "",
            "started_at": None,
            "finished_at": None,
            "retain_from": now,
        })
        row = STORE.one("SELECT * FROM tasks WHERE task_id=?", (task_id,))
        return envelope(_task_public(row, avatar), "已重新排队")

    @app.delete("/api/tasks/{task_id}")
    def delete_task(task_id: str, request: Request, username: str | None = None, user_id: str | None = None):
        try:
            row, _avatar = _get_task(task_id, first_user(username, user_id), admin=_is_admin(request))
        except ValueError as exc:
            return fail(str(exc))
        if not row:
            return fail("任务不存在")
        if row["status"] == "run":
            return fail("正在合成的任务不能删除")
        purge_task(STORE, DATA_DIR, row)
        return envelope({"task_id": task_id}, "已删除")

    @app.api_route("/api/tasks/{task_id}/preview", methods=["GET", "HEAD"])
    def preview_task(task_id: str, request: Request, x_user_id: str | None = Header(default=None), x_username: str | None = Header(default=None)):
        row, avatar = _media_task(task_id, request, x_user_id, x_username)
        if row["status"] != "done":
            raise HTTPException(400, detail="任务尚未合成完成")
        path = Path(row.get("preview_path") or "")
        if not path.exists():
            raise HTTPException(404, detail="成片文件不存在")
        headers = {"Cache-Control": "private, no-store"} if avatar and avatar["type"] == "private" else {"Cache-Control": "no-store"}
        return FileResponse(path, media_type="video/mp4", headers=headers)

    @app.api_route("/api/tasks/{task_id}/download", methods=["GET", "HEAD"])
    def download_task(task_id: str, request: Request, x_user_id: str | None = Header(default=None), x_username: str | None = Header(default=None)):
        row, _avatar = _media_task(task_id, request, x_user_id, x_username)
        if row["status"] != "done":
            raise HTTPException(400, detail="任务尚未合成完成")
        path = Path(row.get("result_path") or "")
        if not path.exists():
            raise HTTPException(404, detail="成片文件不存在")
        filename = quote(f"{task_id}.mp4")
        return FileResponse(
            path,
            media_type="video/mp4",
            headers={
                "Content-Disposition": f"attachment; filename=\"{task_id}.mp4\"; filename*=UTF-8''{filename}",
                "Cache-Control": "no-store",
            },
        )

    @app.get("/api/tasks/{task_id}/log")
    def task_log(task_id: str, request: Request, tail: int = 500, username: str | None = None, user_id: str | None = None):
        try:
            row, _avatar = _get_task(task_id, first_user(username, user_id), admin=_is_admin(request))
        except ValueError as exc:
            return fail(str(exc))
        if not row:
            return fail("任务不存在")
        path = DATA_DIR / "tasks" / task_id / "task.log"
        content = ""
        exists = path.exists()
        if exists:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            content = "\n".join(lines[-max(1, tail):])
        return envelope({
            "content": content,
            "exists": exists,
            "task_status": row["status"],
            "progress": row["progress"],
            "progress_message": row["progress_message"],
            "log_path": str(path) if exists else "",
            "updated_at": iso(utc_now()),
        })

    # ---------- audio uploads (no envelope) ----------
    @app.post("/api/uploads")
    async def upload_init(request: Request):
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"detail": "请使用 JSON 提交 filename 与 size"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"detail": "请使用 JSON 提交 filename 与 size"}, status_code=400)
        filename = str(body.get("filename") or "audio.wav")
        ext = Path(filename).suffix.lower()
        if ext not in AUDIO_EXTS:
            return JSONResponse({"detail": f"不支持的音频格式：{ext or '未知'}"}, status_code=400)
        size = int(body.get("size") or body.get("filesize") or 0)
        if size <= 0 or size > MAX_AUDIO_BYTES:
            return JSONResponse({"detail": "音频不能超过 300MB"}, status_code=400)
        chunk_size = DEFAULT_CHUNK
        total = max(1, math.ceil(size / chunk_size))
        upload_id = new_id()
        now = iso(utc_now())
        (DATA_DIR / "uploads" / upload_id).mkdir(parents=True)
        STORE.insert("uploads", {
            "upload_id": upload_id,
            "kind": "audio",
            "filename": filename,
            "filesize": size,
            "chunk_size": chunk_size,
            "total_chunks": total,
            "received": STORE.dumps([]),
            "created_at": now,
            "updated_at": now,
        })
        return {"upload_id": upload_id, "chunk_size": chunk_size, "total_chunks": total}

    @app.put("/api/uploads/{upload_id}/chunks/{index}")
    async def upload_chunk(upload_id: str, index: int, request: Request):
        row = STORE.one("SELECT * FROM uploads WHERE upload_id=? AND kind='audio'", (upload_id,))
        if not row:
            return JSONResponse({"detail": "上传任务不存在"}, status_code=404)
        data = await request.body()
        if not data:
            return JSONResponse({"detail": "空分片"}, status_code=400)
        dest = DATA_DIR / "uploads" / upload_id / f"{index:06d}.part"
        dest.write_bytes(data)
        received = set(STORE.loads(row["received"], []))
        received.add(index)
        STORE.update("uploads", "upload_id", upload_id, {
            "received": STORE.dumps(sorted(received)),
            "updated_at": iso(utc_now()),
        })
        return {"index": index, "received": len(received), "total_chunks": row["total_chunks"]}

    @app.post("/api/uploads/{upload_id}/complete")
    def upload_complete(upload_id: str):
        row = STORE.one("SELECT * FROM uploads WHERE upload_id=? AND kind='audio'", (upload_id,))
        if not row:
            return JSONResponse({"detail": "上传任务不存在"}, status_code=404)
        total = int(row["total_chunks"] or 0)
        # 以磁盘上的分片文件为准：并发上传时 received 列表存在读改写竞态，
        # 不能拿它当唯一依据，否则偶发丢一次计数就误报"分片不完整"。
        parts_dir = DATA_DIR / "uploads" / upload_id
        missing = [i for i in range(total) if not (parts_dir / f"{i:06d}.part").is_file()]
        if missing:
            shown = ", ".join(str(i) for i in missing[:5])
            more = f" 等 {len(missing)} 片" if len(missing) > 5 else ""
            return JSONResponse({"detail": f"分片 {shown}{more} 文件丢失（请重新上传）"}, status_code=400)
        dest = DATA_DIR / "uploads" / upload_id / row["filename"]
        with dest.open("wb") as out:
            for i in range(total):
                part = DATA_DIR / "uploads" / upload_id / f"{i:06d}.part"
                if not part.exists():
                    return JSONResponse({"detail": f"分片 {i} 文件丢失"}, status_code=400)
                out.write(part.read_bytes())
        if dest.stat().st_size != int(row["filesize"] or 0):
            return JSONResponse({"detail": "合并后文件大小与声明不一致"}, status_code=400)
        try:
            duration = media.probe_audio(dest)
        except Exception:
            purge_upload(STORE, DATA_DIR, upload_id)
            return JSONResponse({"detail": "无法读取音频时长"}, status_code=400)
        limit_msg = audio_duration_limit_message(duration, CFG.get("max_audio_duration_seconds"))
        if limit_msg:
            purge_upload(STORE, DATA_DIR, upload_id)
            return JSONResponse({"detail": limit_msg}, status_code=400)
        drop_upload_parts(DATA_DIR, upload_id)
        STORE.update("uploads", "upload_id", upload_id, {"updated_at": iso(utc_now())})
        return {"upload_id": upload_id, "filename": row["filename"], "duration": duration}

    @app.delete("/api/uploads/{upload_id}")
    def upload_delete(upload_id: str):
        purge_upload(STORE, DATA_DIR, upload_id)
        return {"ok": True}

    return app


def _preprocess_ready(avatar: dict[str, Any] | None) -> bool:
    if not avatar:
        return False
    paths = []
    if avatar.get("preprocess_path"):
        paths.append(Path(avatar["preprocess_path"]))
    ident = avatar.get("identifier")
    if ident:
        paths.append(DATA_DIR / "avatars" / ident / "preprocess.pkl")
        paths.append(DATA_DIR / "avatars" / ident / "work.pkl")
    for path in paths:
        try:
            if path.exists() and 0 < path.stat().st_size < 64 * 1024 * 1024:
                return True
        except OSError:
            continue
    return False


_WORKER_PROC: subprocess.Popen | None = None


def _ensure_worker() -> None:
    global _WORKER_PROC
    if worker_alive(DATA_DIR) or worker_process_alive(DATA_DIR):
        return
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    log_file = (DATA_DIR / "worker.log").open("a", encoding="utf-8")
    kwargs: dict[str, Any] = {
        "cwd": str(ROOT),
        "stdout": log_file,
        "stderr": subprocess.STDOUT,
        "env": os.environ.copy(),
    }
    # 模拟联调跟启动窗口同生共死；正式环境仍拆开进程组，避免重启接口杀掉 Worker。
    if CFG.get("mock_synthesis"):
        _WORKER_PROC = subprocess.Popen([sys.executable, "-m", "server.worker"], **kwargs)
        return
    try:
        subprocess.Popen([sys.executable, "-m", "server.worker"], start_new_session=True, **kwargs)
    except TypeError:
        subprocess.Popen([sys.executable, "-m", "server.worker"], **kwargs)


def _stop_spawned_worker() -> None:
    global _WORKER_PROC
    proc = _WORKER_PROC
    _WORKER_PROC = None
    if not proc or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=8)
    except subprocess.TimeoutExpired:
        proc.kill()


atexit.register(lambda: _stop_spawned_worker() if CFG.get("mock_synthesis") else None)


def _cuda_available() -> bool:
    try:
        import torch
        return bool(torch.cuda.is_available())
    except Exception:
        return False


def _ready_payload() -> dict[str, Any]:
    ffmpeg_ok = media.have_ffmpeg()
    mock = bool(CFG.get("mock_synthesis"))
    ckpt = ROOT / "checkpoints"
    model_ok = mock or all((
        (ckpt / "tbdub_base.safetensors").exists(),
        (ckpt / "tbdub_finetune.safetensors").exists(),
        (ckpt / "tbdub_student.safetensors").exists(),
        (ckpt / "hubert-large-ll60k").exists(),
    ))
    gpu_ok = mock or _cuda_available()
    current = running_task(STORE)
    gpu_busy = current is not None
    gpus = []
    if gpu_ok or mock:
        gpus.append({
            "id": 0,
            "busy": gpu_busy,
            "job_id": current["task_id"] if current else None,
        })
    return {
        "ready": ffmpeg_ok and model_ok and gpu_ok,
        "checks": {"ffmpeg": ffmpeg_ok, "gpu": gpu_ok, "model": model_ok},
        "gpu_busy": gpu_busy,
        "gpus": gpus,
        "worker_alive": worker_alive(DATA_DIR),
        "features": _features(),
    }


def _features() -> dict[str, Any]:
    audio_limit = audio_duration_limit_seconds(CFG.get("max_audio_duration_seconds"))
    return {
        "max_synthesis_duration_seconds": audio_limit,
        "max_audio_duration_seconds": audio_limit,
        "max_video_duration_seconds": video_duration_limit_seconds(CFG.get("max_video_duration_seconds")),
        "task_retain_days": int(CFG.get("task_retain_days") or 15),
    }


def _status_payload() -> dict[str, Any]:
    ready = _ready_payload()
    avatars = STORE.query("SELECT type, bake_status FROM avatars")
    tasks = STORE.query("SELECT status FROM tasks")
    public = sum(1 for a in avatars if a["type"] == "public")
    private = sum(1 for a in avatars if a["type"] == "private")
    return {
        "baking": sum(1 for a in avatars if a["bake_status"] == "processing"),
        "queued": sum(1 for t in tasks if t["status"] == "wait"),
        "running": sum(1 for t in tasks if t["status"] == "run"),
        "avatars": {"public": public, "private": private, "total": public + private},
        "tasks": {
            "done": sum(1 for t in tasks if t["status"] == "done"),
            "run": sum(1 for t in tasks if t["status"] == "run"),
            "wait": sum(1 for t in tasks if t["status"] == "wait"),
            "error": sum(1 for t in tasks if t["status"] == "error"),
            "cancelled": sum(1 for t in tasks if t["status"] == "cancelled"),
        },
        "gpu_busy": ready["gpu_busy"],
        "gpus": ready["gpus"],
        "worker_alive": worker_alive(DATA_DIR),
        "mock_synthesis": bool(CFG.get("mock_synthesis")),
        "features": _features(),
    }


def _user_summary(uid: str) -> dict[str, Any]:
    avatars = STORE.query("SELECT bake_status FROM avatars WHERE type='private' AND user_id=?", (uid,))
    tasks = STORE.query("SELECT status, created_at FROM tasks WHERE user_id=?", (uid,))
    waiting = STORE.query("SELECT task_id, user_id, created_at FROM tasks WHERE status='wait' ORDER BY created_at ASC, task_id ASC")
    mine_wait = [t for t in waiting if t["user_id"] == uid]
    ahead = None
    position = None
    if mine_wait:
        first = mine_wait[0]
        ahead = sum(1 for t in waiting if t["created_at"] < first["created_at"])
        position = ahead + 1
    return {
        "avatars": {
            "total": len(avatars),
            "ready": sum(1 for a in avatars if a["bake_status"] == "ready"),
            "processing": sum(1 for a in avatars if a["bake_status"] == "processing"),
            "error": sum(1 for a in avatars if a["bake_status"] == "error"),
        },
        "tasks": {
            "done": sum(1 for t in tasks if t["status"] == "done"),
            "run": sum(1 for t in tasks if t["status"] == "run"),
            "wait": sum(1 for t in tasks if t["status"] == "wait"),
            "error": sum(1 for t in tasks if t["status"] == "error"),
            "cancelled": sum(1 for t in tasks if t["status"] == "cancelled"),
        },
        "queue": {
            "mine": len(mine_wait),
            "ahead": ahead,
            "position": position,
            "gpu_busy": running_task(STORE) is not None,
        },
    }


def _poster_url(ident: str, poster_path: str | None) -> str:
    if not poster_path or not Path(poster_path).exists():
        return ""
    version = int(Path(poster_path).stat().st_mtime)
    return f"/api/characters/{ident}/poster?v={version}"


def _avatar_public(row: dict[str, Any]) -> dict[str, Any]:
    ident = row["identifier"]
    ready = row["bake_status"] == "ready"
    thumb = _poster_url(ident, row.get("poster_path"))
    video = f"/api/characters/{ident}/video" if ready and row.get("preview_path") else ""
    return {
        "identifier": ident,
        "id": ident,
        "name": row["name"],
        "type": row["type"],
        "user_id": row.get("user_id"),
        "username": row.get("user_id"),
        "status": row.get("status") or row["bake_status"],
        "error": row.get("error"),
        "progress": row.get("bake_progress"),
        "duration": row.get("duration"),
        "width": row.get("width"),
        "height": row.get("height"),
        "created_at": row["created_at"],
        "thumbnail": thumb,
        "preview_thumbnail": thumb,
        "video_path": video,
        "preview_video_path": video,
        "bake_status": row["bake_status"],
        "bake_progress": row.get("bake_progress") or 0,
        "bake_message": row.get("bake_message") or "",
        "preprocess_ready": _preprocess_ready(row),
    }


def _task_public(row: dict[str, Any] | None, avatar: dict[str, Any] | None) -> dict[str, Any]:
    assert row is not None
    av = _avatar_public(avatar) if avatar else None
    status = row["status"]
    queue_position = None
    queue_ahead = None
    if status == "wait":
        waiting = STORE.query("SELECT task_id, created_at FROM tasks WHERE status='wait' ORDER BY created_at ASC, task_id ASC")
        ids = [t["task_id"] for t in waiting]
        if row["task_id"] in ids:
            queue_position = ids.index(row["task_id"]) + 1
            queue_ahead = queue_position - 1
    remain = (
        remaining_for(
            row,
            has_preprocess=bool(av.get("preprocess_ready")) if av else False,
            avatar_duration=av.get("duration") if av else None,
            data_dir=DATA_DIR,
        )
        if status == "run"
        else None
    )
    result = f"/api/tasks/{row['task_id']}/preview" if status == "done" else ""
    return {
        "task_id": row["task_id"],
        "task_name": row["task_name"],
        "username": row["user_id"],
        "user_id": row["user_id"],
        "avatar_identifier": row["avatar_identifier"],
        "avatar_name": av["name"] if av else "",
        "avatar_thumbnail": av["thumbnail"] if av else "",
        "avatar_preview_video": av["preview_video_path"] if av else "",
        "avatar_video_path": av["video_path"] if av else "",
        "avatar_bake_status": av["bake_status"] if av else "",
        "avatar_missing": avatar is None,
        "status": status,
        "progress": row.get("progress") or 0,
        "progress_message": row.get("progress_message") or "",
        "error_message": row.get("error_message") or "",
        "result_path": result,
        "result_path_lbr": result,
        "result_thumbnail": av["thumbnail"] if av and status == "done" else "",
        "steps": row["steps"],
        "quality_label": row["quality_label"],
        "audio_name": row.get("audio_name") or "",
        "audio_duration": row.get("audio_duration") or 0,
        "avatar_duration": av.get("duration") if av else 0,
        "preprocess_ready": bool(av.get("preprocess_ready")) if av else False,
        "remaining_seconds": remain,
        "remaining_at": iso(utc_now()) if remain is not None else "",
        "total_duration_text": remaining_text(remain) if remain is not None else "",
        "queue_position": queue_position,
        "queue_ahead": queue_ahead,
        "created_at": row["created_at"],
        "started_at": row.get("started_at"),
        "finished_at": row.get("finished_at"),
        "expires_at": _task_expires_at(row),
        "retain_days": int(CFG.get("task_retain_days") or 15),
    }


def _task_avatar_kind(row: dict[str, Any], avatar: dict[str, Any] | None) -> str:
    if avatar and avatar.get("type"):
        return str(avatar["type"])
    kind = str(row.get("avatar_type") or "").strip().lower()
    return kind if kind in {"public", "private"} else "private"


def _task_expires_at(row: dict[str, Any]) -> str | None:
    if row.get("status") in {"wait", "run"}:
        return None
    base = parse_iso(row.get("retain_from") or row.get("finished_at") or row.get("created_at"))
    if not base:
        return None
    days = int(CFG.get("task_retain_days") or 15)
    return iso(base + timedelta(days=days))


def _get_avatar(identifier: str, uid: str | None, admin: bool = False) -> dict[str, Any] | None:
    if not identifier:
        return None
    row = STORE.one("SELECT * FROM avatars WHERE identifier=?", (identifier,))
    if not row:
        return None
    if admin:
        return row
    if row["type"] == "private" and (not uid or row["user_id"] != uid):
        return None
    return row


def _get_task(task_id: str, uid: str | None, admin: bool = False) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    row = STORE.one("SELECT * FROM tasks WHERE task_id=?", (task_id,))
    if not row:
        return None, None
    avatar = STORE.one("SELECT * FROM avatars WHERE identifier=?", (row["avatar_identifier"],))
    if admin:
        return row, avatar
    kind = _task_avatar_kind(row, avatar)
    if kind == "private":
        if not uid or row["user_id"] != uid:
            return None, None
    elif uid and row["user_id"] != uid:
        return None, None
    return row, avatar


def _is_admin(request: Request | None) -> bool:
    if request is None:
        return False
    return _gate_ok(request.cookies.get(COOKIE))


def _can_access_avatar(request: Request, row: dict[str, Any], x_user_id: str | None, x_username: str | None) -> bool:
    if row["type"] == "public":
        return True
    if _gate_ok(request.cookies.get(COOKIE)):
        return True
    try:
        uid = first_user(x_user_id, x_username)
    except ValueError:
        return False
    return bool(uid and uid == row.get("user_id"))


def _media_task(task_id: str, request: Request, x_user_id: str | None, x_username: str | None):
    row = STORE.one("SELECT * FROM tasks WHERE task_id=?", (task_id,))
    if not row:
        raise HTTPException(404, detail="任务不存在")
    avatar = STORE.one("SELECT * FROM avatars WHERE identifier=?", (row["avatar_identifier"],))
    if _task_avatar_kind(row, avatar) == "private":
        if avatar:
            allowed = _can_access_avatar(request, avatar, x_user_id, x_username)
        elif _gate_ok(request.cookies.get(COOKIE)):
            allowed = True
        else:
            try:
                uid = first_user(x_user_id, x_username)
            except ValueError:
                uid = None
            allowed = bool(uid and uid == row.get("user_id"))
        if not allowed:
            raise HTTPException(404, detail="任务不存在")
    return row, avatar


async def _avatar_init(form) -> JSONResponse:
    try:
        name = str(form.get("name") or "").strip()
        if not name:
            return JSONResponse(fail("请填写形象名称"))
        if len(name) > 80:
            return JSONResponse(fail("形象名称不能超过 80 个字符"))
        kind = str(form.get("type") or "public").strip().lower() or "public"
        if kind not in {"public", "private"}:
            return JSONResponse(fail("形象类型只能是 public（公共）或 private（个人）"))
        uid = first_user(str(form.get("username") or ""), str(form.get("user_id") or ""))
        if kind == "private" and not uid:
            return JSONResponse(fail("个人形象必须填写用户ID"))
        filename = str(form.get("filename") or "")
        ext = Path(filename).suffix.lower()
        if ext not in VIDEO_EXTS:
            return JSONResponse(fail(f"不支持的视频格式：{ext or '未知'}"))
        try:
            filesize = int(form.get("filesize") or 0)
        except (TypeError, ValueError):
            return JSONResponse(fail("视频大小需在 1B–2GB 之间"))
        if filesize < 1 or filesize > MAX_VIDEO_BYTES:
            return JSONResponse(fail("视频大小需在 1B–2GB 之间"))
        try:
            chunk_size = int(form.get("chunk_size") or DEFAULT_CHUNK)
        except (TypeError, ValueError):
            return JSONResponse(fail("分片大小无效"))
        if chunk_size < 1 or chunk_size > MAX_CHUNK:
            return JSONResponse(fail("分片大小无效"))
        if filesize >= MIN_CHUNK and chunk_size < MIN_CHUNK:
            return JSONResponse(fail("分片大小无效"))
    except ValueError as exc:
        return JSONResponse(fail(str(exc)))
    total = max(1, math.ceil(filesize / chunk_size))
    upload_id = new_id()
    now = iso(utc_now())
    (DATA_DIR / "uploads" / upload_id).mkdir(parents=True)
    STORE.insert("uploads", {
        "upload_id": upload_id,
        "kind": "avatar",
        "filename": filename,
        "filesize": filesize,
        "chunk_size": chunk_size,
        "total_chunks": total,
        "received": STORE.dumps([]),
        "name": name,
        "type": kind,
        "user_id": uid,
        "created_at": now,
        "updated_at": now,
    })
    return JSONResponse(envelope({"upload_id": upload_id, "total_chunks": total, "chunk_size": chunk_size}))


async def _avatar_chunk(form) -> JSONResponse:
    upload_id = str(form.get("upload_id") or "")
    chunk = form.get("chunk")
    chunk_index = form.get("chunk_index")
    if not upload_id or chunk is None or not hasattr(chunk, "read") or chunk_index in (None, ""):
        return JSONResponse(fail("分片参数不完整"))
    row = STORE.one("SELECT * FROM uploads WHERE upload_id=? AND kind='avatar'", (upload_id,))
    if not row:
        return JSONResponse(fail("上传任务不存在"))
    if form.get("total_chunks") not in (None, "",) and int(form.get("total_chunks")) != row["total_chunks"]:
        return JSONResponse(fail("分片总数与初始化不一致"))
    data = await chunk.read()
    if not data:
        return JSONResponse(fail("空分片"))
    try:
        index = int(chunk_index)
    except (TypeError, ValueError):
        return JSONResponse(fail("分片参数不完整"))
    total = int(row["total_chunks"])
    size = int(row["chunk_size"])
    expected = int(row["filesize"]) - size * (total - 1) if index == total - 1 else size
    if len(data) != expected:
        return JSONResponse(fail(f"{'最后分片大小应为' if index == total - 1 else '分片大小应为'} {expected} 字节"))
    dest = DATA_DIR / "uploads" / upload_id / f"{index:06d}.part"
    dest.write_bytes(data)
    received = set(STORE.loads(row["received"], []))
    received.add(index)
    STORE.update("uploads", "upload_id", upload_id, {
        "received": STORE.dumps(sorted(received)),
        "updated_at": iso(utc_now()),
    })
    return JSONResponse(envelope({"index": index, "received": len(received), "total_chunks": total}))


def _avatar_complete(upload_id: str) -> JSONResponse:
    if not upload_id:
        return JSONResponse(fail("缺少 upload_id"))
    row = STORE.one("SELECT * FROM uploads WHERE upload_id=? AND kind='avatar'", (upload_id,))
    if not row:
        return JSONResponse(fail("上传任务不存在"))
    total = int(row["total_chunks"])
    # 同上：以磁盘实际分片为准，避免并发上传时的计数竞态误报。
    parts_dir = DATA_DIR / "uploads" / upload_id
    missing = [i for i in range(total) if not (parts_dir / f"{i:06d}.part").is_file()]
    if missing:
        shown = ", ".join(str(i) for i in missing[:5])
        more = f" 等 {len(missing)} 片" if len(missing) > 5 else ""
        return JSONResponse(fail(f"分片 {shown}{more} 文件丢失（请重新上传）"))
    ident = new_avatar_id()
    folder = DATA_DIR / "avatars" / ident
    folder.mkdir(parents=True)
    ext = Path(row["filename"]).suffix.lower() or ".mp4"
    dest = folder / f"source{ext}"
    with dest.open("wb") as out:
        for i in range(total):
            part = DATA_DIR / "uploads" / upload_id / f"{i:06d}.part"
            if not part.exists():
                shutil.rmtree(folder, ignore_errors=True)
                return JSONResponse(fail(f"分片 {i} 文件丢失"))
            out.write(part.read_bytes())
    if dest.stat().st_size != int(row["filesize"]):
        shutil.rmtree(folder, ignore_errors=True)
        return JSONResponse(fail("合并后文件大小与声明不一致"))
    try:
        duration = float(media.probe(dest).get("duration") or 0)
    except Exception:
        shutil.rmtree(folder, ignore_errors=True)
        purge_upload(STORE, DATA_DIR, upload_id)
        return JSONResponse(fail("无法读取形象视频时长"))
    limit_msg = video_duration_limit_message(duration, CFG.get("max_video_duration_seconds"))
    if limit_msg:
        shutil.rmtree(folder, ignore_errors=True)
        purge_upload(STORE, DATA_DIR, upload_id)
        return JSONResponse(fail(limit_msg))
    now = iso(utc_now())
    STORE.insert("avatars", {
        "identifier": ident,
        "name": row["name"],
        "type": row["type"],
        "user_id": row.get("user_id"),
        "status": "queued",
        "bake_status": "processing",
        "bake_progress": 5,
        "bake_message": "上传成功，正在处理",
        "source_path": str(dest),
        "created_at": now,
    })
    purge_upload(STORE, DATA_DIR, upload_id)
    avatar = STORE.one("SELECT * FROM avatars WHERE identifier=?", (ident,))
    return JSONResponse(envelope(_avatar_public(avatar), "上传成功，正在处理"))


def _abort_avatar_upload(upload_id: str) -> JSONResponse:
    if not upload_id:
        return JSONResponse(fail("缺少 upload_id"))
    if len(upload_id) < 8:
        return JSONResponse(fail("上传任务不存在"))
    purge_upload(STORE, DATA_DIR, upload_id)
    return JSONResponse(envelope({"upload_id": upload_id}, "已取消并清理分片"))


async def _resolve_audio(form) -> tuple[Path | None, str]:
    upload_id = str(form.get("audio_upload_id") or "").strip()
    if upload_id:
        row = STORE.one("SELECT * FROM uploads WHERE upload_id=? AND kind='audio'", (upload_id,))
        if not row:
            return None, "上传任务不存在"
        path = DATA_DIR / "uploads" / upload_id / row["filename"]
        if not path.exists():
            return None, "音频文件为空"
        return path, row["filename"]
    audio = form.get("audio")
    if audio is None or not hasattr(audio, "read"):
        return None, "音频文件为空"
    filename = Path(getattr(audio, "filename", "") or "audio.wav").name
    ext = Path(filename).suffix.lower()
    if ext not in AUDIO_EXTS:
        return None, f"不支持的音频格式：{ext or '未知'}"
    data = await audio.read()
    if not data:
        return None, "音频文件为空"
    if len(data) > MAX_AUDIO_BYTES:
        return None, "音频不能超过 300MB"
    tmp = DATA_DIR / "uploads" / f"direct-{new_id()}"
    tmp.mkdir(parents=True)
    dest = tmp / filename
    dest.write_bytes(data)
    return dest, filename


def _make_token() -> str:
    issued = str(int(time.time()))
    digest = hmac.new(CFG["admin_key"].encode(), issued.encode(), hashlib.sha256).hexdigest()
    return f"{issued}.{digest}"


def _gate_ok(token: str | None) -> bool:
    if not token or "." not in token:
        return False
    issued, digest = token.split(".", 1)
    expect = hmac.new(CFG["admin_key"].encode(), issued.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(digest, expect):
        return False
    try:
        age = time.time() - int(issued)
    except ValueError:
        return False
    return 0 <= age <= int(CFG["idle_seconds"]) + 30


def _set_cookie(response: JSONResponse, token: str) -> None:
    response.set_cookie(
        COOKIE,
        token,
        httponly=True,
        samesite="lax",
        path="/",
        max_age=int(CFG["idle_seconds"]),
    )


app = build_app()

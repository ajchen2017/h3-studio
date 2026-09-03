"""Standalone post-production: a 3-track timeline (video / original audio /
SFX audio) that works on either a just-generated job's video or an
uploaded local video - independent of the main generation flow, so it's
useful even on a day nothing was generated.

Track model (v1 - whole-segment editing, not arbitrary mid-clip splicing):
- track1 (video): one video clip, with in/out trim points.
- track2 (original audio): no file of its own - it's the video's own
  baked-in audio - so cut/delete/paste here just toggle whether it's
  included in the final mix.
- track3 (SFX audio): one SFX clip (generated via HunyuanVideo-Foley,
  conditioned on track1's current video), positioned at an offset into
  the timeline.

Sessions are file-backed under postprod_sessions/{session_id}/, same
persistence pattern as JOBS_DIR/{job_id}/ in main.py.
"""

import asyncio
import json
import shutil
import subprocess
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, Form, UploadFile, File
from fastapi.responses import FileResponse, JSONResponse

import auth
from comfy_client import queue_via_prompt_api, get_history, check_comfyui_alive, ensure_comfyui_memory_healthy
from foley_client import build_video_prompt

router = APIRouter(prefix="/api/postprod")

COMFYUI_INPUT_DIR = Path(r"C:\Users\user\ai\ComfyUI\ComfyUI\input")
COMFYUI_OUTPUT_DIR = Path(r"C:\Users\user\ai\ComfyUI\ComfyUI\output")
SESSIONS_DIR = Path(__file__).parent / "postprod_sessions"
SESSIONS_DIR.mkdir(exist_ok=True)
FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"

SESSIONS: dict[str, dict] = {}


def _state_path(sid: str) -> Path:
    return SESSIONS_DIR / sid / "state.json"


def _save(sid: str):
    path = _state_path(sid)
    path.parent.mkdir(exist_ok=True, parents=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(SESSIONS[sid], f, ensure_ascii=False)


def _load_all():
    if not SESSIONS_DIR.exists():
        return
    for d in SESSIONS_DIR.iterdir():
        sp = d / "state.json"
        if sp.exists():
            try:
                with open(sp, "r", encoding="utf-8") as f:
                    SESSIONS[d.name] = json.load(f)
            except Exception:
                pass


_load_all()


def _probe_duration(path: Path) -> float:
    out = subprocess.check_output([
        FFPROBE, "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(path),
    ])
    return float(out.strip())


def _new_state(sid: str) -> dict:
    return {
        "session_id": sid,
        "track1": {"file": None, "duration": None, "in": 0.0, "out": None, "clipboard": None},
        "track2": {"included": True, "clipboard": None},
        "track3": {"file": None, "duration": None, "offset": 0.0, "gain_db": 0.0, "clipboard": None},
        "sfx_pending": None,
        "mixed_video": None,
    }


@router.post("/sessions")
async def create_session(user: dict = Depends(auth.get_admin_user)):
    sid = uuid.uuid4().hex[:12]
    SESSIONS[sid] = _new_state(sid)
    _save(sid)
    return {"session_id": sid}


@router.get("/sessions/{sid}")
async def get_session(sid: str, user: dict = Depends(auth.get_admin_user)):
    s = SESSIONS.get(sid)
    if not s:
        return JSONResponse({"error": "not found"}, status_code=404)
    return s


@router.post("/sessions/{sid}/track1/upload")
async def upload_track1(sid: str, file: UploadFile = File(...), user: dict = Depends(auth.get_admin_user)):
    s = SESSIONS.get(sid)
    if not s:
        return JSONResponse({"error": "not found"}, status_code=404)
    dest_dir = SESSIONS_DIR / sid
    dest_dir.mkdir(exist_ok=True, parents=True)
    ext = Path(file.filename or "video.mp4").suffix or ".mp4"
    dest = dest_dir / f"track1_{uuid.uuid4().hex[:8]}{ext}"
    with open(dest, "wb") as f:
        f.write(await file.read())
    duration = _probe_duration(dest)
    s["track1"] = {"file": str(dest), "duration": duration, "in": 0.0, "out": duration, "clipboard": None}
    _save(sid)
    return s["track1"]


def set_track1_from_file(sid: str, src_path: Path) -> dict:
    """Called from main.py to seed track1 from an existing generation
    job's final video. Lives here (not exposed as a job_id-aware route in
    this module) so postprod.py never has to import main.py / JOBS -
    main.py already owns that lookup, this just copies the resolved path
    in."""
    s = SESSIONS.get(sid)
    if not s:
        raise ValueError("session not found")
    dest_dir = SESSIONS_DIR / sid
    dest_dir.mkdir(exist_ok=True, parents=True)
    dest = dest_dir / f"track1_{uuid.uuid4().hex[:8]}.mp4"
    shutil.copy(src_path, dest)
    duration = _probe_duration(dest)
    s["track1"] = {"file": str(dest), "duration": duration, "in": 0.0, "out": duration, "clipboard": None}
    _save(sid)
    return s["track1"]


@router.post("/sessions/{sid}/track1/trim")
async def trim_track1(sid: str, in_sec: float = Form(...), out_sec: float = Form(...), user: dict = Depends(auth.get_admin_user)):
    s = SESSIONS.get(sid)
    if not s or not s["track1"]["file"]:
        return JSONResponse({"error": "沒有影片可裁切"}, status_code=400)
    duration = s["track1"]["duration"]
    in_sec = max(0.0, min(in_sec, duration))
    out_sec = max(in_sec, min(out_sec, duration))
    s["track1"]["in"] = in_sec
    s["track1"]["out"] = out_sec
    _save(sid)
    return s["track1"]


@router.post("/sessions/{sid}/track3/offset")
async def set_track3_offset(sid: str, offset_sec: float = Form(...), user: dict = Depends(auth.get_admin_user)):
    s = SESSIONS.get(sid)
    if not s:
        return JSONResponse({"error": "not found"}, status_code=404)
    s["track3"]["offset"] = max(0.0, offset_sec)
    _save(sid)
    return s["track3"]


@router.post("/sessions/{sid}/track3/gain")
async def set_track3_gain(sid: str, gain_db: float = Form(...), user: dict = Depends(auth.get_admin_user)):
    s = SESSIONS.get(sid)
    if not s:
        return JSONResponse({"error": "not found"}, status_code=404)
    s["track3"]["gain_db"] = max(-40.0, min(20.0, gain_db))
    _save(sid)
    return s["track3"]


@router.post("/sessions/{sid}/track/{track_num}/{action}")
async def track_action(sid: str, track_num: int, action: str, user: dict = Depends(auth.get_admin_user)):
    s = SESSIONS.get(sid)
    if not s:
        return JSONResponse({"error": "not found"}, status_code=404)
    if track_num not in (1, 2, 3) or action not in ("cut", "delete", "paste"):
        return JSONResponse({"error": "invalid track or action"}, status_code=400)

    t = s[f"track{track_num}"]

    if track_num == 2:
        # No file of its own (it's the video's baked-in audio) - cut/
        # delete/paste just toggle whether it's included in the mix.
        if action == "cut":
            t["clipboard"] = t["included"]
            t["included"] = False
        elif action == "delete":
            t["included"] = False
        else:  # paste
            t["included"] = t["clipboard"] if t["clipboard"] is not None else True
    else:
        if action == "cut":
            if not t["file"]:
                return JSONResponse({"error": "這一軌沒有內容可剪下"}, status_code=400)
            t["clipboard"] = {"file": t["file"], "duration": t["duration"]}
            t["file"] = None
            t["duration"] = None
            if track_num == 1:
                t["in"], t["out"] = 0.0, None
            else:
                t["offset"] = 0.0
        elif action == "delete":
            t["file"] = None
            t["duration"] = None
            if track_num == 1:
                t["in"], t["out"] = 0.0, None
            else:
                t["offset"] = 0.0
        else:  # paste
            if not t.get("clipboard"):
                return JSONResponse({"error": "剪貼簿是空的"}, status_code=400)
            t["file"] = t["clipboard"]["file"]
            t["duration"] = t["clipboard"]["duration"]
            if track_num == 1:
                t["in"], t["out"] = 0.0, t["duration"]

    _save(sid)
    return t


def _wait_for_prompt(prompt_id, on_tick=None):
    while True:
        hist = get_history(prompt_id)
        rec = hist.get(prompt_id)
        if rec:
            status = rec.get("status", {})
            if status.get("status_str") in ("success", "error"):
                return rec
        if on_tick:
            on_tick()
        time.sleep(5)


def _run_sfx(sid: str, prompt: str, negative_prompt: str, cfg_scale: float, steps: int, seed: int | None):
    s = SESSIONS[sid]
    sfx = s["sfx_pending"]
    try:
        sfx["status"] = "running"
        _save(sid)

        t1 = s["track1"]
        in_sec = t1["in"]
        out_sec = t1["out"] if t1["out"] is not None else t1["duration"]
        duration = max(0.1, out_sec - in_sec)

        video_copy_name = f"{uuid.uuid4().hex}_sfx_src.mp4"
        shutil.copy(t1["file"], COMFYUI_INPUT_DIR / video_copy_name)

        wf = build_video_prompt(
            video_filename=video_copy_name,
            prompt=prompt,
            negative_prompt=negative_prompt,
            duration=duration,
            cfg_scale=cfg_scale,
            steps=steps,
            seed=seed if seed is not None else int(time.time()),
            filename_prefix=f"audio/h3studio_postprod_{sid}_sfx",
        )
        ensure_comfyui_memory_healthy()
        prompt_id = queue_via_prompt_api(wf, client_id=f"h3studio-postprod-{sid}")
        sfx["prompt_id"] = prompt_id
        _save(sid)

        rec = _wait_for_prompt(prompt_id, on_tick=lambda: _save(sid))
        status = rec.get("status", {})
        if status.get("status_str") != "success":
            sfx["status"] = "error"
            sfx["error"] = f"SFX 生成失敗：{json.dumps(status)[:500]}"
            _save(sid)
            return

        out_file = None
        for node_out in rec.get("outputs", {}).values():
            for a in node_out.get("audio", []):
                out_file = COMFYUI_OUTPUT_DIR / a["subfolder"] / a["filename"]
        if out_file is None or not out_file.exists():
            sfx["status"] = "error"
            sfx["error"] = "ComfyUI 回報成功但沒有輸出音檔"
            _save(sid)
            return

        sfx["audio_file"] = str(out_file)
        sfx["duration"] = duration
        sfx["status"] = "done"
        _save(sid)
    except Exception as e:
        sfx["status"] = "error"
        sfx["error"] = str(e)
        _save(sid)


@router.post("/sessions/{sid}/sfx")
async def generate_sfx(
    sid: str,
    prompt: str = Form(...),
    negative_prompt: str = Form(""),
    cfg_scale: float = Form(5.0),
    steps: int = Form(50),
    seed: int | None = Form(None),
    user: dict = Depends(auth.get_admin_user),
):
    s = SESSIONS.get(sid)
    if not s or not s["track1"]["file"]:
        return JSONResponse({"error": "第一軌還沒有影片，無法生成音效"}, status_code=400)
    alive, detail = check_comfyui_alive()
    if not alive:
        return JSONResponse({"error": f"ComfyUI 目前無法連線（{detail}），請確認 ComfyUI 是否正在執行後再試一次。"}, status_code=503)
    try:
        ensure_comfyui_memory_healthy()
    except RuntimeError as e:
        return JSONResponse({"error": str(e)}, status_code=503)

    s["sfx_pending"] = {"status": "queued", "prompt": prompt, "audio_file": None, "error": None, "started_at": time.time()}
    _save(sid)
    asyncio.get_event_loop().run_in_executor(
        None, _run_sfx, sid, prompt, negative_prompt, cfg_scale, steps, seed,
    )
    return {"status": "queued"}


@router.get("/sessions/{sid}/sfx/audio")
async def sfx_preview_audio(sid: str, user: dict = Depends(auth.get_admin_user_flexible)):
    s = SESSIONS.get(sid)
    sfx = s.get("sfx_pending") if s else None
    if not sfx or sfx.get("status") != "done":
        return JSONResponse({"error": "not ready"}, status_code=404)
    path = Path(sfx["audio_file"])
    if not path.exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(path, media_type="audio/flac")


@router.post("/sessions/{sid}/sfx/insert")
async def insert_sfx(sid: str, user: dict = Depends(auth.get_admin_user)):
    s = SESSIONS.get(sid)
    if not s:
        return JSONResponse({"error": "not found"}, status_code=404)
    sfx = s.get("sfx_pending")
    if not sfx or sfx.get("status") != "done":
        return JSONResponse({"error": "沒有已完成的音效可插入"}, status_code=400)
    s["track3"] = {"file": sfx["audio_file"], "duration": sfx.get("duration"), "offset": 0.0, "gain_db": 0.0, "clipboard": None}
    s["sfx_pending"] = None
    _save(sid)
    return s["track3"]


@router.get("/sessions/{sid}/video")
async def get_track1_video(sid: str, user: dict = Depends(auth.get_admin_user_flexible)):
    s = SESSIONS.get(sid)
    if not s or not s["track1"]["file"]:
        return JSONResponse({"error": "not ready"}, status_code=404)
    return FileResponse(s["track1"]["file"], media_type="video/mp4")


def _compose(video_path: Path, in_sec: float, out_sec: float, include_original_audio: bool,
             sfx_path: Path | None, sfx_offset: float, sfx_gain_db: float, out_path: Path):
    """Trims track1 to [in,out] (input-side -ss/-to, applies only to the
    video input), then combines its own audio (if enabled) with the SFX
    clip (if present) - gained by sfx_gain_db and shifted to sfx_offset
    seconds into that trimmed timeline via adelay. amix keeps both audible
    rather than one replacing the other. Video stream is always a plain
    copy - never re-encoded."""
    cmd = [FFMPEG, "-y", "-ss", str(in_sec), "-to", str(out_sec), "-i", str(video_path)]

    if sfx_path:
        cmd += ["-i", str(sfx_path)]
        delay_ms = int(max(0.0, sfx_offset) * 1000)
        if include_original_audio:
            filter_complex = (
                f"[1:a]volume={sfx_gain_db}dB,adelay={delay_ms}|{delay_ms}[sfx];"
                f"[0:a][sfx]amix=inputs=2:duration=first:dropout_transition=0[aout]"
            )
        else:
            filter_complex = f"[1:a]volume={sfx_gain_db}dB,adelay={delay_ms}|{delay_ms}[aout]"
        cmd += [
            "-filter_complex", filter_complex, "-map", "0:v", "-map", "[aout]",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
        ]
    elif include_original_audio:
        cmd += ["-c", "copy"]
    else:
        cmd += ["-c:v", "copy", "-an"]

    cmd += [str(out_path)]
    subprocess.run(cmd, check=True, capture_output=True)


@router.post("/sessions/{sid}/mix")
async def mix(sid: str, user: dict = Depends(auth.get_admin_user)):
    s = SESSIONS.get(sid)
    if not s or not s["track1"]["file"]:
        return JSONResponse({"error": "第一軌沒有影片"}, status_code=400)

    t1, t2, t3 = s["track1"], s["track2"], s["track3"]
    in_sec = t1["in"]
    out_sec = t1["out"] if t1["out"] is not None else t1["duration"]
    out_path = SESSIONS_DIR / sid / "mixed.mp4"

    try:
        _compose(
            Path(t1["file"]), in_sec, out_sec, t2["included"],
            Path(t3["file"]) if t3["file"] else None, t3["offset"], t3.get("gain_db", 0.0), out_path,
        )
    except subprocess.CalledProcessError as e:
        detail = e.stderr.decode("utf-8", errors="replace")[-500:] if e.stderr else str(e)
        return JSONResponse({"error": f"合成失敗：{detail}"}, status_code=500)

    s["mixed_video"] = str(out_path)
    _save(sid)
    return {"status": "done"}


@router.get("/sessions/{sid}/mixed")
async def get_mixed(sid: str, user: dict = Depends(auth.get_admin_user_flexible)):
    s = SESSIONS.get(sid)
    if not s or not s.get("mixed_video"):
        return JSONResponse({"error": "not ready"}, status_code=404)
    return FileResponse(s["mixed_video"], media_type="video/mp4")

import asyncio
import json
import math
import os
import shutil
import subprocess
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from fastapi import Depends, FastAPI, Request, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

import psutil

import auth
import email_service
from workflow_builder import build_workflow, get_model_info
from character_workflow import build_character_image_workflow, CHECKPOINT_NAME as CHARACTER_PHOTO_CHECKPOINT_NAME
from comfy_client import queue_via_prompt_api, get_history, check_comfyui_alive, ensure_comfyui_memory_healthy, interrupt_current
import prompt_rewriter
import postprod


def _load_dotenv():
    """No new dependency for something this small - just KEY=VALUE lines,
    same shape python-dotenv would read. Real values (SMTP password, etc.)
    live only in the gitignored .env file, never in this source file."""
    env_path = Path(__file__).parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


_load_dotenv()
auth.seed_password_if_missing(os.environ.get("ADMIN_INITIAL_PASSWORD", ""))

COMFYUI_INPUT_DIR = Path(r"C:\Users\user\ai\ComfyUI\ComfyUI\input")
COMFYUI_OUTPUT_DIR = Path(r"C:\Users\user\ai\ComfyUI\ComfyUI\output")
# Existence = enabled. start_comfyui.bat checks this sentinel at boot and
# conditionally adds --use-sage-attention - a plain file is trivial to
# check from batch, unlike parsing JSON there. Toggling this endpoint does
# NOT restart ComfyUI itself (would kill whatever job is currently
# running) - it only takes effect the next time ComfyUI restarts, whether
# that's manual or the memory-guard's automatic restart.
SAGE_ATTENTION_FLAG_PATH = Path(r"C:\Users\user\ai\ComfyUI\sage_attention.enabled")
JOBS_DIR = Path(__file__).parent / "jobs"
UPLOADS_DIR = Path(__file__).parent / "uploads"
VOICE_PROFILES_DIR = Path(__file__).parent / "voice_profiles"
VOICE_PROFILES_INDEX = VOICE_PROFILES_DIR / "index.json"
PROJECTS_DIR = Path(__file__).parent / "projects"
REF_IMAGES_DIR = Path(__file__).parent / "ref_images"
REF_IMAGES_INDEX = REF_IMAGES_DIR / "index.json"
FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"

JOBS_DIR.mkdir(exist_ok=True)
UPLOADS_DIR.mkdir(exist_ok=True)
VOICE_PROFILES_DIR.mkdir(exist_ok=True)
PROJECTS_DIR.mkdir(exist_ok=True)
REF_IMAGES_DIR.mkdir(exist_ok=True)

app = FastAPI(title="H3 Studio API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def no_store_api_responses(request, call_next):
    """Every /api/* GET serves mutable state (job status, project data,
    generated video/image files) from URLs that stay identical across
    regenerations/edits - without this, the browser's own HTTP cache can
    silently serve a stale response and nothing here ever notices (already
    confirmed twice: a stale /api/projects/{id} GET got autosaved back over
    a fresh edit, and a stale segment video looked "unchanged" after a
    real regenerate). Blanket no-store on every API response is simpler
    and more robust than chasing this endpoint-by-endpoint."""
    response = await call_next(request)
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


app.include_router(postprod.router)

JOBS: dict[str, dict] = {}


# ── Auth ─────────────────────────────────────────────────────
@app.post("/api/auth/login")
async def login(password: str = Form(...)):
    if not auth.is_password_set():
        return JSONResponse({"error": "尚未設定密碼，請先用「忘記密碼」流程設定一組"}, status_code=400)
    if not auth.check_password(password):
        return JSONResponse({"error": "密碼錯誤"}, status_code=401)
    token = auth.create_token({"sub": "admin"})
    return {"token": token}


@app.get("/api/auth/me")
async def auth_me(user: dict = Depends(auth.get_admin_user)):
    return {"ok": True}


@app.post("/api/auth/change-password")
async def change_password(
    current_password: str = Form(...),
    new_password: str = Form(...),
    user: dict = Depends(auth.get_admin_user),
):
    if not auth.check_password(current_password):
        return JSONResponse({"error": "目前密碼不正確"}, status_code=400)
    if len(new_password) < 6:
        return JSONResponse({"error": "新密碼至少需要 6 個字元"}, status_code=400)
    auth.set_password(new_password)
    return {"ok": True}


@app.post("/api/auth/forgot-password")
async def forgot_password():
    token = auth.create_reset_token()
    reset_url = f"https://studio.umaya.tw/?reset_token={token}"
    html = email_service.build_reset_email_html(reset_url)
    sent = email_service.send_email(auth.ADMIN_EMAIL, "H3 Studio 管理密碼重設", html)
    if not sent:
        return JSONResponse({"error": "寄送失敗，請稍後再試"}, status_code=502)
    return {"ok": True}


@app.post("/api/auth/reset-password")
async def reset_password(token: str = Form(...), new_password: str = Form(...)):
    if not auth.verify_reset_token(token):
        return JSONResponse({"error": "連結無效或已過期，請重新申請"}, status_code=400)
    if len(new_password) < 6:
        return JSONResponse({"error": "新密碼至少需要 6 個字元"}, status_code=400)
    auth.set_password(new_password)
    return {"ok": True}


def _job_state_path(job_id: str) -> Path:
    return JOBS_DIR / job_id / "state.json"


def _save_job_state(job_id: str):
    path = _job_state_path(job_id)
    path.parent.mkdir(exist_ok=True, parents=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(JOBS[job_id], f, ensure_ascii=False)


def _load_all_job_states():
    """Rehydrate JOBS from disk on startup so a backend restart doesn't
    orphan in-flight generations from the frontend's point of view."""
    if not JOBS_DIR.exists():
        return
    for job_dir in JOBS_DIR.iterdir():
        state_file = job_dir / "state.json"
        if state_file.exists():
            try:
                with open(state_file, "r", encoding="utf-8") as f:
                    job = json.load(f)
                # A job that was mid-flight when the process died can never
                # resume (its worker thread is gone) - surface that honestly
                # rather than showing a stale "running" forever.
                if job.get("status") in ("queued", "running", "concatenating"):
                    job["status"] = "error"
                    job["error"] = "後端重啟導致此任務中斷，請重新送出生成"
                JOBS[job_dir.name] = job
            except Exception:
                continue


_load_all_job_states()


def _save_upload(upload: UploadFile, dest_dir: Path) -> str:
    ext = Path(upload.filename).suffix
    name = f"{uuid.uuid4().hex}{ext}"
    dest = dest_dir / name
    with open(dest, "wb") as f:
        shutil.copyfileobj(upload.file, f)
    return name


def _load_voice_profiles() -> list[dict]:
    if not VOICE_PROFILES_INDEX.exists():
        return []
    with open(VOICE_PROFILES_INDEX, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_voice_profiles(profiles: list[dict]):
    with open(VOICE_PROFILES_INDEX, "w", encoding="utf-8") as f:
        json.dump(profiles, f, ensure_ascii=False)


@app.get("/api/voice-profiles")
async def list_voice_profiles(user: dict = Depends(auth.get_admin_user)):
    return {"profiles": _load_voice_profiles()}


@app.post("/api/voice-profiles")
async def create_voice_profile(name: str = Form(...), file: UploadFile = File(...), user: dict = Depends(auth.get_admin_user)):
    ext = Path(file.filename).suffix or ".webm"
    profile_id = uuid.uuid4().hex[:12]
    filename = f"{profile_id}{ext}"
    dest = VOICE_PROFILES_DIR / filename
    with open(dest, "wb") as f:
        shutil.copyfileobj(file.file, f)

    # Also drop a copy into ComfyUI's input/ folder under a stable name so
    # it can be selected directly as a ref_audio filename by the generate
    # step without a separate re-upload step.
    comfy_filename = f"voiceprofile_{profile_id}{ext}"
    shutil.copy(dest, COMFYUI_INPUT_DIR / comfy_filename)

    try:
        duration = _probe_duration(dest)
    except Exception:
        duration = None

    profile = {
        "id": profile_id,
        "name": name,
        "filename": filename,
        "comfy_filename": comfy_filename,
        "duration": duration,
        "created_at": time.time(),
    }
    profiles = _load_voice_profiles()
    profiles.append(profile)
    _save_voice_profiles(profiles)
    return profile


@app.delete("/api/voice-profiles/{profile_id}")
async def delete_voice_profile(profile_id: str, user: dict = Depends(auth.get_admin_user)):
    profiles = _load_voice_profiles()
    match = next((p for p in profiles if p["id"] == profile_id), None)
    if not match:
        return JSONResponse({"error": "not found"}, status_code=404)
    (VOICE_PROFILES_DIR / match["filename"]).unlink(missing_ok=True)
    (COMFYUI_INPUT_DIR / match["comfy_filename"]).unlink(missing_ok=True)
    profiles = [p for p in profiles if p["id"] != profile_id]
    _save_voice_profiles(profiles)
    return {"ok": True}


@app.get("/api/voice-profiles/{profile_id}/audio")
async def get_voice_profile_audio(profile_id: str, user: dict = Depends(auth.get_admin_user_flexible)):
    profiles = _load_voice_profiles()
    match = next((p for p in profiles if p["id"] == profile_id), None)
    if not match:
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(VOICE_PROFILES_DIR / match["filename"])


def _load_ref_image_library() -> list[dict]:
    if not REF_IMAGES_INDEX.exists():
        return []
    with open(REF_IMAGES_INDEX, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_ref_image_library(entries: list[dict]):
    with open(REF_IMAGES_INDEX, "w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False)


@app.post("/api/utils/remove-background")
async def remove_background(file: UploadFile = File(...), user: dict = Depends(auth.get_admin_user)):
    """One-off transform, not tied to the ref-image library - lets the user
    preview a white-background version of an uploaded (real, not
    AI-generated) photo before deciding whether to save that version into
    the library (2026-09-02 request). rembg does the actual foreground/
    background segmentation (U2Net, downloads its model weights to
    ~/.u2net on first use); this endpoint just composites the isolated
    foreground onto solid white afterward. Imported lazily so a plain
    server boot never pays rembg's import cost for a feature that might
    not get used this session."""
    from rembg import remove as rembg_remove
    from PIL import Image
    import io

    data = await file.read()
    foreground_png = rembg_remove(data)
    foreground = Image.open(io.BytesIO(foreground_png)).convert("RGBA")
    white_bg = Image.new("RGBA", foreground.size, (255, 255, 255, 255))
    composited = Image.alpha_composite(white_bg, foreground).convert("RGB")

    out_buf = io.BytesIO()
    composited.save(out_buf, format="PNG")
    out_buf.seek(0)
    return StreamingResponse(out_buf, media_type="image/png")


@app.get("/api/ref-image-library")
async def list_ref_image_library(user: dict = Depends(auth.get_admin_user)):
    return {"images": _load_ref_image_library()}


@app.post("/api/ref-image-library")
async def add_ref_image_library(name: str = Form(...), file: UploadFile = File(...), user: dict = Depends(auth.get_admin_user)):
    ext = Path(file.filename).suffix or ".png"
    image_id = uuid.uuid4().hex[:12]
    # Saved directly into ComfyUI's input/ dir under a stable name, same
    # as voice profiles - the workflow reads reference images from there,
    # and /api/uploads/image/{filename} already serves it back for
    # thumbnail preview without needing a separate endpoint.
    comfy_filename = f"reflib_{image_id}{ext}"
    dest = COMFYUI_INPUT_DIR / comfy_filename
    with open(dest, "wb") as f:
        shutil.copyfileobj(file.file, f)

    entry = {"id": image_id, "name": name, "filename": comfy_filename, "created_at": time.time()}
    entries = _load_ref_image_library()
    entries.append(entry)
    _save_ref_image_library(entries)
    return entry


@app.delete("/api/ref-image-library/{image_id}")
async def delete_ref_image_library(image_id: str, user: dict = Depends(auth.get_admin_user)):
    entries = _load_ref_image_library()
    match = next((e for e in entries if e["id"] == image_id), None)
    if not match:
        return JSONResponse({"error": "not found"}, status_code=404)
    (COMFYUI_INPUT_DIR / match["filename"]).unlink(missing_ok=True)
    entries = [e for e in entries if e["id"] != image_id]
    _save_ref_image_library(entries)
    return {"ok": True}


@app.post("/api/characters/extract")
async def extract_characters_endpoint(text: str = Form(...), user: dict = Depends(auth.get_admin_user)):
    loop = asyncio.get_event_loop()
    try:
        characters = await loop.run_in_executor(None, prompt_rewriter.extract_characters, text)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"error": f"人物抽取失敗（Haiku API）：{e}"}, status_code=503)
    return {"characters": characters}


def _generate_character_photo_sync(description: str, seed: int, name: str = "") -> dict:
    """Queues one SD3.5 portrait, waits for it (image gen is fast enough not
    to need the job/resume machinery video generation has), and copies the
    result into ComfyUI's input/ dir under a preview filename. Deliberately
    does NOT register it in the ref-image library yet - the user reviews the
    photo first and explicitly saves it via /api/characters/save-to-library
    (see that endpoint), so a bad generation never silently clutters the
    library."""
    prefix = f"character/h3studio_char_{uuid.uuid4().hex[:8]}"
    wf = build_character_image_workflow(description, seed, filename_prefix=prefix)
    prompt_id = queue_via_prompt_api(wf, client_id=f"h3studio-char-{uuid.uuid4().hex[:8]}")

    # SD3.5 + its 3 text encoders are a separate model set from the H3/music
    # models ComfyUI usually has loaded - a cold load (first character photo
    # since a restart, or right after ensure_comfyui_memory_healthy() just
    # restarted ComfyUI) has taken several minutes in practice, well past a
    # plain 180s budget, even though the actual sampling itself is fast.
    deadline = time.time() + 480
    out_file = None
    while time.time() < deadline:
        hist = get_history(prompt_id)
        rec = hist.get(prompt_id)
        if rec:
            status = rec.get("status", {})
            if status.get("status_str") == "success":
                for node_out in rec.get("outputs", {}).values():
                    for img in node_out.get("images", []):
                        out_file = COMFYUI_OUTPUT_DIR / img["subfolder"] / img["filename"]
                break
            if status.get("status_str") == "error":
                raise RuntimeError(f"生成失敗：{json.dumps(status)[:300]}")
        time.sleep(3)
    if out_file is None or not out_file.exists():
        raise RuntimeError("生成逾時或找不到輸出檔案")

    image_id = uuid.uuid4().hex[:12]
    comfy_filename = f"charpreview_{image_id}{out_file.suffix}"
    shutil.copy(out_file, COMFYUI_INPUT_DIR / comfy_filename)

    return {"id": image_id, "name": (name.strip() or description[:40]), "filename": comfy_filename, "created_at": time.time()}


@app.post("/api/characters/generate-photo")
async def generate_character_photo(
    description: str = Form(...),
    seed: int | None = Form(None),
    name: str = Form(""),
    user: dict = Depends(auth.get_admin_user),
):
    alive, detail = check_comfyui_alive()
    if not alive:
        return JSONResponse(
            {"error": f"ComfyUI 目前無法連線（{detail}），請確認 ComfyUI 是否正在執行後再試一次。"},
            status_code=503,
        )
    try:
        ensure_comfyui_memory_healthy()
    except RuntimeError as e:
        return JSONResponse({"error": str(e)}, status_code=503)

    actual_seed = seed if seed is not None else int(time.time() * 1000) % (2**32)
    loop = asyncio.get_event_loop()
    try:
        entry = await loop.run_in_executor(None, _generate_character_photo_sync, description, actual_seed, name)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=503)
    return entry


@app.post("/api/characters/save-to-library")
async def save_character_photo_to_library(
    filename: str = Form(...),
    name: str = Form(...),
    user: dict = Depends(auth.get_admin_user),
):
    """Commits a previously-generated preview (see generate_character_photo,
    filename like charpreview_xxx.png already sitting in COMFYUI_INPUT_DIR)
    into the ref-image library. Kept as a separate step so an unreviewed or
    rejected generation never ends up in the library automatically."""
    src = COMFYUI_INPUT_DIR / filename
    if not src.exists() or not filename.startswith("charpreview_"):
        return JSONResponse({"error": "找不到預覽圖片，請重新生成"}, status_code=404)

    image_id = uuid.uuid4().hex[:12]
    comfy_filename = f"reflib_{image_id}{src.suffix}"
    shutil.copy(src, COMFYUI_INPUT_DIR / comfy_filename)

    entry = {"id": image_id, "name": name.strip() or filename, "filename": comfy_filename, "created_at": time.time()}
    entries = _load_ref_image_library()
    entries.append(entry)
    _save_ref_image_library(entries)
    return entry


def _translate_text(text: str, target: str, source: str = "auto") -> str:
    """Uses Google Translate's public web endpoint (no API key needed) -
    the same unauthenticated endpoint translate.google.com's own page
    calls client-side. Fine for a local personal tool; not for production
    scale (no SLA, could break/rate-limit without notice)."""
    params = urllib.parse.urlencode({
        "client": "gtx", "sl": source, "tl": target, "dt": "t", "q": text,
    })
    url = f"https://translate.googleapis.com/translate_a/single?{params}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read())
    return "".join(seg[0] for seg in data[0] if seg[0])


@app.post("/api/translate")
async def translate(text: str = Form(...), target: str = Form(...), source: str = Form("auto"), user: dict = Depends(auth.get_admin_user)):
    try:
        translated = _translate_text(text, target, source)
        return {"translated": translated}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=502)


@app.post("/api/upload/image")
async def upload_image(file: UploadFile = File(...), user: dict = Depends(auth.get_admin_user)):
    name = _save_upload(file, COMFYUI_INPUT_DIR)
    return {"filename": name}


@app.post("/api/upload/audio")
async def upload_audio(file: UploadFile = File(...), user: dict = Depends(auth.get_admin_user)):
    name = _save_upload(file, COMFYUI_INPUT_DIR)
    return {"filename": name}


@app.post("/api/upload/video")
async def upload_video(file: UploadFile = File(...), user: dict = Depends(auth.get_admin_user)):
    name = _save_upload(file, COMFYUI_INPUT_DIR)
    return {"filename": name}


@app.post("/api/upload/music")
async def upload_music(file: UploadFile = File(...), user: dict = Depends(auth.get_admin_user)):
    name = _save_upload(file, COMFYUI_INPUT_DIR)
    return {"filename": name}


def _probe_duration(path: Path) -> float:
    out = subprocess.check_output([
        FFPROBE, "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(path),
    ])
    return float(out.strip())


def _extract_last_frame(video_path: Path, out_png: Path):
    subprocess.run([
        FFMPEG, "-y", "-sseof", "-1", "-i", str(video_path),
        "-update", "1", "-frames:v", "1", str(out_png),
    ], check=True, capture_output=True)


def _extract_tail_clip(video_path: Path, out_mp4: Path, seconds: float = 5.0):
    subprocess.run([
        FFMPEG, "-y", "-sseof", f"-{seconds}", "-i", str(video_path),
        "-c", "copy", str(out_mp4),
    ], check=True, capture_output=True)


def _trim_audio(audio_path: Path, out_path: Path, max_seconds: float):
    """Trims from the start (head) up to max_seconds, or the whole clip if
    it's already shorter - this is the 'crop to the configured max video
    length' step for an uploaded background-music reference."""
    actual = _probe_duration(audio_path)
    take = min(actual, max_seconds)
    subprocess.run([
        FFMPEG, "-y", "-i", str(audio_path), "-t", str(take),
        "-c", "copy", str(out_path),
    ], check=True, capture_output=True)


def _concat_videos(segment_paths: list[Path], out_path: Path):
    list_file = out_path.with_suffix(".txt")
    with open(list_file, "w", encoding="utf-8") as f:
        for p in segment_paths:
            f.write(f"file '{p.as_posix()}'\n")
    subprocess.run([
        FFMPEG, "-y", "-f", "concat", "-safe", "0", "-i", str(list_file),
        "-c", "copy", str(out_path),
    ], check=True, capture_output=True)
    list_file.unlink(missing_ok=True)


def _postprocess_format(in_path: Path, out_path: Path, width: int, height: int, fps: int):
    subprocess.run([
        FFMPEG, "-y", "-i", str(in_path),
        "-vf", f"scale={width}:{height}:flags=lanczos,fps={fps}",
        "-c:v", "libx264", "-preset", "slow", "-crf", "16", "-pix_fmt", "yuv420p",
        "-c:a", "copy", str(out_path),
    ], check=True, capture_output=True)


def _wait_for_prompt(prompt_id: str, job: dict, on_tick=None) -> dict | None:
    """Returns the ComfyUI history record, or None if /api/jobs/{id}/stop
    set job["cancel_requested"] while this was waiting - checked once per
    poll tick, the actual GPU-side abort happens immediately via
    interrupt_current() in the stop endpoint, not on this cadence."""
    while True:
        if job.get("cancel_requested"):
            return None
        hist = get_history(prompt_id)
        rec = hist.get(prompt_id)
        if rec:
            status = rec.get("status", {})
            if status.get("status_str") in ("success", "error"):
                return rec
        if on_tick:
            on_tick()
        time.sleep(5)


def _job_n_segments(params: dict) -> int:
    segment_prompts = params.get("segment_prompts") or None
    if segment_prompts:
        # Per-segment prompts (from the segment table UI) are authoritative
        # about how many segments there are - duration_minutes is just
        # derived display info at that point, not a source of truth.
        return len(segment_prompts)
    total_seconds = params["duration_minutes"] * 60
    return max(1, math.ceil(total_seconds / params["segment_length_seconds"]))


def _initial_ref_images(params: dict, temp_files: list) -> list[str]:
    """Extend mode: derive an initial image reference from the last frame of
    the supplied reference video, unless the experimental direct ref_video
    path was explicitly requested. Independent of where in the segment loop
    a run starts/resumes - this is a static reference, not per-segment
    continuity, so it's safe (and correct) to recompute on every call."""
    ref_images = list(params.get("ref_image_filenames") or [])
    ref_video = params.get("ref_video_filename")
    use_ref_video_direct = params.get("use_ref_video_direct", False)
    if ref_video and not use_ref_video_direct:
        last_frame_name = f"{uuid.uuid4().hex}_lastframe.png"
        _extract_last_frame(COMFYUI_INPUT_DIR / ref_video, COMFYUI_INPUT_DIR / last_frame_name)
        ref_images = ref_images + [last_frame_name]
        temp_files.append(COMFYUI_INPUT_DIR / last_frame_name)
    return ref_images


def _continuation_ref_images(job: dict, params: dict, index: int, temp_files: list) -> tuple[list[str], int | None]:
    """Returns (ref_images, continuation_picture_n). For index > 0, chains
    from the IMMEDIATELY PRECEDING segment's own last frame - not just
    segment 0's - appended AFTER whatever subject/character reference the
    user selected (both are used together, up to the workflow's 3-slot
    cap), so a segment both keeps the character's face locked AND
    literally continues from wherever the previous segment's video ended,
    instead of re-imagining the scene from text alone each time (2026-08-26,
    replacing the old either/or _uses_own_continuity()/_own_continuity_
    ref_images() split - there was no real reason the two couldn't combine,
    per the official guide's own <Picture N>-as-continuation-anchor
    pattern, section 2.2/5.3). continuation_picture_n is the anchor's
    <Picture N> number (1-indexed, counting past however many subject refs
    already occupy earlier slots) for the caller to inject into
    retention_analysis - None if no continuation frame was added (index 0,
    previous segment not actually done yet, or the 3-slot cap was already
    full from subject refs alone)."""
    ref_images = _initial_ref_images(params, temp_files)
    if index <= 0 or len(ref_images) >= 3:
        return ref_images, None
    prev_seg = job["segments"][index - 1]
    prev_output = prev_seg.get("output_file")
    if not prev_output or not Path(prev_output).exists():
        return ref_images, None  # previous segment isn't real output yet - nothing to continue from
    cont_frame = f"{uuid.uuid4().hex}_seg{index - 1}_lastframe.png"
    _extract_last_frame(Path(prev_output), COMFYUI_INPUT_DIR / cont_frame)
    temp_files.append(COMFYUI_INPUT_DIR / cont_frame)
    picture_n = len(ref_images) + 1
    return ref_images + [cont_frame], picture_n


def _inject_continuation_anchor(seg_prompt: str, picture_n: int) -> str:
    """The continuation-anchor image (previous segment's actual last
    frame) doesn't exist yet at Haiku-rewrite time - it's only produced
    once the previous segment finishes rendering, well after this
    segment's text was frozen at submission - so it can't have been
    declared in retention_analysis originally. Splice the declaration in
    now, right before generation, the same way _fix_shot_timestamps()
    deterministically patches text post-Haiku elsewhere in this app."""
    anchor_line = (
        f"<Picture {picture_n}> ([Shot 1] first frame): fully_preserved - this shot "
        "begins exactly where the previous segment's video left off; the scene, lighting, "
        "and composition continue seamlessly from this frame."
    )
    marker = "retention_analysis:\n"
    idx = seg_prompt.find(marker)
    if idx != -1:
        insert_at = idx + len(marker)
        return seg_prompt[:insert_at] + anchor_line + "\n" + seg_prompt[insert_at:]
    marker2 = "detailed_description:"
    idx2 = seg_prompt.find(marker2)
    if idx2 == -1:
        return seg_prompt  # unexpected format - leave untouched rather than guess
    return seg_prompt[:idx2] + f"retention_analysis:\n{anchor_line}\n\n" + seg_prompt[idx2:]


def _preflight_validate_segments(job_id: str, params: dict, end_index: int | None = None) -> str | None:
    """Structurally validates every segment's workflow BEFORE committing to
    the real (potentially 10+ minute) multi-segment run - catches "why did
    this fail" cases (missing/deleted reference file, bad seed, typo'd
    model/LoRA filename) in seconds instead of only after earlier segments
    have already burned real GPU time (2026-09-02, user-requested: "不要等
    到十幾分鐘後才發現無法生成"). For each segment: build the exact same
    workflow _generate_segment() would, POST it to ComfyUI's /prompt (which
    validates node inputs synchronously, before anything executes, and
    returns node_errors immediately if something's wrong), then immediately
    interrupt_current() if it was accepted so it never actually samples.
    Segment index > 0's continuation-from-previous-frame reference doesn't
    exist yet at this point (nothing has run) - _continuation_ref_images()
    already degrades gracefully and just omits it, which is fine: this pass
    is only checking structural/file-availability issues, not verifying the
    actual continuity content.
    Returns None if every segment validates, otherwise a message describing
    the first segment that failed."""
    n_segments = _job_n_segments(params)
    if end_index is not None:
        n_segments = min(n_segments, end_index)
    fake_job = {"segments": [{"output_file": None} for _ in range(n_segments)]}
    temp_files = []
    base_seed = params.get("seed") or int(time.time())
    segment_prompts = params.get("segment_prompts") or None
    try:
        for i in range(n_segments):
            ref_images, _picture_n = _continuation_ref_images(fake_job, params, i, temp_files)
            seg_prompt = segment_prompts[i] if segment_prompts else params["prompt"]
            if not seg_prompt or not seg_prompt.strip():
                return f"第 {i + 1} 段的 prompt 是空的"
            use_ref_video_direct = params.get("use_ref_video_direct", False) and i == 0
            wf = build_workflow(
                prompt=seg_prompt,
                ref_image_filenames=ref_images[:3],
                ref_audio_filename=params.get("ref_audio_filename"),
                bg_music_filename=params.get("bg_music_filename"),
                ref_video_filename=params.get("ref_video_filename") if use_ref_video_direct else None,
                use_ref_video_direct=use_ref_video_direct,
                width=params.get("width", 864),
                height=params.get("height", 480),
                duration_seconds=params["segment_length_seconds"],
                seed=base_seed + i,
                ref_image_size=params.get("ref_image_size", "max"),
                filename_prefix=f"video/h3studio_preflight_{job_id}_seg{i:03d}",
                steps=params.get("steps", 8),
            )
            try:
                queue_via_prompt_api(wf, client_id=f"h3studio-preflight-{job_id}")
            except Exception as e:
                return f"第 {i + 1} 段驗證失敗：{e}"
            interrupt_current()
        return None
    finally:
        for f in temp_files:
            try:
                f.unlink(missing_ok=True)
            except OSError:
                pass


def _generate_segment(
    job_id: str, job: dict, params: dict, i: int, ref_images: list[str], bg_music_trimmed: str | None,
    base_seed: int, seed_override: int | None = None, continuation_picture_n: int | None = None,
) -> bool:
    """Generates exactly one segment (index i), updating job['segments'][i]
    and persisting state as it goes. Returns True on success; on failure or
    cancellation, job['segments'][i]['status'] is already set to 'error' or
    'cancelled' so the caller just needs to read it, not re-derive it.
    seed_override (2026-08-26) lets a single-segment regenerate try a
    different seed without touching the job's own resolved_seed - every
    OTHER segment (and a plain re-regenerate with no override) keeps using
    the same base_seed+i formula it always has."""
    seg = job["segments"][i]
    seg["status"] = "running"
    seg["output_file"] = None
    seg_start = time.time()
    seg["started_at"] = seg_start
    _save_job_state(job_id)

    actual_seed = seed_override if seed_override is not None else base_seed + i
    seg["seed"] = actual_seed
    segment_length = params["segment_length_seconds"]
    segment_prompts = params.get("segment_prompts") or None
    ref_video = params.get("ref_video_filename")
    use_ref_video_direct = params.get("use_ref_video_direct", False)
    prefix = f"video/h3studio_{job_id}_seg{i:03d}_{uuid.uuid4().hex[:6]}"
    seg_prompt = segment_prompts[i] if segment_prompts else params["prompt"]
    if continuation_picture_n is not None:
        seg_prompt = _inject_continuation_anchor(seg_prompt, continuation_picture_n)
    wf = build_workflow(
        prompt=seg_prompt,
        ref_image_filenames=ref_images[:3],
        ref_audio_filename=params.get("ref_audio_filename"),
        bg_music_filename=bg_music_trimmed,
        ref_video_filename=ref_video if (i == 0 and use_ref_video_direct) else None,
        use_ref_video_direct=use_ref_video_direct and i == 0,
        width=params.get("width", 864),
        height=params.get("height", 480),
        duration_seconds=segment_length,
        seed=actual_seed,
        ref_image_size=params.get("ref_image_size", "max"),
        filename_prefix=prefix,
        steps=params.get("steps", 8),
    )
    ensure_comfyui_memory_healthy()
    prompt_id = queue_via_prompt_api(wf, client_id=f"h3studio-{job_id}")
    seg["prompt_id"] = prompt_id
    _save_job_state(job_id)

    rec = _wait_for_prompt(prompt_id, job, on_tick=lambda: _save_job_state(job_id))
    if rec is None:
        # Cancelled via /api/jobs/{id}/stop - the GPU-side abort already
        # happened (interrupt_current(), called from the stop endpoint
        # before this loop even notices).
        seg["status"] = "cancelled"
        _save_job_state(job_id)
        return False
    seg["elapsed"] = round(time.time() - seg_start, 1)

    status = rec.get("status", {})
    if status.get("status_str") != "success":
        seg["status"] = "error"
        _save_job_state(job_id)
        return False

    out_file = None
    for node_out in rec.get("outputs", {}).values():
        for img in node_out.get("images", []):
            out_file = COMFYUI_OUTPUT_DIR / img["subfolder"] / img["filename"]
    if out_file is None or not out_file.exists():
        seg["status"] = "error"
        _save_job_state(job_id)
        return False

    seg["output_file"] = str(out_file)
    seg["status"] = "done"
    _save_job_state(job_id)
    return True


def _run_job(job_id: str, params: dict, start_index: int = 0, end_index: int | None = None):
    """Generates segments [start_index, end_index) (end_index defaults to
    all of them) and stops there - it does NOT concatenate a final video
    (see /api/jobs/{id}/concatenate, a separate manual step per 2026-08-25
    request: letting a bad segment get caught and regenerated before
    committing to the combined video, instead of finding out only after
    ~10min/segment of GPU time already went into concatenating it).
    start_index > 0 is a resume (job["segments"][:start_index] are assumed
    already "done" - the caller, /api/jobs/{id}/resume, is responsible for
    verifying that) or a partial run continuing past an earlier checkpoint."""
    job = JOBS[job_id]
    try:
        n_segments = _job_n_segments(params)
        end_index = n_segments if end_index is None else min(end_index, n_segments)
        if start_index == 0:
            job["segments"] = [
                {"index": i, "status": "pending", "prompt_id": None, "output_file": None, "elapsed": None, "started_at": None}
                for i in range(n_segments)
            ]
        job["status"] = "running"
        job["cancel_requested"] = False
        _save_job_state(job_id)

        # Scratch files generated for this run (extracted frames, trimmed
        # audio) - not real output, so a stop request deletes them; already
        # -completed segment videos are real output and are left alone.
        temp_files = []

        bg_music = params.get("bg_music_filename")
        bg_music_trimmed = None
        if bg_music:
            # Trim the uploaded background-music reference (head) to the
            # per-segment duration, once, and reuse the same trimmed clip
            # for every segment - the model only sees duration_seconds of
            # context per call anyway.
            trimmed_name = f"{uuid.uuid4().hex}_music_trimmed{Path(bg_music).suffix}"
            _trim_audio(COMFYUI_INPUT_DIR / bg_music, COMFYUI_INPUT_DIR / trimmed_name, params["segment_length_seconds"])
            bg_music_trimmed = trimmed_name
            temp_files.append(COMFYUI_INPUT_DIR / trimmed_name)

        base_seed = job.get("resolved_seed") or params.get("seed") or int(time.time())
        job["resolved_seed"] = base_seed
        _save_job_state(job_id)

        for i in range(start_index, end_index):
            # Recomputed every iteration (2026-08-26) - each segment i > 0
            # chains from segment i-1's own just-finished last frame, not a
            # single reference resolved once before the loop. See
            # _continuation_ref_images().
            ref_images, continuation_picture_n = _continuation_ref_images(job, params, i, temp_files)
            ok = _generate_segment(job_id, job, params, i, ref_images, bg_music_trimmed, base_seed, continuation_picture_n=continuation_picture_n)
            if not ok:
                seg_status = job["segments"][i]["status"]
                job["status"] = seg_status  # "error" or "cancelled"
                if seg_status == "error":
                    job["error"] = f"segment {i} failed"
                if seg_status == "cancelled":
                    for f in temp_files:
                        try:
                            f.unlink(missing_ok=True)
                        except OSError:
                            pass
                _save_job_state(job_id)
                return

        job["status"] = "segments_ready"
        _save_job_state(job_id)
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)
        _save_job_state(job_id)


def _run_concatenate(job_id: str, params: dict):
    job = JOBS[job_id]
    try:
        segments = job["segments"]
        missing = [s["index"] for s in segments if s["status"] != "done" or not s.get("output_file")]
        if missing:
            job["status"] = "segments_ready"
            job["error"] = f"以下段落尚未完成，無法合成：{', '.join(str(i + 1) for i in missing)}"
            _save_job_state(job_id)
            return

        job["status"] = "concatenating"
        job["error"] = None
        _save_job_state(job_id)

        final_raw = JOBS_DIR / job_id / "final_raw.mp4"
        final_raw.parent.mkdir(exist_ok=True, parents=True)

        all_paths = [Path(s["output_file"]) for s in sorted(segments, key=lambda s: s["index"])]
        ref_video = params.get("ref_video_filename")
        if ref_video and not params.get("use_ref_video_direct", False):
            all_paths = [COMFYUI_INPUT_DIR / ref_video] + all_paths

        _concat_videos(all_paths, final_raw)

        out_width = params.get("output_width", params.get("width", 864))
        out_height = params.get("output_height", params.get("height", 480))
        out_fps = params.get("output_fps", 24)
        final_path = JOBS_DIR / job_id / "final.mp4"
        if (out_width, out_height, out_fps) != (params.get("width", 864), params.get("height", 480), 24):
            _postprocess_format(final_raw, final_path, out_width, out_height, out_fps)
        else:
            shutil.copy(final_raw, final_path)

        job["final_video"] = str(final_path)
        job["status"] = "done"
        _save_job_state(job_id)
    except Exception as e:
        job["status"] = "segments_ready"
        job["error"] = f"合成失敗：{e}"
        _save_job_state(job_id)


@app.get("/api/health")
async def health():
    alive, detail = check_comfyui_alive()
    return {"comfyui_alive": alive, "detail": detail}


@app.get("/api/model-info")
async def model_info():
    info = get_model_info()
    info["character_photo_ckpt_name"] = CHARACTER_PHOTO_CHECKPOINT_NAME
    return info


@app.get("/api/settings")
async def get_settings(user: dict = Depends(auth.get_admin_user)):
    return {"sage_attention_enabled": SAGE_ATTENTION_FLAG_PATH.exists()}


@app.post("/api/settings")
async def update_settings(sage_attention_enabled: bool = Form(...), user: dict = Depends(auth.get_admin_user)):
    if sage_attention_enabled:
        SAGE_ATTENTION_FLAG_PATH.touch()
    else:
        SAGE_ATTENTION_FLAG_PATH.unlink(missing_ok=True)
    return {"sage_attention_enabled": SAGE_ATTENTION_FLAG_PATH.exists()}


def _gpu_stats() -> dict | None:
    try:
        out = subprocess.check_output([
            "nvidia-smi",
            "--query-gpu=utilization.gpu,memory.used,memory.total,temperature.gpu",
            "--format=csv,noheader,nounits",
        ], timeout=3).decode().strip()
        util, mem_used, mem_total, temp = (float(x) for x in out.split(","))
        return {
            "util_percent": util,
            "mem_used_mb": mem_used,
            "mem_total_mb": mem_total,
            "mem_percent": round(mem_used / mem_total * 100, 1),
            "temp_c": temp,
        }
    except Exception:
        return None


@app.get("/api/system-stats")
async def system_stats(user: dict = Depends(auth.get_admin_user)):
    vm = psutil.virtual_memory()
    return {
        "gpu": _gpu_stats(),
        "ram": {
            "used_gb": round(vm.used / 1e9, 1),
            "total_gb": round(vm.total / 1e9, 1),
            "percent": vm.percent,
        },
        "cpu_percent": psutil.cpu_percent(interval=0.1),
    }


@app.post("/api/prompt/generate")
async def generate_prompt_draft(
    idea: str = Form(...),
    duration_seconds: float = Form(15),
    num_subjects: int = Form(0),
    has_audio: bool = Form(False),
    num_segments: int = Form(1),
    user: dict = Depends(auth.get_admin_user),
):
    """'AI 產生 Prompt' button - expands a one-line idea into a Chinese
    shot-by-shot draft, one segment (num_segments=1, the active card) or
    the whole multi-segment story at once (num_segments=N). See
    prompt_rewriter.generate_shot_draft for why this one call is allowed to
    be creative, unlike the rest of the module. Returns drafts for the
    Shot/soundscape fields - not submitted anywhere, the user reviews and
    can hand-edit before this goes near /api/generate."""
    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(
            None, prompt_rewriter.generate_shot_draft, idea, duration_seconds, num_subjects, has_audio, num_segments
        )
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"error": f"AI 產生失敗（Haiku API）：{e}"}, status_code=503)
    return result


@app.post("/api/prompt/retention-preview")
async def retention_preview(
    ref_image_roles: str = Form("[]"),
    retention_level: str = Form("fully_preserved"),
    has_ref_audio: bool = Form(False),
    user: dict = Depends(auth.get_admin_user),
):
    """Read-only preview of retention_analysis for the Prompt-input tab's
    live display (2026-08-26) - pure string building, no Haiku call, so it's
    cheap enough to re-call on every ref-image/audio/retention-level change.
    Reuses prompt_rewriter's exact section-building logic so this preview
    never drifts from what actually gets sent at submit time."""
    parsed_roles = json.loads(ref_image_roles)
    if not parsed_roles and not has_ref_audio:
        return {"retention_analysis": ""}
    text = prompt_rewriter._build_retention_analysis(parsed_roles, "", retention_level, has_ref_audio)
    return {"retention_analysis": text}


@app.post("/api/prompt/workflow-preview")
async def workflow_preview(
    segment_prompts: str = Form("[]"),
    duration_minutes: float = Form(1.0),
    segment_length_seconds: int = Form(15),
    ref_image_filenames: str = Form("[]"),
    ref_audio_filename: str = Form(""),
    bg_music_filename: str = Form(""),
    ref_video_filename: str = Form(""),
    use_ref_video_direct: bool = Form(False),
    width: int = Form(864),
    height: int = Form(480),
    seed: int | None = Form(None),
    ref_image_size: str = Form("max"),
    steps: int = Form(8),
    job_id: str = Form(""),
    user: dict = Depends(auth.get_admin_user),
):
    """Returns the raw ComfyUI API-format workflow JSON for EVERY segment of
    the CURRENT on-screen settings, unqueued - lets the user actually see
    the wiring for their specific setup (2+ ref images, voice, music, per-
    segment continuity, etc.) instead of the one static example saved in
    ComfyUI ("h3-studio-current-config" isn't kept in sync automatically -
    see 2026-09-02 discussion). Doesn't run the Haiku rewrite (the prompt
    TEXT doesn't affect graph shape, only which reference files are wired in
    does), so this is instant and free. Reuses the exact same per-segment
    reference-image logic (_continuation_ref_images) the real job and the
    preflight validator use, so segment i>0's continuity slot is included
    whenever it's actually resolvable.
    job_id (2026-09-03): if this project has already actually been
    generated, pass its job_id so the continuity lookup uses the REAL
    segments (with real output_file paths) instead of a synthetic "nothing
    has run yet" stub - otherwise segment i>0's continuation slot always
    came back empty even for an already-completed job, which is wrong and
    was reported as a bug. Falls back to the synthetic stub if job_id is
    missing/unknown (a project that's never been generated has no real
    segments to look up anyway)."""
    parsed_segment_prompts = json.loads(segment_prompts)
    params = {
        "prompt": parsed_segment_prompts[0] if parsed_segment_prompts else "",
        "segment_prompts": parsed_segment_prompts or None,
        "ref_image_filenames": json.loads(ref_image_filenames),
        "ref_audio_filename": ref_audio_filename or None,
        "bg_music_filename": bg_music_filename or None,
        "ref_video_filename": ref_video_filename or None,
        "use_ref_video_direct": use_ref_video_direct,
        "duration_minutes": duration_minutes,
        "segment_length_seconds": segment_length_seconds,
        "width": width,
        "height": height,
        "seed": seed,
        "ref_image_size": ref_image_size,
    }
    n_segments = _job_n_segments(params)
    real_job = JOBS.get(job_id) if job_id else None
    if real_job and len(real_job.get("segments") or []) >= n_segments:
        fake_job = real_job
    else:
        fake_job = {"segments": [{"output_file": None} for _ in range(n_segments)]}
    # Deliberately NOT cleaned up afterward (unlike _preflight_validate_
    # segments, which only validates and produces nothing the user keeps) -
    # this endpoint's whole point is that the returned JSON is meant to be
    # downloaded and actually dragged into ComfyUI afterward, so any
    # continuation-frame image it extracts and references by filename has
    # to still exist on disk when the user gets there. Deleting it
    # immediately (2026-09-03 bug, caught by the user) left the JSON
    # pointing at an already-deleted file.
    temp_files = []
    base_seed = params.get("seed") or int(time.time())
    results = []
    for i in range(n_segments):
        ref_images, _picture_n = _continuation_ref_images(fake_job, params, i, temp_files)
        seg_prompt = (params.get("segment_prompts") or [None] * n_segments)[i] or "(preview - actual prompt text doesn't affect graph shape)"
        use_ref_video_direct_i = params.get("use_ref_video_direct", False) and i == 0
        wf = build_workflow(
            prompt=seg_prompt,
            ref_image_filenames=ref_images[:3],
            ref_audio_filename=params.get("ref_audio_filename"),
            bg_music_filename=params.get("bg_music_filename"),
            ref_video_filename=params.get("ref_video_filename") if use_ref_video_direct_i else None,
            use_ref_video_direct=use_ref_video_direct_i,
            width=params.get("width", 864),
            height=params.get("height", 480),
            duration_seconds=params["segment_length_seconds"],
            seed=base_seed + i,
            ref_image_size=params.get("ref_image_size", "max"),
            filename_prefix=f"video/h3studio_preview_seg{i:03d}",
            steps=steps,
        )
        results.append({"index": i, "workflow": wf})
    return {"segments": results}


@app.post("/api/prompt/preview")
async def preview_prompt(
    segment_prompts: str = Form("[]"),
    ref_image_filenames: str = Form("[]"),
    ref_image_roles: str = Form("[]"),
    ref_audio_filename: str = Form(""),
    segment_length_seconds: int = Form(15),
    retention_level: str = Form("fully_preserved"),
    user: dict = Depends(auth.get_admin_user),
):
    """Runs the same draft -> official-format rewrite /api/generate does,
    without creating a job or touching ComfyUI - lets the user review the
    exact English text that would be sent before committing GPU time to it."""
    parsed_segment_prompts = json.loads(segment_prompts)
    parsed_ref_images = json.loads(ref_image_filenames)
    parsed_ref_image_roles = json.loads(ref_image_roles)
    while len(parsed_ref_image_roles) < len(parsed_ref_images):
        parsed_ref_image_roles.append({"role": "subject"})
    loop = asyncio.get_event_loop()
    try:
        rewritten = [
            await loop.run_in_executor(
                None, prompt_rewriter.rewrite_prompt, p, parsed_ref_image_roles,
                segment_length_seconds, retention_level, bool(ref_audio_filename),
            )
            for p in parsed_segment_prompts
        ]
    except Exception as e:
        return JSONResponse({"error": f"Prompt 格式轉換失敗（Haiku API）：{e}"}, status_code=503)
    return {"rewritten": rewritten}


@app.post("/api/generate")
async def generate(
    prompt: str = Form(...),
    segment_prompts: str = Form("[]"),
    prompt_overrides: str = Form("[]"),
    ref_image_filenames: str = Form("[]"),
    ref_audio_filename: str = Form(""),
    bg_music_filename: str = Form(""),
    ref_video_filename: str = Form(""),
    use_ref_video_direct: bool = Form(False),
    duration_minutes: float = Form(1.0),
    segment_length_seconds: int = Form(15),
    width: int = Form(864),
    height: int = Form(480),
    output_width: int = Form(864),
    output_height: int = Form(480),
    output_fps: int = Form(24),
    seed: int | None = Form(None),
    ref_image_size: str = Form("max"),
    retention_level: str = Form("fully_preserved"),
    ref_image_roles: str = Form("[]"),
    run_up_to_segment: int | None = Form(None),
    steps: int = Form(8),
    user: dict = Depends(auth.get_admin_user),
):
    # Fail fast rather than accepting the job and letting the user wait
    # through an edit-and-submit cycle before discovering ComfyUI is down.
    alive, detail = check_comfyui_alive()
    if not alive:
        return JSONResponse(
            {"error": f"ComfyUI 目前無法連線（{detail}），請確認 ComfyUI 是否正在執行後再試一次。"},
            status_code=503,
        )
    try:
        ensure_comfyui_memory_healthy()
    except RuntimeError as e:
        return JSONResponse({"error": str(e)}, status_code=503)

    job_id = uuid.uuid4().hex[:12]
    parsed_segment_prompts = json.loads(segment_prompts)
    parsed_ref_images = json.loads(ref_image_filenames)
    parsed_overrides = json.loads(prompt_overrides)
    parsed_ref_image_roles = json.loads(ref_image_roles)
    while len(parsed_ref_image_roles) < len(parsed_ref_images):
        parsed_ref_image_roles.append({"role": "subject"})

    # Rewrite every segment's draft into MiniMax H3's official ref2va
    # format (6 sections, English, correct shot-timestamp syntax) before
    # it ever reaches ComfyUI - see prompt_rewriter.py for why this can't
    # just be Python string templating. A segment whose index has a
    # non-null entry in prompt_overrides was already reviewed (and
    # possibly hand-edited) by the user via /api/prompt/preview - use that
    # exact text verbatim instead of re-rewriting it, so what gets
    # generated matches what was reviewed.
    loop = asyncio.get_event_loop()
    try:
        if parsed_segment_prompts:
            # anthropic's SDK call is blocking - run each off the event
            # loop rather than stalling every other request while it waits.
            parsed_segment_prompts = [
                override if override else
                await loop.run_in_executor(
                    None, prompt_rewriter.rewrite_prompt, p, parsed_ref_image_roles,
                    segment_length_seconds, retention_level, bool(ref_audio_filename),
                )
                for p, override in zip(parsed_segment_prompts, parsed_overrides + [None] * len(parsed_segment_prompts))
            ]
            prompt = parsed_segment_prompts[0]
        else:
            prompt = await loop.run_in_executor(
                None, prompt_rewriter.rewrite_prompt, prompt, parsed_ref_image_roles,
                segment_length_seconds, retention_level, bool(ref_audio_filename),
            )
    except Exception as e:
        return JSONResponse({"error": f"Prompt 格式轉換失敗（Haiku API）：{e}"}, status_code=503)

    params = {
        "prompt": prompt,
        "segment_prompts": parsed_segment_prompts or None,
        "ref_image_filenames": parsed_ref_images,
        "ref_audio_filename": ref_audio_filename or None,
        "bg_music_filename": bg_music_filename or None,
        "ref_video_filename": ref_video_filename or None,
        "use_ref_video_direct": use_ref_video_direct,
        "duration_minutes": duration_minutes,
        "segment_length_seconds": segment_length_seconds,
        "width": width,
        "height": height,
        "output_width": output_width,
        "output_height": output_height,
        "output_fps": output_fps,
        "seed": seed,
        "ref_image_size": ref_image_size,
        "steps": steps,
    }

    preflight_error = await loop.run_in_executor(None, _preflight_validate_segments, job_id, params, run_up_to_segment)
    if preflight_error:
        return JSONResponse({"error": f"生成前驗證失敗，尚未送出：{preflight_error}"}, status_code=400)

    JOBS[job_id] = {"status": "queued", "segments": [], "params": params, "created_at": time.time()}
    _save_job_state(job_id)
    asyncio.get_event_loop().run_in_executor(None, _run_job_and_drain, job_id, params, 0, run_up_to_segment)
    return {"job_id": job_id}


def _apply_regenerate_overrides(job_id: str, job: dict, index: int, prompt: str | None, ref_image_filenames: str | None):
    """Shared by the immediate-run path and the queued-drain path
    (_process_pending_regenerates) so both apply overrides identically.
    ref_image_filenames (2026-08-26): lets the frontend wire the project's
    currently-selected reference images into a job that was originally
    submitted without any - e.g. a character reference photo added to the
    library after the job's first run (see _continuation_ref_images()).
    prompt (2026-08-26): the frontend re-runs the same Haiku rewrite the
    initial submit used, on just this segment's current draft text -
    replaces this segment's frozen prompt permanently, so a later
    regenerate-without-edits or the eventual concatenate step both see the
    updated content too, not just this one regenerate call."""
    changed = False
    if ref_image_filenames is not None:
        job["params"]["ref_image_filenames"] = json.loads(ref_image_filenames)
        changed = True
    if prompt:
        segment_prompts = job["params"].get("segment_prompts")
        if segment_prompts:
            segment_prompts[index] = prompt
        else:
            job["params"]["prompt"] = prompt
        changed = True
    if changed:
        _save_job_state(job_id)


def _process_pending_regenerates(job_id: str):
    """Drains job["pending_regenerates"] one at a time, in submission
    order - called after anything that can leave a job idle (a single
    regenerate finishing, or a full _run_job finishing) so a regenerate
    queued while the job was busy actually gets its turn instead of
    silently sitting there forever. Loops rather than recursing so a long
    queue doesn't grow the call stack."""
    job = JOBS.get(job_id)
    if not job:
        return
    while True:
        pending = job.get("pending_regenerates") or []
        if not pending or job["status"] in ("queued", "running", "concatenating"):
            return
        item = pending.pop(0)
        index = item["index"]
        segments = job.get("segments") or []
        if index < 0 or index >= len(segments):
            continue  # stale entry (segment list changed under it) - drop and keep draining

        # No hard block on the previous segment being done (2026-08-26):
        # _continuation_ref_images() already degrades gracefully (just
        # skips the continuation anchor) if segment index-1 isn't real
        # output yet - it's an enhancement on top of the subject reference,
        # not the only identity-lock mechanism the way the old own-
        # continuity fallback was.

        alive, _detail = check_comfyui_alive()
        if not alive:
            segments[index]["status"] = "error"
            job["error"] = f"segment {index}：ComfyUI 目前無法連線"
            _save_job_state(job_id)
            continue
        try:
            ensure_comfyui_memory_healthy()
        except RuntimeError as e:
            segments[index]["status"] = "error"
            job["error"] = str(e)
            _save_job_state(job_id)
            continue

        _apply_regenerate_overrides(job_id, job, index, item.get("prompt"), item.get("ref_image_filenames"))
        _run_regenerate_segment(job_id, job["params"], index, item.get("seed"))
        # loop back around: _run_regenerate_segment left job idle again (see
        # its own status handling), so the next pending item (if any) is
        # picked up on this same pass rather than needing a fresh trigger.


def _run_job_and_drain(job_id: str, params: dict, start_index: int = 0, end_index: int | None = None):
    _run_job(job_id, params, start_index, end_index)
    _process_pending_regenerates(job_id)


def _run_regenerate_segment_and_drain(job_id: str, params: dict, index: int, seed_override: int | None = None):
    _run_regenerate_segment(job_id, params, index, seed_override)
    _process_pending_regenerates(job_id)


def _run_regenerate_segment(job_id: str, params: dict, index: int, seed_override: int | None = None):
    """Regenerates exactly one already-existing segment slot, leaving every
    other segment untouched. Reuses the same continuity-reference
    resolution _run_job uses for a resume, since a mid-story index needs
    the same "derive from segment 0's own last frame" handling."""
    job = JOBS[job_id]
    # Snapshot what to fall back to if this attempt errors/cancels - a
    # single-segment regenerate is scoped to one slot, it shouldn't be able
    # to drag the whole job's status down to "error"/"cancelled" (which
    # hides the entire generate tab - see applyProjectData()'s
    # segments_ready check) nor destroy a previously-good segment result
    # just because the retry didn't pan out (2026-08-26 incident: cancelling
    # a regenerate wiped that segment's already-completed output_file with
    # no way back except manually editing job state on disk).
    prev_job_status = job["status"]
    prev_segment_snapshot = dict(job["segments"][index])
    try:
        job["status"] = "running"
        job["cancel_requested"] = False
        job["error"] = None
        _save_job_state(job_id)

        temp_files = []
        bg_music = params.get("bg_music_filename")
        bg_music_trimmed = None
        if bg_music:
            trimmed_name = f"{uuid.uuid4().hex}_music_trimmed{Path(bg_music).suffix}"
            _trim_audio(COMFYUI_INPUT_DIR / bg_music, COMFYUI_INPUT_DIR / trimmed_name, params["segment_length_seconds"])
            bg_music_trimmed = trimmed_name
            temp_files.append(COMFYUI_INPUT_DIR / trimmed_name)

        base_seed = job.get("resolved_seed") or params.get("seed") or int(time.time())
        job["resolved_seed"] = base_seed

        ref_images, continuation_picture_n = _continuation_ref_images(job, params, index, temp_files)

        ok = _generate_segment(
            job_id, job, params, index, ref_images, bg_music_trimmed, base_seed, seed_override, continuation_picture_n
        )
        if not ok:
            seg_status = job["segments"][index]["status"]  # "error" or "cancelled"
            if prev_segment_snapshot.get("status") == "done":
                job["segments"][index] = prev_segment_snapshot
            job["status"] = prev_job_status
            if seg_status == "error":
                job["error"] = f"segment {index} failed"
            if seg_status == "cancelled":
                for f in temp_files:
                    try:
                        f.unlink(missing_ok=True)
                    except OSError:
                        pass
            _save_job_state(job_id)
            return

        job["status"] = "segments_ready"
        _save_job_state(job_id)
    except Exception as e:
        if prev_segment_snapshot.get("status") == "done":
            job["segments"][index] = prev_segment_snapshot
        job["status"] = prev_job_status
        job["error"] = str(e)
        _save_job_state(job_id)


@app.post("/api/jobs/{job_id}/resume")
async def resume_job(job_id: str, run_up_to_segment: int | None = Form(None), user: dict = Depends(auth.get_admin_user)):
    """Continues a job that stopped short of all segments being done -
    whether it errored, was cancelled, or was deliberately submitted with
    run_up_to_segment as a checkpoint. Picks up from the first segment
    that isn't a real "done" (status != done, or its output file was lost)
    rather than requiring the caller to know the index."""
    job = JOBS.get(job_id)
    if not job:
        return JSONResponse({"error": "not found"}, status_code=404)
    if job["status"] in ("queued", "running", "concatenating"):
        return JSONResponse({"error": f"job is currently {job['status']}, nothing to resume"}, status_code=400)
    segments = job.get("segments") or []
    start_index = next(
        (s["index"] for s in segments if s["status"] != "done" or not s.get("output_file") or not Path(s["output_file"]).exists()),
        None,
    )
    if start_index is None:
        return JSONResponse({"error": "所有段落都已完成，沒有可續跑的部分（請用合成）"}, status_code=400)

    alive, detail = check_comfyui_alive()
    if not alive:
        return JSONResponse(
            {"error": f"ComfyUI 目前無法連線（{detail}），請確認 ComfyUI 是否正在執行後再試一次。"},
            status_code=503,
        )
    try:
        ensure_comfyui_memory_healthy()
    except RuntimeError as e:
        return JSONResponse({"error": str(e)}, status_code=503)

    params = job["params"]
    asyncio.get_event_loop().run_in_executor(None, _run_job_and_drain, job_id, params, start_index, run_up_to_segment)
    return {"job_id": job_id, "resumed_from_segment": start_index}


@app.post("/api/jobs/{job_id}/segments/{index}/regenerate")
async def regenerate_segment(
    job_id: str,
    index: int,
    prompt: str | None = Form(None),
    ref_image_filenames: str | None = Form(None),
    seed: int | None = Form(None),
    user: dict = Depends(auth.get_admin_user),
):
    job = JOBS.get(job_id)
    if not job:
        return JSONResponse({"error": "not found"}, status_code=404)
    segments = job.get("segments") or []
    if index < 0 or index >= len(segments):
        return JSONResponse({"error": f"segment index {index} out of range"}, status_code=400)

    # 2026-08-26: the job used to just reject a regenerate while it was
    # busy ("job is currently running") - the user has to notice and retry
    # by hand. Queue it instead: it's remembered here and drained (in
    # submission order) by _process_pending_regenerates(), called from
    # whatever's currently running once IT finishes. Overrides are stored
    # with the queued entry rather than applied to job["params"] right
    # away, since the currently-running generation must keep using
    # whatever params it already started with.
    if job["status"] in ("queued", "running", "concatenating"):
        job.setdefault("pending_regenerates", [])
        job["pending_regenerates"] = [p for p in job["pending_regenerates"] if p["index"] != index]
        job["pending_regenerates"].append(
            {"index": index, "prompt": prompt, "ref_image_filenames": ref_image_filenames, "seed": seed}
        )
        segments[index]["status"] = "queued"
        _save_job_state(job_id)
        return {"job_id": job_id, "queued_segment": index}

    alive, detail = check_comfyui_alive()
    if not alive:
        return JSONResponse(
            {"error": f"ComfyUI 目前無法連線（{detail}），請確認 ComfyUI 是否正在執行後再試一次。"},
            status_code=503,
        )
    try:
        ensure_comfyui_memory_healthy()
    except RuntimeError as e:
        return JSONResponse({"error": str(e)}, status_code=503)

    _apply_regenerate_overrides(job_id, job, index, prompt, ref_image_filenames)
    asyncio.get_event_loop().run_in_executor(
        None, _run_regenerate_segment_and_drain, job_id, job["params"], index, seed
    )
    return {"job_id": job_id, "regenerating_segment": index}


@app.post("/api/jobs/{job_id}/segments/{index}/unqueue")
async def unqueue_segment(job_id: str, index: int, user: dict = Depends(auth.get_admin_user)):
    """Cancels a queued-but-not-yet-started regenerate (see the queueing
    branch above) - the segment just goes back to whatever it was before
    being queued, no ComfyUI/GPU involvement since it never started."""
    job = JOBS.get(job_id)
    if not job:
        return JSONResponse({"error": "not found"}, status_code=404)
    pending = job.get("pending_regenerates") or []
    remaining = [p for p in pending if p["index"] != index]
    if len(remaining) == len(pending):
        return JSONResponse({"error": "this segment isn't queued"}, status_code=400)
    job["pending_regenerates"] = remaining
    segments = job.get("segments") or []
    if 0 <= index < len(segments) and segments[index]["status"] == "queued":
        segments[index]["status"] = "pending" if not segments[index].get("output_file") else "done"
    _save_job_state(job_id)
    return {"ok": True}


@app.post("/api/jobs/{job_id}/concatenate")
async def concatenate_job(job_id: str, user: dict = Depends(auth.get_admin_user)):
    """Manual final-video step (2026-08-25) - deliberately NOT automatic
    after the last segment finishes, so a bad segment can be caught and
    regenerated (see /segments/{index}/regenerate) before committing to
    building the combined video around it."""
    job = JOBS.get(job_id)
    if not job:
        return JSONResponse({"error": "not found"}, status_code=404)
    if job["status"] in ("queued", "running", "concatenating"):
        return JSONResponse({"error": f"job is currently {job['status']}, cannot concatenate right now"}, status_code=400)
    segments = job.get("segments") or []
    missing = [s["index"] for s in segments if s["status"] != "done" or not s.get("output_file")]
    if missing:
        return JSONResponse(
            {"error": f"以下段落尚未完成，無法合成：{', '.join(str(i + 1) for i in missing)}"},
            status_code=400,
        )
    asyncio.get_event_loop().run_in_executor(None, _run_concatenate, job_id, job["params"])
    return {"job_id": job_id}


@app.get("/api/jobs")
async def list_jobs(user: dict = Depends(auth.get_admin_user)):
    """Generation history - lets the frontend link back to previously
    produced videos, newest first."""
    candidates = [jid for jid in JOBS if _job_state_path(jid).exists()]
    candidates.sort(key=lambda jid: _job_state_path(jid).stat().st_mtime, reverse=True)
    out = []
    for jid in candidates[:50]:
        job = JOBS[jid]
        params = job.get("params", {})
        prompt = params.get("prompt") or ""
        summary_match = None
        for line in prompt.splitlines():
            if line.strip().lower().startswith("summary:"):
                summary_match = line.split(":", 1)[1].strip()
                break
        out.append({
            "job_id": jid,
            "status": job["status"],
            "has_final_video": "final_video" in job,
            "summary": summary_match or prompt[:60],
        })
    return {"jobs": out}


@app.get("/api/jobs/latest")
async def latest_job(user: dict = Depends(auth.get_admin_user)):
    """Lets the frontend recover a job it lost track of (e.g. the browser
    was closed before job_id got saved client-side, or localStorage was
    cleared) - falls back to each job folder's mtime since older jobs
    loaded from disk on startup may not carry an in-memory created_at."""
    candidates = [jid for jid in JOBS if _job_state_path(jid).exists()]
    if not candidates:
        return JSONResponse({"error": "no jobs"}, status_code=404)
    job_id = max(candidates, key=lambda jid: _job_state_path(jid).stat().st_mtime)
    return {"job_id": job_id, "status": JOBS[job_id]["status"]}


@app.get("/api/jobs/{job_id}")
async def job_status(job_id: str, user: dict = Depends(auth.get_admin_user)):
    job = JOBS.get(job_id)
    if not job:
        return JSONResponse({"error": "not found"}, status_code=404)
    params = job.get("params", {})
    return {
        "status": job["status"],
        "segments": job["segments"],
        "error": job.get("error"),
        "has_final_video": "final_video" in job,
        "prompt": params.get("prompt"),
        "segment_prompts": params.get("segment_prompts"),
        "ref_image_filenames": params.get("ref_image_filenames") or [],
        "resolved_seed": job.get("resolved_seed"),
        "created_at": job.get("created_at"),
    }


@app.post("/api/jobs/{job_id}/stop")
async def stop_job(job_id: str, user: dict = Depends(auth.get_admin_user)):
    job = JOBS.get(job_id)
    if not job:
        return JSONResponse({"error": "not found"}, status_code=404)
    if job["status"] not in ("queued", "running"):
        return JSONResponse({"error": f"job is already {job['status']}, nothing to stop"}, status_code=400)
    job["cancel_requested"] = True
    try:
        interrupt_current()
    except Exception:
        pass  # ComfyUI unreachable - _run_job's own poll loop will still pick up cancel_requested and stop cleanly
    return {"ok": True}


@app.get("/api/jobs/{job_id}/segments/{index}/video")
async def segment_video(job_id: str, index: int, user: dict = Depends(auth.get_admin_user_flexible)):
    job = JOBS.get(job_id)
    if not job:
        return JSONResponse({"error": "not found"}, status_code=404)
    segments = job.get("segments", [])
    if index < 0 or index >= len(segments) or not segments[index].get("output_file"):
        return JSONResponse({"error": "not ready"}, status_code=404)
    path = Path(segments[index]["output_file"])
    if not path.exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(path, media_type="video/mp4")


@app.get("/api/uploads/image/{filename}")
async def get_uploaded_image(filename: str, user: dict = Depends(auth.get_admin_user_flexible)):
    # Reference images live in ComfyUI's own input/ dir (that's what the
    # workflow reads them from) - this just lets the frontend show a
    # thumbnail of what was actually submitted, e.g. when recovering a
    # job's progress after a reload. Names are our own uuid-based
    # filenames (see _save_upload), never resolve outside that directory.
    path = COMFYUI_INPUT_DIR / Path(filename).name
    if not path.exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(path)


@app.get("/api/jobs/{job_id}/video")
async def job_video(job_id: str, user: dict = Depends(auth.get_admin_user_flexible)):
    job = JOBS.get(job_id)
    if not job or "final_video" not in job:
        return JSONResponse({"error": "not ready"}, status_code=404)
    return FileResponse(job["final_video"], media_type="video/mp4")


@app.post("/api/postprod/sessions/{sid}/track1/from-job")
async def postprod_track1_from_job(sid: str, job_id: str = Form(...), user: dict = Depends(auth.get_admin_user)):
    # Lives in main.py (not postprod.py) because it's the one place
    # post-production needs to read a generation job's final video path -
    # keeps the dependency one-way (main -> postprod, never the reverse).
    job = JOBS.get(job_id)
    if not job or "final_video" not in job:
        return JSONResponse({"error": "該生成任務還沒有完成影片"}, status_code=400)
    try:
        return postprod.set_track1_from_file(sid, Path(job["final_video"]))
    except ValueError:
        return JSONResponse({"error": "postprod session not found"}, status_code=404)


def _project_path(project_id: str) -> Path:
    return PROJECTS_DIR / f"{project_id}.json"


@app.get("/api/projects")
async def list_projects(user: dict = Depends(auth.get_admin_user)):
    out = []
    for path in PROJECTS_DIR.glob("*.json"):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            out.append({"id": path.stem, "name": data.get("name", path.stem), "updated_at": data.get("updated_at", 0)})
        except Exception:
            continue
    out.sort(key=lambda p: p["updated_at"], reverse=True)
    return {"projects": out}


@app.get("/api/projects/{project_id}")
async def get_project(project_id: str, user: dict = Depends(auth.get_admin_user)):
    path = _project_path(project_id)
    if not path.exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


@app.post("/api/projects")
async def save_project(request: Request, user: dict = Depends(auth.get_admin_user)):
    body = await request.json()
    project_id = body.get("id") or uuid.uuid4().hex[:12]
    body["id"] = project_id
    body["updated_at"] = time.time()
    with open(_project_path(project_id), "w", encoding="utf-8") as f:
        json.dump(body, f, ensure_ascii=False)
    return body


@app.post("/api/projects/{project_id}/clone")
async def clone_project(project_id: str, user: dict = Depends(auth.get_admin_user)):
    src = _project_path(project_id)
    if not src.exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    with open(src, "r", encoding="utf-8") as f:
        data = json.load(f)
    new_id = uuid.uuid4().hex[:12]
    data["id"] = new_id
    data["name"] = f"{data.get('name', project_id)} (複製)"
    data["updated_at"] = time.time()
    with open(_project_path(new_id), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    return data


@app.delete("/api/projects/{project_id}")
async def delete_project(project_id: str, user: dict = Depends(auth.get_admin_user)):
    _project_path(project_id).unlink(missing_ok=True)
    return {"ok": True}


app.mount("/", StaticFiles(directory=str(Path(__file__).parent.parent / "frontend"), html=True), name="frontend")

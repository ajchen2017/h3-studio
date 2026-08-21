import asyncio
import json
import math
import shutil
import subprocess
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import psutil

from workflow_builder import build_workflow
from comfy_client import queue_via_prompt_api, get_history, check_comfyui_alive

COMFYUI_INPUT_DIR = Path(r"C:\Users\user\ai\ComfyUI\ComfyUI\input")
COMFYUI_OUTPUT_DIR = Path(r"C:\Users\user\ai\ComfyUI\ComfyUI\output")
JOBS_DIR = Path(__file__).parent / "jobs"
UPLOADS_DIR = Path(__file__).parent / "uploads"
VOICE_PROFILES_DIR = Path(__file__).parent / "voice_profiles"
VOICE_PROFILES_INDEX = VOICE_PROFILES_DIR / "index.json"
FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"

JOBS_DIR.mkdir(exist_ok=True)
UPLOADS_DIR.mkdir(exist_ok=True)
VOICE_PROFILES_DIR.mkdir(exist_ok=True)

app = FastAPI(title="H3 Studio API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

JOBS: dict[str, dict] = {}


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
async def list_voice_profiles():
    return {"profiles": _load_voice_profiles()}


@app.post("/api/voice-profiles")
async def create_voice_profile(name: str = Form(...), file: UploadFile = File(...)):
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
async def delete_voice_profile(profile_id: str):
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
async def get_voice_profile_audio(profile_id: str):
    profiles = _load_voice_profiles()
    match = next((p for p in profiles if p["id"] == profile_id), None)
    if not match:
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(VOICE_PROFILES_DIR / match["filename"])


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
async def translate(text: str = Form(...), target: str = Form(...), source: str = Form("auto")):
    try:
        translated = _translate_text(text, target, source)
        return {"translated": translated}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=502)


@app.post("/api/upload/image")
async def upload_image(file: UploadFile = File(...)):
    name = _save_upload(file, COMFYUI_INPUT_DIR)
    return {"filename": name}


@app.post("/api/upload/audio")
async def upload_audio(file: UploadFile = File(...)):
    name = _save_upload(file, COMFYUI_INPUT_DIR)
    return {"filename": name}


@app.post("/api/upload/video")
async def upload_video(file: UploadFile = File(...)):
    name = _save_upload(file, COMFYUI_INPUT_DIR)
    return {"filename": name}


@app.post("/api/upload/music")
async def upload_music(file: UploadFile = File(...)):
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


def _wait_for_prompt(prompt_id: str, on_tick=None) -> dict:
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


def _run_job(job_id: str, params: dict):
    job = JOBS[job_id]
    try:
        segment_length = params["segment_length_seconds"]
        total_seconds = params["duration_minutes"] * 60
        n_segments = max(1, math.ceil(total_seconds / segment_length))
        job["segments"] = [
            {"index": i, "status": "pending", "prompt_id": None, "output_file": None, "elapsed": None, "started_at": None}
            for i in range(n_segments)
        ]
        job["status"] = "running"
        _save_job_state(job_id)

        ref_images = params.get("ref_image_filenames") or []
        ref_audio = params.get("ref_audio_filename")
        ref_video = params.get("ref_video_filename")
        use_ref_video_direct = params.get("use_ref_video_direct", False)
        bg_music = params.get("bg_music_filename")

        # Extend mode: derive an initial image reference from the last frame
        # of the supplied reference video, unless the experimental direct
        # ref_video path was explicitly requested.
        if ref_video and not use_ref_video_direct:
            last_frame_name = f"{uuid.uuid4().hex}_lastframe.png"
            _extract_last_frame(COMFYUI_INPUT_DIR / ref_video, COMFYUI_INPUT_DIR / last_frame_name)
            ref_images = list(ref_images) + [last_frame_name]

        # Trim the uploaded background-music reference (head) to the
        # per-segment duration, once, and reuse the same trimmed clip for
        # every segment - the model only sees duration_seconds of context
        # per call anyway.
        bg_music_trimmed = None
        if bg_music:
            trimmed_name = f"{uuid.uuid4().hex}_music_trimmed{Path(bg_music).suffix}"
            _trim_audio(COMFYUI_INPUT_DIR / bg_music, COMFYUI_INPUT_DIR / trimmed_name, segment_length)
            bg_music_trimmed = trimmed_name

        segment_output_paths = []
        base_seed = params.get("seed") or int(time.time())

        for i in range(n_segments):
            seg = job["segments"][i]
            seg["status"] = "running"
            seg_start = time.time()
            seg["started_at"] = seg_start
            _save_job_state(job_id)

            prefix = f"video/h3studio_{job_id}_seg{i:03d}"
            wf = build_workflow(
                prompt=params["prompt"],
                ref_image_filenames=ref_images[:3],
                ref_audio_filename=ref_audio,
                bg_music_filename=bg_music_trimmed,
                ref_video_filename=ref_video if (i == 0 and use_ref_video_direct) else None,
                use_ref_video_direct=use_ref_video_direct and i == 0,
                width=params.get("width", 864),
                height=params.get("height", 480),
                duration_seconds=segment_length,
                seed=base_seed + i,
                filename_prefix=prefix,
            )
            prompt_id = queue_via_prompt_api(wf, client_id=f"h3studio-{job_id}")
            seg["prompt_id"] = prompt_id
            _save_job_state(job_id)

            rec = _wait_for_prompt(prompt_id, on_tick=lambda: _save_job_state(job_id))
            seg["elapsed"] = round(time.time() - seg_start, 1)

            status = rec.get("status", {})
            if status.get("status_str") != "success":
                seg["status"] = "error"
                job["status"] = "error"
                job["error"] = f"segment {i} failed: {json.dumps(status)[:500]}"
                _save_job_state(job_id)
                return

            out_file = None
            for node_out in rec.get("outputs", {}).values():
                for img in node_out.get("images", []):
                    out_file = COMFYUI_OUTPUT_DIR / img["subfolder"] / img["filename"]
            if out_file is None or not out_file.exists():
                seg["status"] = "error"
                job["status"] = "error"
                job["error"] = f"segment {i}: no output file found"
                _save_job_state(job_id)
                return

            seg["output_file"] = str(out_file)
            seg["status"] = "done"
            segment_output_paths.append(out_file)
            _save_job_state(job_id)

            # After the first segment in extend mode, keep continuity by using
            # its own last frame as the reference for subsequent segments too
            # (keeps the same validated last-frame technique going).
            if i == 0 and not ref_images:
                cont_frame = f"{uuid.uuid4().hex}_seg0_lastframe.png"
                _extract_last_frame(out_file, COMFYUI_INPUT_DIR / cont_frame)
                ref_images = [cont_frame]

        job["status"] = "concatenating"
        _save_job_state(job_id)
        final_raw = JOBS_DIR / job_id / "final_raw.mp4"
        final_raw.parent.mkdir(exist_ok=True, parents=True)

        all_paths = segment_output_paths
        if ref_video and not use_ref_video_direct:
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
        job["status"] = "error"
        job["error"] = str(e)
        _save_job_state(job_id)


@app.get("/api/health")
async def health():
    alive, detail = check_comfyui_alive()
    return {"comfyui_alive": alive, "detail": detail}


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
async def system_stats():
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


@app.post("/api/generate")
async def generate(
    prompt: str = Form(...),
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
):
    # Fail fast rather than accepting the job and letting the user wait
    # through an edit-and-submit cycle before discovering ComfyUI is down.
    alive, detail = check_comfyui_alive()
    if not alive:
        return JSONResponse(
            {"error": f"ComfyUI 目前無法連線（{detail}），請確認 ComfyUI 是否正在執行後再試一次。"},
            status_code=503,
        )

    job_id = uuid.uuid4().hex[:12]
    params = {
        "prompt": prompt,
        "ref_image_filenames": json.loads(ref_image_filenames),
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
    }
    JOBS[job_id] = {"status": "queued", "segments": [], "params": params, "created_at": time.time()}
    _save_job_state(job_id)
    asyncio.get_event_loop().run_in_executor(None, _run_job, job_id, params)
    return {"job_id": job_id}


@app.get("/api/jobs/latest")
async def latest_job():
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
async def job_status(job_id: str):
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
        "ref_image_filenames": params.get("ref_image_filenames") or [],
    }


@app.get("/api/uploads/image/{filename}")
async def get_uploaded_image(filename: str):
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
async def job_video(job_id: str):
    job = JOBS.get(job_id)
    if not job or "final_video" not in job:
        return JSONResponse({"error": "not ready"}, status_code=404)
    return FileResponse(job["final_video"], media_type="video/mp4")


app.mount("/", StaticFiles(directory=str(Path(__file__).parent.parent / "frontend"), html=True), name="frontend")

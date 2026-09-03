import asyncio
import json
import subprocess
import time
import urllib.error
import urllib.request

import psutil

COMFY_URL = "http://127.0.0.1:8188"

# 2026-08-24 incident: ComfyUI's committed memory grew from a clean ~1-2GB
# baseline to 27GB, then to 51GB across two separate generation attempts,
# both crashing with "HostBuffer.read_file_slice failed" once Windows'
# commit charge got critically tight (>90%) - the async weight-offload /
# pinned-memory path (now disabled by default in start_comfyui.bat) looked
# like the source. This threshold is a safety net regardless of whether
# that flag change fully fixes the leak: never start a job on top of an
# already-bloated ComfyUI process.
COMFY_MEMORY_GUARD_GB = 15
COMFY_TASK_NAME = "H3Studio-ComfyUI"


def _find_comfyui_process():
    for p in psutil.process_iter(["pid", "name", "exe"]):
        try:
            exe = (p.info.get("exe") or "").lower()
            # Windows paths are case-insensitive on disk but this folder's
            # actual casing varies by context ("comfyUI" vs "ComfyUI") -
            # compare lowercased rather than assume one.
            if "comfyui" in exe and (p.info.get("name") or "").lower().startswith("python"):
                return p
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return None


def comfyui_memory_gb() -> float | None:
    """Windows commit-charge footprint of the ComfyUI process (matches
    Task Manager's 'Commit size' / PowerShell's PagedMemorySize64) - None
    if the process isn't found (not our job to start it from cold here)."""
    proc = _find_comfyui_process()
    if not proc:
        return None
    try:
        return proc.memory_info().pagefile / 1e9
    except (psutil.NoSuchProcess, psutil.AccessDenied, AttributeError):
        return None


def restart_comfyui_and_wait(timeout=90) -> bool:
    proc = _find_comfyui_process()
    if proc:
        try:
            proc.kill()
            proc.wait(timeout=10)
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.TimeoutExpired):
            pass
    subprocess.run(["schtasks", "/Run", "/TN", COMFY_TASK_NAME], capture_output=True)
    t0 = time.time()
    while time.time() - t0 < timeout:
        alive, _ = check_comfyui_alive(timeout=2)
        if alive:
            return True
        time.sleep(3)
    return False


def ensure_comfyui_memory_healthy():
    """Call before queuing a prompt. Restarts ComfyUI first if its own
    memory footprint is already past COMFY_MEMORY_GUARD_GB, so a job never
    starts on top of an already-bloated process. Raises RuntimeError if a
    needed restart doesn't come back up in time."""
    mem_gb = comfyui_memory_gb()
    if mem_gb is None or mem_gb <= COMFY_MEMORY_GUARD_GB:
        return
    if not restart_comfyui_and_wait():
        raise RuntimeError(
            f"ComfyUI 記憶體過高（{mem_gb:.1f}GB），自動重啟後逾時未恢復，請手動檢查"
        )


def flatten_and_queue(workflow_ui_json: dict) -> str:
    """Submit a UI-format workflow graph to ComfyUI by asking its own
    frontend graph object to flatten + queue it. Returns the ComfyUI
    system prompt_id.

    We don't hand-roll the UI->API graph flattening (subgraphs, autogrow
    inputs, etc. make that error-prone) - instead this talks to ComfyUI's
    documented queue endpoint using a client id, exactly like the web UI
    does, but from the backend process directly.
    """
    raise NotImplementedError("use queue_via_browser or queue_via_prompt_api")


def queue_via_prompt_api(api_format_prompt: dict, client_id: str) -> str:
    payload = json.dumps({"prompt": api_format_prompt, "client_id": client_id}).encode("utf-8")
    req = urllib.request.Request(f"{COMFY_URL}/prompt", data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        # ComfyUI's 400 response body carries the actual validation error
        # (which node/input failed and why) - the bare HTTPError str() is
        # just "HTTP Error 400: Bad Request", useless on its own.
        detail = e.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(detail)
            detail = json.dumps(parsed.get("node_errors", parsed), ensure_ascii=False)[:1500]
        except Exception:
            detail = detail[:1500]
        raise RuntimeError(f"ComfyUI 拒絕此 workflow（HTTP {e.code}）：{detail}") from e
    return body["prompt_id"]


def interrupt_current(timeout=10) -> None:
    """Stops whatever ComfyUI is currently executing (the /interrupt
    endpoint used manually during this session's Haiku-hallucination
    incidents, e.g. `curl -X POST http://127.0.0.1:8188/interrupt`) - used
    by the job "stop" button. Only ever one segment is actually queued at
    a time (main.py's _run_job waits for each segment before queuing the
    next), so there's no backlog to also clear."""
    req = urllib.request.Request(f"{COMFY_URL}/interrupt", data=b"", method="POST")
    with urllib.request.urlopen(req, timeout=timeout):
        pass


def check_comfyui_alive(timeout=3) -> tuple[bool, str]:
    """Lightweight reachability check - used to fail fast before a user
    fills out a whole generation form, rather than only discovering
    ComfyUI is down after clicking generate and waiting."""
    try:
        req = urllib.request.Request(f"{COMFY_URL}/system_stats")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status == 200:
                return True, "ok"
            return False, f"ComfyUI 回應異常狀態碼: {resp.status}"
    except Exception as e:
        return False, str(e)


def get_history(prompt_id: str) -> dict:
    req = urllib.request.Request(f"{COMFY_URL}/history/{prompt_id}")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


async def wait_for_completion(prompt_id: str, poll_interval=5, timeout=3600):
    elapsed = 0
    while elapsed < timeout:
        hist = get_history(prompt_id)
        rec = hist.get(prompt_id)
        if rec:
            status = rec.get("status", {})
            if status.get("status_str") in ("success", "error"):
                return rec
        await asyncio.sleep(poll_interval)
        elapsed += poll_interval
    raise TimeoutError(f"prompt {prompt_id} did not finish within {timeout}s")

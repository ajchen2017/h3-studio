import asyncio
import json
import urllib.error
import urllib.request

COMFY_URL = "http://127.0.0.1:8188"


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

import json
import copy
from pathlib import Path

TEMPLATE_PATH = Path(__file__).parent / "template_ref2va_api.json"

R2V_NODE_ID = "136"
PROMPT_NODE_ID = "138"
DURATION_NODE_ID = "132"
RESOLUTION_NODE_ID = "115"
SAVE_NODE_ID = "92"
SEED_NODE_ID = "129"
IMAGE_LOADER_IDS = ["137", "139", "141"]
AUDIO_LOADER_ID = "142"
MUSIC_LOADER_ID = "148"
VIDEO_LOADER_ID = "143"
GET_VIDEO_COMPONENTS_ID = "144"


def _load_template() -> dict:
    with open(TEMPLATE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def build_workflow(
    prompt: str,
    ref_image_filenames=None,
    ref_audio_filename=None,
    bg_music_filename=None,
    ref_video_filename=None,
    use_ref_video_direct=False,
    width=864,
    height=480,
    duration_seconds=15,
    seed=None,
    filename_prefix="video/h3studio",
):
    """Build a ComfyUI API-format prompt dict for one segment.

    ref_image_filenames: filenames already uploaded into ComfyUI's input/
        folder (up to 3 people).
    ref_audio_filename: single voice reference filename, wired to
        ref_audio_0 (optional).
    bg_music_filename: background-music reference, wired to the separate
        ref_audio_1 slot so it doesn't collide with the voice reference
        (optional, experimental — untested whether MiniMax H3 actually
        treats this as a music bed vs. just another voice/style cue; the
        prompt's non_diegetic_music text should describe it explicitly,
        e.g. "the score follows the mood and rhythm of the reference
        track", to help the model assign it the right role).
    ref_video_filename: reference clip filename, 2-15s (optional). Only
        wired in when use_ref_video_direct is True — otherwise the caller
        should extract a last frame and pass it via ref_image_filenames
        instead (the validated technique; see MiniMax-AI/MiniMax-H3#17
        for why per-character voice binding is the part that's flaky, not
        this).
    """
    ref_image_filenames = ref_image_filenames or []
    if len(ref_image_filenames) > len(IMAGE_LOADER_IDS):
        raise ValueError(f"template only supports up to {len(IMAGE_LOADER_IDS)} reference images")

    data = _load_template()
    r2v_inputs = data[R2V_NODE_ID]["inputs"]

    data[PROMPT_NODE_ID]["inputs"]["value"] = prompt
    data[DURATION_NODE_ID]["inputs"]["value"] = duration_seconds
    data[RESOLUTION_NODE_ID]["inputs"]["megapixels"] = round((width * height) / 1_000_000, 3)
    data[SAVE_NODE_ID]["inputs"]["filename_prefix"] = filename_prefix
    if seed is not None:
        data[SEED_NODE_ID]["inputs"]["noise_seed"] = seed

    used_node_ids = set()

    for i, loader_id in enumerate(IMAGE_LOADER_IDS):
        key = f"ref_images.ref_image_{i}"
        if i < len(ref_image_filenames):
            data[loader_id]["inputs"]["image"] = ref_image_filenames[i]
            r2v_inputs[key] = [loader_id, 0]
            used_node_ids.add(loader_id)
        else:
            r2v_inputs.pop(key, None)

    if ref_audio_filename:
        data[AUDIO_LOADER_ID]["inputs"]["audio"] = ref_audio_filename
        r2v_inputs["ref_audios.ref_audio_0"] = [AUDIO_LOADER_ID, 0]
        used_node_ids.add(AUDIO_LOADER_ID)
    else:
        r2v_inputs.pop("ref_audios.ref_audio_0", None)

    if bg_music_filename:
        data[MUSIC_LOADER_ID]["inputs"]["audio"] = bg_music_filename
        r2v_inputs["ref_audios.ref_audio_1"] = [MUSIC_LOADER_ID, 0]
        used_node_ids.add(MUSIC_LOADER_ID)
    else:
        r2v_inputs.pop("ref_audios.ref_audio_1", None)

    if ref_video_filename and use_ref_video_direct:
        data[VIDEO_LOADER_ID]["inputs"]["file"] = ref_video_filename
        r2v_inputs["ref_videos.ref_video_0"] = [GET_VIDEO_COMPONENTS_ID, 0]
        used_node_ids.add(VIDEO_LOADER_ID)
        used_node_ids.add(GET_VIDEO_COMPONENTS_ID)
    else:
        r2v_inputs.pop("ref_videos.ref_video_0", None)

    # Drop unused loader nodes entirely rather than leaving dead placeholders
    # (LoadImage with an empty/nonexistent filename, etc.) in the graph.
    for loader_id in IMAGE_LOADER_IDS:
        if loader_id not in used_node_ids:
            data.pop(loader_id, None)
    if AUDIO_LOADER_ID not in used_node_ids:
        data.pop(AUDIO_LOADER_ID, None)
    if MUSIC_LOADER_ID not in used_node_ids:
        data.pop(MUSIC_LOADER_ID, None)
    if VIDEO_LOADER_ID not in used_node_ids:
        data.pop(VIDEO_LOADER_ID, None)
        data.pop(GET_VIDEO_COMPONENTS_ID, None)

    return data

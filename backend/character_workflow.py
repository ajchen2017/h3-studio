# Text-to-image workflow for generating character reference photos from a
# visual description (backend/main.py's /api/characters/generate-photo).
# Separate pipeline from workflow_builder.py's MiniMax H3 ref2va graph -
# this uses a plain SD3.5 checkpoint, already installed locally alongside
# the H3/music models (F:/ComfyUI/models), to solve the "no reference
# image -> face drifts between independently-generated segments" problem
# documented in prompt_rewriter.py: generate a portrait once, save it into
# the existing ref-image library, then it's usable as a normal <Picture N>
# reference like any manually-uploaded photo.

# SD3.5 needs its 3 text encoders loaded separately (TripleCLIPLoader) -
# the checkpoint file itself doesn't bundle a working CLIP, confirmed by a
# real "clip input is invalid: None" error when tried via
# CheckpointLoaderSimple's own CLIP output alone.
CLIP_NAMES = ("clip_l.safetensors", "clip_g.safetensors", "t5xxl_fp8_e4m3fn.safetensors")
CHECKPOINT_NAME = "sd3.5_large.safetensors"

DEFAULT_NEGATIVE_PROMPT = (
    "blurry, low quality, cartoon, illustration, deformed, extra limbs, "
    "text, watermark, bad anatomy, multiple people, collage"
)


def build_character_image_workflow(
    positive_text: str,
    seed: int,
    negative_text: str = DEFAULT_NEGATIVE_PROMPT,
    width: int = 640,
    height: int = 960,
    filename_prefix: str = "character/h3studio_char",
) -> dict:
    """Portrait aspect ratio (640x960 by default) - reference photos are
    headshot/portrait framing, not landscape video framing. Lower than
    SD3.5's usual 832x1216 recommendation, traded for faster generation
    since these are reference photos, not final deliverables."""
    return {
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": CHECKPOINT_NAME}},
        "9": {"class_type": "TripleCLIPLoader", "inputs": {
            "clip_name1": CLIP_NAMES[0], "clip_name2": CLIP_NAMES[1], "clip_name3": CLIP_NAMES[2],
        }},
        "2": {"class_type": "CLIPTextEncode", "inputs": {"text": positive_text, "clip": ["9", 0]}},
        "3": {"class_type": "CLIPTextEncode", "inputs": {"text": negative_text, "clip": ["9", 0]}},
        "4": {"class_type": "ModelSamplingSD3", "inputs": {"model": ["1", 0], "shift": 3.0}},
        "5": {"class_type": "EmptySD3LatentImage", "inputs": {"width": width, "height": height, "batch_size": 1}},
        "6": {"class_type": "KSampler", "inputs": {
            "model": ["4", 0], "seed": seed, "steps": 30, "cfg": 4.5,
            "sampler_name": "dpmpp_2m", "scheduler": "sgm_uniform",
            "positive": ["2", 0], "negative": ["3", 0], "latent_image": ["5", 0], "denoise": 1.0,
        }},
        "7": {"class_type": "VAEDecode", "inputs": {"samples": ["6", 0], "vae": ["1", 2]}},
        "8": {"class_type": "SaveImage", "inputs": {"images": ["7", 0], "filename_prefix": filename_prefix}},
    }


# Z-Image-Turbo (Tongyi/Comfy-Org) - candidate replacement for the SD3.5
# pipeline above, being A/B tested per 2026-09-02 request. Distilled 8-NFE
# turbo model: single CLIP (no TripleCLIPLoader), cfg=1 (no negative
# guidance needed - ConditioningZeroOut stands in for "negative"), much
# smaller than SD3.5 Large. Node graph and defaults (res_multistep/simple,
# steps=8, cfg=1, ModelSamplingAuraFlow shift=3) copied from Comfy-Org's own
# official "Text to Image (Z-Image-Turbo)" workflow template, NOT guessed -
# see https://github.com/Comfy-Org/workflow_templates (image_z_image_turbo).
# CLIPLoader type is "lumina2", not "z_image" - confirmed from that same
# template; Z-Image shares Lumina2's text-encoder plumbing in ComfyUI.
ZIMAGE_UNET_NAME = "z_image_turbo_int8_convrot.safetensors"
ZIMAGE_CLIP_NAME = "qwen_3_4b_fp8_mixed.safetensors"
ZIMAGE_VAE_NAME = "ae.safetensors"


def build_character_image_workflow_zimage(
    positive_text: str,
    seed: int,
    width: int = 640,
    height: int = 960,
    filename_prefix: str = "character/h3studio_char_zimage",
) -> dict:
    """Same portrait framing as build_character_image_workflow() above, for
    a like-for-like comparison."""
    return {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": ZIMAGE_UNET_NAME, "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": ZIMAGE_CLIP_NAME, "type": "lumina2", "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": ZIMAGE_VAE_NAME}},
        "4": {"class_type": "CLIPTextEncode", "inputs": {"text": positive_text, "clip": ["2", 0]}},
        "5": {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["4", 0]}},
        "6": {"class_type": "ModelSamplingAuraFlow", "inputs": {"model": ["1", 0], "shift": 3}},
        "7": {"class_type": "EmptySD3LatentImage", "inputs": {"width": width, "height": height, "batch_size": 1}},
        "8": {"class_type": "KSampler", "inputs": {
            "model": ["6", 0], "seed": seed, "steps": 8, "cfg": 1,
            "sampler_name": "res_multistep", "scheduler": "simple",
            "positive": ["4", 0], "negative": ["5", 0], "latent_image": ["7", 0], "denoise": 1,
        }},
        "9": {"class_type": "VAEDecode", "inputs": {"samples": ["8", 0], "vae": ["3", 0]}},
        "10": {"class_type": "SaveImage", "inputs": {"images": ["9", 0], "filename_prefix": filename_prefix}},
    }

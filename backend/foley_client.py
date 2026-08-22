"""Builds the ComfyUI node graph for generating a sound-effects track from
a text prompt + the just-generated video (for audio-video sync), via
HunyuanVideo-Foley. The video-conditioned graph shape comes from minimax
music's git history (commit d16687d in that repo dropped this path since
that app had no real use for image/video-conditioned SFX) - h3 studio's
case is exactly what it's for: SFX synced to a specific generated video."""

MODEL_NAME = "hunyuanvideo_foley_fp8_e4m3fn.safetensors"
VAE_NAME = "vae_128d_48k_fp16.safetensors"
SYNCHFORMER_NAME = "synchformer_state_dict_fp16.safetensors"


def build_video_prompt(video_filename, prompt, negative_prompt, duration, cfg_scale, steps, seed, filename_prefix):
    return {
        "model": {"class_type": "HunyuanModelLoader", "inputs": {"model_name": MODEL_NAME, "precision": "auto", "quantization": "auto"}},
        "deps": {"class_type": "HunyuanDependenciesLoader", "inputs": {"vae_name": VAE_NAME, "synchformer_name": SYNCHFORMER_NAME}},
        "load_video": {"class_type": "LoadVideo", "inputs": {"file": video_filename}},
        "video_components": {"class_type": "GetVideoComponents", "inputs": {"video": ["load_video", 0]}},
        "sampler": {
            "class_type": "HunyuanFoleySampler",
            "inputs": {
                "hunyuan_model": ["model", 0],
                "hunyuan_deps": ["deps", 0],
                # frame_rate comes from the video itself rather than a fixed
                # value, same as the original video-conditioned path.
                "frame_rate": ["video_components", 2],
                "duration": duration,
                "prompt": prompt,
                "negative_prompt": negative_prompt,
                "cfg_scale": cfg_scale,
                "steps": steps,
                "sampler": "euler",
                "batch_size": 1,
                "seed": seed,
                "force_offload": True,
                "image": ["video_components", 0],
            },
        },
        "save": {"class_type": "SaveAudioAdvanced", "inputs": {"audio": ["sampler", 0], "filename_prefix": filename_prefix, "format": "flac"}},
    }

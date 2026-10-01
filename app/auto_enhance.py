"""Local AI "Auto Enhance": a small multimodal model (Qwen2.5-VL-7B-Instruct,
run entirely on-device) looks at the photo -- not just its histogram -- and
suggests global tone/color adjustments the way Luminar Neo's AI enhance
does: auto exposure, contrast, white balance/color-cast correction, and
saturation/vibrance, in one click.

Qwen2.5-VL, not Gemma 3, deliberately: Gemma's checkpoints are gated on
Hugging Face (require an account, accepting Google's license, and an auth
token before they'll even download), which doesn't fit an otherwise fully
local, no-account-needed feature. Qwen2.5-VL-7B-Instruct is Apache-2.0 and
downloads with no login at all.

The model is loaded lazily (first use only, with a one-time ~16GB weight
download cached by Hugging Face's own cache dir, gated behind an explicit
in-app confirmation -- see EditorView._on_auto_enhance_clicked) and kept
resident for the life of the process, the same pattern as segmentation.py's
SAM2 wrapper. Runs on CUDA when available (~14GB VRAM in bfloat16 -- fits a
16GB card with headroom to spare only if nothing else heavy is using the GPU
at the time; if loading or inference hits a CUDA out-of-memory error, this
falls back to CPU automatically and retries there, same as if no GPU had
been detected at all). CPU inference works but is slow, a few minutes per
photo.

A vision-language model is not reliable at estimating precise pixel
statistics purely by looking -- the same reason a human retoucher glances at
the histogram, not just the photo -- so the prompt is grounded with actual
computed histogram stats (mean luminance, clipping, per-channel balance)
alongside the image itself.
"""

import json
import re
import threading

import numpy as np

from . import processing
from .quality import CLIP_PIXEL_THRESHOLD

MODEL_ID = "Qwen/Qwen2.5-VL-7B-Instruct"

# Plenty of resolution for a global tone/color judgment; keeps inference
# fast without needing the full editing-preview or export resolution.
ANALYSIS_MAX_DIM = 896

# key -> (lo, hi, description). Same ranges as processing.default_recipe(),
# scoped to fields it makes sense for a model to judge from looking at a
# photo -- skips per-channel RGB gain (redundant with temp/tint for this),
# sharpen/denoise (image-quality, not scene-content judgments), and curves
# (too unconstrained a target for reliable JSON output).
RECIPE_FIELDS = {
    "exposure": (-5.0, 5.0, "overall brightness in EV stops; negative darkens, positive brightens"),
    "contrast": (-100.0, 100.0, "tonal contrast/punch; negative flattens, positive punches up"),
    "highlights": (-100.0, 100.0, "negative recovers blown-out bright areas, positive boosts them"),
    "shadows": (-100.0, 100.0, "positive lifts dark areas/opens up shadow detail, negative deepens them"),
    "whites": (-100.0, 100.0, "sets the white point"),
    "blacks": (-100.0, 100.0, "sets the black point"),
    "temp": (-100.0, 100.0, "white balance warmth; negative cooler/blue, positive warmer/yellow"),
    "tint": (-100.0, 100.0, "white balance green-magenta; negative green, positive magenta"),
    "saturation": (-100.0, 100.0, "overall color intensity, all colors shifted equally"),
    "vibrance": (-100.0, 100.0, "boosts muted colors more than already-saturated ones, protects skin tones"),
}

_device = None
_model = None
_processor = None
# RLock: suggest_adjustments() holds this for the whole call (model-load-or-
# reuse + inference); the model object isn't guaranteed safe for concurrent
# invocation from multiple threads, so this also serializes actual inference
# calls, not just loading -- same reasoning as segmentation.py's lock.
_model_lock = threading.RLock()


def _detect_device() -> str:
    global _device
    if _device is not None:
        return _device
    try:
        import torch

        if torch.cuda.is_available():
            x = torch.randn(64, 64, device="cuda")
            _ = x @ x
            torch.cuda.synchronize()
            _device = "cuda"
        else:
            _device = "cpu"
    except Exception:
        _device = "cpu"
    return _device


def is_gpu_active() -> bool:
    return _detect_device() != "cpu"


def device_description() -> str:
    if not is_gpu_active():
        return "CPU (no compatible GPU detected -- Auto Enhance will be slow, a minute or more per photo)"
    try:
        import torch

        return f"GPU ({torch.cuda.get_device_name(0)})"
    except Exception:
        return "GPU"


def is_model_loaded() -> bool:
    return _model is not None


def _get_model_and_processor():
    global _model, _processor, _device
    with _model_lock:
        if _model is None:
            import torch
            from transformers import AutoProcessor, Qwen2_5_VLForConditionalGeneration

            if _detect_device() == "cuda":
                try:
                    _model = Qwen2_5_VLForConditionalGeneration.from_pretrained(
                        MODEL_ID, dtype=torch.bfloat16, device_map="auto"
                    )
                except torch.cuda.OutOfMemoryError:
                    # The earlier _detect_device() probe only exercises a tiny
                    # tensor op -- it confirms CUDA works at all, not that
                    # ~14GB is actually free right now (e.g. another app is
                    # already using the GPU). Fall back to CPU exactly as if
                    # no GPU had been detected in the first place.
                    print("[auto-enhance] CUDA out of memory loading the model -- falling back to CPU")
                    torch.cuda.empty_cache()
                    _device = "cpu"
                    _model = Qwen2_5_VLForConditionalGeneration.from_pretrained(MODEL_ID, dtype=torch.float32).to(
                        "cpu"
                    )
            else:
                _model = Qwen2_5_VLForConditionalGeneration.from_pretrained(MODEL_ID, dtype=torch.float32).to("cpu")
            _model.eval()
            _processor = AutoProcessor.from_pretrained(MODEL_ID)
    return _model, _processor


def _image_stats(image_float: np.ndarray) -> str:
    img = np.clip(image_float, 0.0, 1.0)
    luminance = 0.2126 * img[..., 0] + 0.7152 * img[..., 1] + 0.0722 * img[..., 2]
    mean_lum = float(luminance.mean())
    clipped_shadows = float((luminance < 0.01).mean() * 100)
    clipped_highlights = float((luminance > 0.99).mean() * 100)
    r_mean, g_mean, b_mean = (float(img[..., c].mean()) for c in range(3))
    return (
        f"Mean luminance: {mean_lum:.3f} (0=black, 1=white). "
        f"Shadow clipping: {clipped_shadows:.1f}% of pixels near-black. "
        f"Highlight clipping: {clipped_highlights:.1f}% of pixels near-white. "
        f"Channel means -- R: {r_mean:.3f}, G: {g_mean:.3f}, B: {b_mean:.3f} "
        "(a color cast shows up as one channel notably higher than the others)."
    )


def _downsample_for_analysis(image_float: np.ndarray) -> np.ndarray:
    h, w = image_float.shape[:2]
    scale = ANALYSIS_MAX_DIM / max(h, w)
    if scale >= 1.0:
        return image_float
    import cv2

    new_w, new_h = max(1, round(w * scale)), max(1, round(h * scale))
    return cv2.resize(image_float, (new_w, new_h), interpolation=cv2.INTER_AREA)


def _build_prompt_text(stats: str) -> str:
    field_lines = "\n".join(
        f'- "{key}": number from {lo} to {hi} -- {desc}' for key, (lo, hi, desc) in RECIPE_FIELDS.items()
    )
    return (
        "You are an expert photo retoucher. Look at this photo, then suggest global "
        "tone and color adjustments that would make it look its best -- correct "
        "exposure, restore contrast, fix any color cast, and make colors pop without "
        "looking over-processed. Judge the photo on its own terms (a moody dark photo "
        "or a deliberately warm sunset shouldn't be \"corrected\" back to neutral) "
        "rather than aiming for a flat, technically neutral midpoint.\n\n"
        f"Histogram statistics for this exact photo:\n{stats}\n\n"
        "Adjustments to suggest, all optional (use 0 for anything that's already fine):\n"
        f"{field_lines}\n\n"
        "Respond with ONLY a JSON object, no other text, in exactly this form:\n"
        '{"adjustments": {"exposure": 0, "contrast": 0, "highlights": 0, "shadows": 0, '
        '"whites": 0, "blacks": 0, "temp": 0, "tint": 0, "saturation": 0, "vibrance": 0}, '
        '"reasoning": "one or two sentences on what you changed and why"}'
    )


def _parse_response(text: str) -> tuple[dict, str]:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    adjustments = {}
    reasoning = ""
    if match:
        try:
            parsed = json.loads(match.group(0))
            raw_adjustments = parsed.get("adjustments", {}) if isinstance(parsed, dict) else {}
            reasoning = str(parsed.get("reasoning", ""))[:500] if isinstance(parsed, dict) else ""
            for key, (lo, hi, _desc) in RECIPE_FIELDS.items():
                value = raw_adjustments.get(key, 0.0)
                try:
                    value = float(value)
                except (TypeError, ValueError):
                    value = 0.0
                adjustments[key] = max(lo, min(hi, value))
        except (json.JSONDecodeError, AttributeError, TypeError):
            adjustments = {}
    if not adjustments:
        adjustments = {key: 0.0 for key in RECIPE_FIELDS}
    return adjustments, reasoning


# How many more percentage points of the photo the suggestion is allowed to
# push into near-black/near-white than the original already had, before
# exposure/contrast/highlights get scaled back. Confirmed live: the model
# suggested +2.00 EV and +30 contrast against a photo that measured mean
# luminance 0.510 with ~0% shadow/highlight clipping -- a textbook-correct
# exposure -- and its own stated reasoning didn't reference those stats at
# all, it just narrated a generic "enhanced the details and colors". A
# vision-language model's own account of its reasoning isn't grounded
# enough to trust here (same unreliability the module docstring already
# calls out for reading stats off the image), so this checks the actual
# pixel data after the fact instead.
MAX_CLIP_INCREASE_PCT = 5.0

# The fields that can drive a photo toward pure black/white with no built-in
# ceiling of their own -- unlike highlights/shadows/whites/blacks, which
# apply_tone_regions() already weights and caps at 0.35 of the adjustable
# range, exposure is an unbounded multiplier (2**EV) and contrast is an
# unbounded pivot scale, so either alone can blow out an already-correct
# exposure with no limit from the pipeline itself. highlights is included
# because, unlike the other three region tools, a positive value boosts
# (rather than recovers) the bright end, so it can also be used to blow out
# highlights specifically.
_BLOWOUT_PRONE_FIELDS = ("exposure", "contrast", "highlights")


def _clip_pcts(image_float: np.ndarray) -> tuple[float, float]:
    """Uses quality.CLIP_PIXEL_THRESHOLD (0.85), not a literal-0/1 cutoff --
    confirmed live on the beach photo that prompted this guardrail: pushing
    +2 EV/+30 contrast through the real pipeline left most of the frame
    washed out to pale pink/white (mean luminance 0.92) while only 2.2% of
    pixels actually hit literal 1.0, because contrast's pivot scaling and
    the final clip-to-[0,1] don't drive every overexposed pixel all the way
    to the ceiling. At the 0.85 threshold that same result measures 93%
    highlight-clipped -- the number that actually matches what the photo
    looks like."""
    img = np.clip(image_float, 0.0, 1.0)
    luminance = 0.2126 * img[..., 0] + 0.7152 * img[..., 1] + 0.0722 * img[..., 2]
    shadow_pct = float((luminance < (1.0 - CLIP_PIXEL_THRESHOLD)).mean() * 100.0)
    highlight_pct = float((luminance > CLIP_PIXEL_THRESHOLD).mean() * 100.0)
    return shadow_pct, highlight_pct


def _simulate(image_float: np.ndarray, adjustments: dict, scale: float) -> np.ndarray:
    """Renders the suggested recipe through the real editing pipeline
    (app.processing.apply_recipe -- the same code the live preview and
    export use), with _BLOWOUT_PRONE_FIELDS scaled toward 0 and everything
    else left at the model's suggested value."""
    recipe = processing.default_recipe()
    for key in RECIPE_FIELDS:
        value = adjustments.get(key, 0.0)
        recipe[key] = value * scale if key in _BLOWOUT_PRONE_FIELDS else value
    return processing.apply_recipe(image_float, recipe)


def _restrain_blowout(image_float: np.ndarray, adjustments: dict) -> dict:
    """Scales _BLOWOUT_PRONE_FIELDS back together if applying them as
    suggested would clip substantially more of the photo than it already
    was -- simulated against the real pixel data, not the model's own
    account of what it did. See MAX_CLIP_INCREASE_PCT."""
    if not any(adjustments.get(key) for key in _BLOWOUT_PRONE_FIELDS):
        return adjustments

    orig_shadow_pct, orig_highlight_pct = _clip_pcts(image_float)

    def over_budget(scale: float) -> bool:
        shadow_pct, highlight_pct = _clip_pcts(_simulate(image_float, adjustments, scale))
        return (
            highlight_pct > orig_highlight_pct + MAX_CLIP_INCREASE_PCT
            or shadow_pct > orig_shadow_pct + MAX_CLIP_INCREASE_PCT
        )

    if not over_budget(1.0):
        return adjustments

    # Binary search for the largest scale (0..1 of the suggested intensity)
    # that stays within budget -- 8 steps narrows it to under 0.5% of the
    # suggested value, far tighter than this triage needs.
    lo, hi = 0.0, 1.0
    for _ in range(8):
        mid = (lo + hi) / 2.0
        if over_budget(mid):
            hi = mid
        else:
            lo = mid

    restrained = dict(adjustments)
    for key in _BLOWOUT_PRONE_FIELDS:
        restrained[key] = adjustments.get(key, 0.0) * lo
    return restrained


def _generate_text(model, processor, pil_img, prompt_text: str) -> str:
    """Two ways to hand the model an image+text prompt: the documented
    apply_chat_template path with tokenize=True and the image embedded
    directly in the messages (preferred), falling back to rendering the
    chat template as plain text (tokenize=False) and passing the image
    separately to the processor -- a known transformers issue affects
    embedded-PIL-image chat templates with tokenize=True on some versions,
    and text-templating + a separate image is the standard lower-level path
    every multimodal processor supports regardless of model family, so this
    sidesteps the bug without hardcoding any model-specific chat tokens."""
    import torch

    messages = [
        {
            "role": "user",
            "content": [
                {"type": "image", "image": pil_img},
                {"type": "text", "text": prompt_text},
            ],
        }
    ]
    try:
        inputs = processor.apply_chat_template(
            messages,
            tokenize=True,
            return_dict=True,
            return_tensors="pt",
            add_generation_prompt=True,
        ).to(model.device)
    except Exception as exc:  # noqa: BLE001 - fall back to the lower-level call below
        print(f"[auto-enhance] tokenized chat template path failed ({exc}), falling back")
        chat_text = processor.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
        inputs = processor(text=chat_text, images=pil_img, return_tensors="pt").to(model.device)

    input_len = inputs["input_ids"].shape[-1]
    with torch.inference_mode():
        output = model.generate(**inputs, max_new_tokens=300, do_sample=False)
    generated = output[0][input_len:]
    return processor.decode(generated, skip_special_tokens=True)


def suggest_adjustments(image_float: np.ndarray) -> dict:
    """image_float: HxWx3 float32 in [0,1] (the editor's preview array is
    fine -- full export resolution isn't needed for a global judgment).
    Returns {"adjustments": {...ten recipe fields...}, "reasoning": str}.

    Never raises for a model-loading, inference, or parsing failure -- falls
    back to all-zero (no-op) adjustments with an empty reasoning string,
    since this is an optional one-click suggestion on an already-open photo,
    not something that should ever be able to crash or corrupt a user's
    in-progress edits."""
    try:
        with _model_lock:
            model, processor = _get_model_and_processor()
            small = _downsample_for_analysis(image_float)
            img_u8 = (np.clip(small, 0.0, 1.0) * 255.0).astype(np.uint8)
            from PIL import Image

            pil_img = Image.fromarray(img_u8, mode="RGB")
            prompt_text = _build_prompt_text(_image_stats(image_float))
            text = _generate_text(model, processor, pil_img, prompt_text)
    except Exception as exc:  # noqa: BLE001 - see docstring
        print(f"[auto-enhance] suggestion failed: {exc}")
        return {"adjustments": {key: 0.0 for key in RECIPE_FIELDS}, "reasoning": ""}

    adjustments, reasoning = _parse_response(text)
    adjustments = _restrain_blowout(image_float, adjustments)
    return {"adjustments": adjustments, "reasoning": reasoning}

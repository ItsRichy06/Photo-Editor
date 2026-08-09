"""AI object selection: point-prompted segmentation via SAM2 (Meta's Segment
Anything Model 2, through Ultralytics' wrapper). Class-agnostic -- a click
near a person, bird, car, house, or anything else produces a mask that snaps
to that specific object's real boundary, the same interaction as Photoshop's
Object Selection Tool.

The model is loaded lazily (first use only, not at import time) and cached
for the life of the process. Device selection actually exercises a real CUDA
op rather than trusting torch.cuda.is_available() alone, since GPU-wheel
support for newer NVIDIA architectures has historically been inconsistent on
Windows -- if that check fails for any reason, everything here transparently
falls back to CPU with a smaller/faster checkpoint instead of crashing.
"""

import threading

import numpy as np

GPU_CHECKPOINT = "sam2_b.pt"
CPU_CHECKPOINT = "sam2_t.pt"

_device = None
_model = None
# RLock, not Lock: segment_from_points() holds this for the whole call
# (model-load-or-reuse + inference), and get_model() re-acquires it inside
# that same call -- a plain Lock would deadlock on that nested acquire.
# The model object itself isn't guaranteed safe for concurrent invocation
# from multiple threads, so this also serializes actual inference calls,
# not just loading.
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
            _device = "0"
        else:
            _device = "cpu"
    except Exception:
        _device = "cpu"
    return _device


def is_gpu_active() -> bool:
    return _detect_device() != "cpu"


def device_description() -> str:
    if not is_gpu_active():
        return "CPU (no compatible GPU detected -- selection will be slower)"
    try:
        import torch

        return f"GPU ({torch.cuda.get_device_name(0)})"
    except Exception:
        return "GPU"


def is_model_loaded() -> bool:
    return _model is not None


def get_model():
    global _model
    with _model_lock:
        if _model is None:
            from ultralytics import SAM

            checkpoint = GPU_CHECKPOINT if is_gpu_active() else CPU_CHECKPOINT
            _model = SAM(checkpoint)
    return _model


def segment_from_points(image_float: np.ndarray, points, labels) -> np.ndarray:
    """image_float: HxWx3 float32 in [0,1]. points: [(x,y), ...] pixel coords
    in that image's frame. labels: matching list, 1=include (positive click),
    0=exclude (negative click). Returns a boolean HxW mask."""
    if not points:
        return np.zeros(image_float.shape[:2], dtype=bool)

    img_u8 = np.ascontiguousarray((np.clip(image_float, 0.0, 1.0) * 255.0).astype(np.uint8))
    device = _detect_device()
    with _model_lock:
        model = get_model()
        results = model(
            img_u8,
            points=[list(p) for p in points],
            labels=list(labels),
            device=device,
            verbose=False,
        )
        r = results[0]
        if r.masks is None or len(r.masks.data) == 0:
            return np.zeros(image_float.shape[:2], dtype=bool)
        return r.masks.data[0].cpu().numpy().astype(bool)

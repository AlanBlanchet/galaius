"""Vision inference (detection, segmentation), run as a subprocess inside the managed
`~/.interact/models/env` venv — the vendor `MACHINE_MODELS` catalog (`request["model"]`, a
Hugging Face Hub id fetched here by `from_pretrained`) AND a workspace's own registered model
(`request["weights_dir"]`, a LOCAL directory `interact.user_models` already fetched and
byte-verified safetensors-only) share this one loader: same architecture classes, same
detection/segmentation/output code, the only difference is where the weights come from.

Kept out of `interact.machines` and the interact runtime's own dependency set on purpose: a bare
`interact machine connect` must stay small, so torch/transformers live only in the isolated venv
`vision_env.ensure_vision_env` provisions on first vision-node run. This module has no dependency
on `interact` or `interact_core` so it never needs them installed into that venv either.

A long-lived worker (`vision_env.VisionWorker` keeps it): each stdin line names one JSON request
file (`{"request": <path>}`); it answers on stdout with phase lines (`{"phase": "loading_model" |
"running_model", "detail": ...}`) then one `{"result": ...}` or `{"error": ...}` line. The model it
last loaded stays loaded, so the next step on it skips the ~6 s of imports and weight loading.
"""

import base64
import io
import json
import sys
import time
import traceback
from collections.abc import Callable
from pathlib import Path

#: Mirrors interact_core.workflows.VALUE_PREVIEW_MAX_PIXELS / VALUE_PREVIEW_MAX_BYTES — literal
#: here, never imported: this script stays dependency-free (see module docstring), so the bound
#: is duplicated, not shared, and must move in step with that contract.
_PREVIEW_MAX_PIXELS = 256
_PREVIEW_MAX_BYTES = 64 * 1024


def _bounded_thumbnail(image) -> str:
    """A same-aspect thumbnail of `image`, at most `_PREVIEW_MAX_PIXELS` on its long side and
    `_PREVIEW_MAX_BYTES` bytes once WebP-encoded — the one shape every machine-built preview (an
    input photo it read, an output overlay it produced) uses, base64-encoded for the JSON result.
    Quality steps down until the byte bound holds; the last step also halves resolution, so even
    a busy, high-entropy frame still lands under the cap."""
    from PIL import Image

    thumbnail = image.convert("RGB")
    thumbnail.thumbnail((_PREVIEW_MAX_PIXELS, _PREVIEW_MAX_PIXELS), Image.LANCZOS)
    for quality in (80, 65, 50, 35, 20):
        buffer = io.BytesIO()
        thumbnail.save(buffer, format="WEBP", quality=quality, method=6)
        encoded = buffer.getvalue()
        if len(encoded) <= _PREVIEW_MAX_BYTES:
            return base64.b64encode(encoded).decode("ascii")
    thumbnail.thumbnail((_PREVIEW_MAX_PIXELS // 2, _PREVIEW_MAX_PIXELS // 2), Image.LANCZOS)
    buffer = io.BytesIO()
    thumbnail.save(buffer, format="WEBP", quality=20, method=6)
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def _say(message: dict) -> None:
    print(json.dumps(message, separators=(",", ":")), flush=True)


def infer(request: dict, loaded: dict, emit: Callable[[str, str], None]) -> dict:
    """One request's result. `loaded` holds the one model kept loaded, keyed by where its
    weights come from and its task: a different model replaces it (bounded GPU memory)."""
    weights_source = request.get("weights_dir") or request["model"]
    key = (weights_source, request["task"])
    if key not in loaded:
        # Said before the imports: the first request's torch import is most of the load.
        emit("loading_model", f"loading {request['model']}")
    import numpy as np
    import torch
    from PIL import Image, ImageDraw, ImageOps
    from transformers import AutoImageProcessor, AutoModelForImageSegmentation, AutoModelForObjectDetection

    model_id = request["model"]
    task_name = request["task"]
    license_name = request["license"]
    cache_root = Path(request["cache_root"])
    run_dir = Path(request["run_dir"])
    score_threshold = request["score_threshold"]
    image_paths = [Path(path) for path in request["image_paths"]]
    #: A workspace's own model names an already-fetched, already-byte-verified LOCAL directory
    #: (`interact.user_models.fetch_weights_dir`) — `from_pretrained` reads it exactly like a Hub
    #: cache snapshot, no network call, no `cache_dir` (there is nothing further to cache). Absent
    #: (the vendor `MACHINE_MODELS` catalog path, unchanged): `model_id` is fetched from the Hub.
    load_kwargs = {} if request.get("weights_dir") else {"cache_dir": str(cache_root)}

    if key not in loaded:
        loaded.clear()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
        processor = AutoImageProcessor.from_pretrained(weights_source, **load_kwargs)
        model_class = AutoModelForObjectDetection if task_name == "detection" else AutoModelForImageSegmentation
        # `use_safetensors=True`: refuse a pickle-based (`.bin`) checkpoint outright rather than fall
        # back to one — pooled-compute safeguard #6 (threat-model threat #5), defense-in-depth on the
        # one live model-loading path this machine has today, ahead of the workspace-model registry.
        model = model_class.from_pretrained(weights_source, use_safetensors=True, **load_kwargs)
        device = "cuda" if torch.cuda.is_available() else "cpu"
        model.to(device)
        model.eval()
        loaded[key] = (processor, model, device)
    processor, model, device = loaded[key]
    emit("running_model", f"running {model_id} on {len(image_paths)} image{'s' if len(image_paths) != 1 else ''}")

    result_images = []
    #: The first read photo, kept only long enough to thumbnail it — the machine-local file the
    #: workflow's upstream input node fed this step never leaves the machine, so this thumbnail is
    #: the only way that node's own card can show anything but its raw path.
    first_input_image = None
    total_started = time.perf_counter()
    for index, image_path in enumerate(image_paths):
        with Image.open(image_path) as source:
            image = ImageOps.exif_transpose(source).convert("RGB")
        if index == 0:
            first_input_image = image.copy()
        width, height = image.size
        inputs = processor(images=image, return_tensors="pt")
        inputs = {name: value.to(device) for name, value in inputs.items()}
        if device == "cuda":
            torch.cuda.synchronize()
        started = time.perf_counter()
        with torch.inference_mode():
            outputs = model(**inputs)
        if device == "cuda":
            torch.cuda.synchronize()
        elapsed_ms = (time.perf_counter() - started) * 1000
        overlay = image.convert("RGBA")
        records = []
        if task_name == "detection":
            predictions = processor.post_process_object_detection(
                outputs,
                threshold=score_threshold,
                target_sizes=[(height, width)],
            )[0]
            drawing = ImageDraw.Draw(overlay)
            for score, label, box in zip(predictions["scores"], predictions["labels"], predictions["boxes"], strict=True):
                x1, y1, x2, y2 = (round(value, 2) for value in box.tolist())
                name = model.config.id2label[int(label)]
                confidence = round(float(score), 4)
                drawing.rectangle((x1, y1, x2, y2), outline=(255, 68, 68, 255), width=max(2, width // 500))
                drawing.text((x1, max(0, y1 - 18)), f"{name} {confidence:.2f}", fill=(255, 68, 68, 255))
                records.append({"label": name, "score": confidence, "box_xyxy": [x1, y1, x2, y2]})
        else:
            prediction = processor.post_process_panoptic_segmentation(
                outputs,
                threshold=score_threshold,
                target_sizes=[(height, width)],
            )[0]
            segmentation = prediction["segmentation"].cpu().numpy()
            colors = np.zeros((height, width, 4), dtype=np.uint8)
            for segment in prediction["segments_info"]:
                segment_id = int(segment["id"])
                label = model.config.id2label[int(segment["label_id"])]
                mask = segmentation == segment_id
                seed = segment_id * 2654435761 % (1 << 24)
                color = ((seed >> 16) & 255, (seed >> 8) & 255, seed & 255)
                colors[mask] = (*color, 112)
                mask_name = f"mask_{index}_{segment_id}.png"
                Image.fromarray(mask.astype(np.uint8) * 255).save(run_dir / mask_name)
                records.append({"label": label, "score": round(float(segment["score"]), 4), "mask_path": mask_name, "pixels": int(mask.sum())})
            overlay = Image.alpha_composite(overlay, Image.fromarray(colors, "RGBA"))
        overlay_name = f"overlay_{index}.jpg"
        overlay.convert("RGB").save(run_dir / overlay_name, quality=88, optimize=True)
        result_images.append({"input": str(image_path), "overlay": str(run_dir / overlay_name), "count": len(records), "elapsed_ms": round(elapsed_ms, 2), "items": records})

    total_ms = (time.perf_counter() - total_started) * 1000
    manifest = {
        "schema_version": 1,
        "model": model_id,
        "task": task_name,
        "license": license_name,
        "device": device,
        "score_threshold": score_threshold,
        "images": result_images,
        "total_elapsed_ms": round(total_ms, 2),
    }
    manifest_path = run_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    preview = Image.open(result_images[0]["overlay"])
    preview.thumbnail((480, 480))
    preview_buffer = io.BytesIO()
    preview.save(preview_buffer, format="JPEG", quality=75, optimize=True)
    result = {
        "model": model_id,
        "task": task_name,
        "device": device,
        "count": sum(item["count"] for item in result_images),
        "total_elapsed_ms": round(total_ms, 2),
        "output_dir": str(run_dir),
        "manifest": str(manifest_path),
        "preview_data": base64.b64encode(preview_buffer.getvalue()).decode("ascii"),
        "input_preview_data": _bounded_thumbnail(first_input_image),
        "images": result_images,
    }
    return result


def serve() -> None:
    """Answers requests from stdin until it closes (see module docstring)."""
    loaded: dict = {}
    emit = lambda phase, detail: _say({"phase": phase, "detail": detail})
    for line in sys.stdin:
        try:
            request = json.loads(Path(json.loads(line)["request"]).read_text(encoding="utf-8"))
            _say({"result": infer(request, loaded, emit)})
        except Exception:
            _say({"error": traceback.format_exc()[-2000:]})


if __name__ == "__main__":
    serve()

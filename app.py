import os
import glob
import json
import math
import time
import traceback
import threading
from io import BytesIO
from typing import Any, Dict, List, Optional, Tuple

from flask import Flask, request, jsonify, render_template_string
from PIL import Image, ImageFile

ImageFile.LOAD_TRUNCATED_IMAGES = True
Image.MAX_IMAGE_PIXELS = int(os.environ.get("MAX_IMAGE_PIXELS", "20000000"))

app = Flask(__name__)

IMAGE_KEY = os.environ.get("IMAGE_KEY", "").strip()
DATA_DIR = os.environ.get("DATA_DIR", "image_ports")
ORIGINAL_DIR = os.environ.get("ORIGINAL_DIR", "image_originals")
VIDEO_ORIGINAL_DIR = os.environ.get("VIDEO_ORIGINAL_DIR", "video_originals")
VIDEO_CACHE_DIR = os.environ.get("VIDEO_CACHE_DIR", "video_cache")

DEFAULT_RES = int(os.environ.get("DEFAULT_RES", "96"))
DEFAULT_COLOR_STEP = int(os.environ.get("DEFAULT_COLOR_STEP", "16"))
ABS_MAX_RES = int(os.environ.get("ABS_MAX_RES", "160"))
MAX_RECTS = int(os.environ.get("MAX_RECTS", "12000"))
ALPHA_LIMIT = int(os.environ.get("ALPHA_LIMIT", "35"))

DEFAULT_VIDEO_RES = int(os.environ.get("DEFAULT_VIDEO_RES", str(min(DEFAULT_RES, 64))))
DEFAULT_VIDEO_FPS = float(os.environ.get("DEFAULT_VIDEO_FPS", "2"))
MAX_VIDEO_FPS = float(os.environ.get("MAX_VIDEO_FPS", "8"))
MAX_VIDEO_FRAMES = int(os.environ.get("MAX_VIDEO_FRAMES", "80"))
VIDEO_MAX_PIXELS = int(os.environ.get("VIDEO_MAX_PIXELS", "4096"))

for folder in (DATA_DIR, ORIGINAL_DIR, VIDEO_ORIGINAL_DIR, VIDEO_CACHE_DIR):
    os.makedirs(folder, exist_ok=True)

VIDEO_PREPARE_LOCK = threading.Lock()
VIDEO_PREPARE_JOBS: Dict[str, Dict[str, Any]] = {}


HTML = """
<!doctype html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <title>Image / Video Painter</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        body { background:#111; color:white; font-family:Arial,sans-serif; padding:24px; }
        .box { max-width:560px; margin:auto; background:#1d1d1d; border:1px solid #333; border-radius:12px; padding:18px; }
        h2 { margin-top:0; }
        input, button { width:100%; margin-top:10px; padding:10px; border-radius:8px; border:0; font-size:15px; box-sizing:border-box; }
        button { background:#00aaff; color:white; font-weight:bold; cursor:pointer; }
        .hint { color:#aaa; font-size:13px; line-height:1.45; }
        code { background:#000; padding:2px 5px; border-radius:4px; }
        .ok { color:#00ff99; }
        .bad { color:#ff7777; }
        .row { display:grid; grid-template-columns:1fr 1fr; gap:10px; }
    </style>
</head>
<body>
<div class="box">
    <h2>Image / Video Painter</h2>

    <form action="/upload" method="post" enctype="multipart/form-data">
        <input name="port" value="{{ port }}" placeholder="Port" required>

        {% if show_key %}
        <input name="key" placeholder="Key">
        {% endif %}

        <input type="file" name="image" accept="image/*,video/*" required>
        <div class="row">
            <input name="video_fps" value="{{ default_fps }}" placeholder="Video FPS">
            <input name="max_frames" value="{{ max_frames }}" placeholder="Max frames">
        </div>
        <button type="submit">Upload</button>
    </form>

    <p>Status: {{ status|safe }}</p>
    <p class="hint">
        Image: <code>/latest?port=PORT</code><br>
        Video: <code>/video/meta?port=PORT</code> · <code>/video/frame?port=PORT&amp;index=0</code><br>
        Upload is instant now. Frames are prepared/cached before playback, then Roblox gets fast diff frames.
    </p>
</div>
</body>
</html>
"""


def json_response(data: Dict[str, Any], status: int = 200):
    resp = jsonify(data)
    resp.status_code = status
    resp.headers["Cache-Control"] = "no-store, max-age=0"
    return resp


def json_error(message: str, status: int = 200, extra: Optional[Dict[str, Any]] = None):
    data = {"ok": False, "error": str(message)}
    if extra:
        data.update(extra)
    return json_response(data, status)


@app.errorhandler(Exception)
def handle_any_exception(e):
    traceback.print_exc()
    return json_error("Server error: " + str(e), 200, {"trace": traceback.format_exc()[-1800:]})


def clean_port(value: str) -> str:
    value = "".join(ch for ch in str(value or "") if ch.isdigit())
    if len(value) < 3:
        raise ValueError("Bad port")
    return value[:8]


def clean_token(value: Any, default: str = "0") -> str:
    raw = str(value if value is not None else default)
    out = []
    for ch in raw:
        if ch.isalnum() or ch in "._-":
            out.append(ch)
    return ("".join(out) or default)[:80]


def image_json_file(port: str) -> str:
    return os.path.join(DATA_DIR, f"{clean_port(port)}.json")


def image_original_file(port: str) -> str:
    return os.path.join(ORIGINAL_DIR, f"{clean_port(port)}.bin")


def video_original_file(port: str, filename: str = "") -> str:
    ext = os.path.splitext(filename or "video.bin")[1].lower()
    if not ext or len(ext) > 10:
        ext = ".bin"
    return os.path.join(VIDEO_ORIGINAL_DIR, f"{clean_port(port)}{ext}")


def find_video_original(port: str) -> Optional[str]:
    matches = glob.glob(os.path.join(VIDEO_ORIGINAL_DIR, f"{clean_port(port)}.*"))
    matches.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return matches[0] if matches else None


def video_info_file(port: str) -> str:
    return os.path.join(VIDEO_CACHE_DIR, f"{clean_port(port)}_info.json")


def video_frame_cache_file(port: str, index: int, max_w: int, max_h: int, color_step: int, fps: float, max_frames: int) -> str:
    fps_token = clean_token(str(round(float(fps), 3)).replace(".", "p"), "2")
    return os.path.join(
        VIDEO_CACHE_DIR,
        f"{clean_port(port)}_{int(max_w)}x{int(max_h)}_c{int(color_step)}_f{fps_token}_m{int(max_frames)}_i{int(index)}.json",
    )


def video_cache_job_key(port: str, max_w: int, max_h: int, color_step: int, fps: float, max_frames: int) -> str:
    fps_token = clean_token(str(round(float(fps), 3)).replace(".", "p"), "2")
    return f"{clean_port(port)}_{int(max_w)}x{int(max_h)}_c{int(color_step)}_f{fps_token}_m{int(max_frames)}"


def count_cached_video_frames(port: str, frame_count: int, max_w: int, max_h: int, color_step: int, fps: float, max_frames: int) -> int:
    total = max(0, int(frame_count or 0))
    ready = 0
    for i in range(total):
        path = video_frame_cache_file(port, i, max_w, max_h, color_step, fps, max_frames)
        if os.path.exists(path):
            ready += 1
    return ready


def atomic_write(path: str, data: bytes) -> None:
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def atomic_write_json(path: str, data: Dict[str, Any]) -> None:
    atomic_write(path, json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def load_json_file(path: str) -> Optional[Dict[str, Any]]:
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def check_key(req) -> bool:
    if not IMAGE_KEY:
        return True
    return (req.values.get("key", "") or req.headers.get("X-Image-Key", "")) == IMAGE_KEY


def read_int(name: str, default: int, lo: int, hi: int) -> int:
    try:
        value = int(float(request.values.get(name, default)))
    except Exception:
        value = default
    return max(lo, min(hi, value))


def read_float(name: str, default: float, lo: float, hi: float) -> float:
    try:
        value = float(request.values.get(name, default))
    except Exception:
        value = default
    return max(lo, min(hi, value))


def quantize_channel(v: int, step: int) -> int:
    step = max(1, min(255, int(step)))
    return max(0, min(255, round(v / step) * step))


def quantize_color(r: int, g: int, b: int, step: int) -> Tuple[int, int, int]:
    return (quantize_channel(r, step), quantize_channel(g, step), quantize_channel(b, step))


def image_to_rects(img: Image.Image, max_w: int, max_h: int, color_step: int) -> Dict[str, Any]:
    img = img.convert("RGBA")
    w, h = img.size
    scale = min(max_w / max(w, 1), max_h / max(h, 1), 1.0)
    new_w = max(1, int(w * scale))
    new_h = max(1, int(h * scale))
    img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
    pixels = img.load()

    grid: List[List[Any]] = []
    for y in range(new_h):
        row = []
        for x in range(new_w):
            r, g, b, a = pixels[x, y]
            row.append(None if a <= ALPHA_LIMIT else quantize_color(r, g, b, color_step))
        grid.append(row)

    runs = []
    for y in range(new_h):
        x = 0
        while x < new_w:
            color = grid[y][x]
            if color is None:
                x += 1
                continue
            x2 = x + 1
            while x2 < new_w and grid[y][x2] == color:
                x2 += 1
            runs.append({"x": x, "y": y, "w": x2 - x, "h": 1, "r": color[0], "g": color[1], "b": color[2]})
            x = x2

    merged = []
    used = [False] * len(runs)
    for i, run in enumerate(runs):
        if used[i]:
            continue
        current = dict(run)
        used[i] = True
        changed = True
        while changed:
            changed = False
            for j, other in enumerate(runs):
                if used[j]:
                    continue
                if (
                    other["x"] == current["x"]
                    and other["w"] == current["w"]
                    and other["r"] == current["r"]
                    and other["g"] == current["g"]
                    and other["b"] == current["b"]
                    and other["y"] == current["y"] + current["h"]
                ):
                    current["h"] += other["h"]
                    used[j] = True
                    changed = True
        merged.append(current)

    return {
        "ok": True,
        "type": "image",
        "created_at": int(time.time()),
        "width": new_w,
        "height": new_h,
        "rects": merged,
        "rect_count": len(merged),
        "pixel_count": new_w * new_h,
        "max_w": max_w,
        "max_h": max_h,
        "color_step": color_step,
    }


def image_to_rects_safe(img: Image.Image, max_w: int, max_h: int, color_step: int) -> Dict[str, Any]:
    max_w = max(1, min(ABS_MAX_RES, int(max_w)))
    max_h = max(1, min(ABS_MAX_RES, int(max_h)))
    color_step = max(4, min(64, int(color_step)))
    last = None
    for _ in range(8):
        data = image_to_rects(img, max_w, max_h, color_step)
        last = data
        if data["rect_count"] <= MAX_RECTS:
            return data
        if max_w > 64 or max_h > 64:
            max_w = max(64, int(max_w * 0.82))
            max_h = max(64, int(max_h * 0.82))
        else:
            color_step = min(64, color_step * 2)
    if last is None:
        raise RuntimeError("Conversion failed")
    last["warning"] = "Auto reduced"
    return last


def save_latest_image(port: str, data: Dict[str, Any]) -> None:
    data["port"] = clean_port(port)
    atomic_write_json(image_json_file(port), data)


def load_cached_latest_image(port: str) -> Dict[str, Any]:
    path = image_json_file(port)
    if not os.path.exists(path):
        return {"ok": False, "error": "No image", "port": clean_port(port)}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_original_image_as_rects(port: str, max_w: int, max_h: int, color_step: int) -> Optional[Dict[str, Any]]:
    path = image_original_file(port)
    if not os.path.exists(path):
        return None
    with open(path, "rb") as f:
        raw = f.read()
    img = Image.open(BytesIO(raw))
    img.load()
    data = image_to_rects_safe(img, max_w, max_h, color_step)
    data["port"] = clean_port(port)
    data["dynamic"] = True
    return data


def clamp_video_size(max_w: int, max_h: int) -> Tuple[int, int]:
    max_w = max(8, min(ABS_MAX_RES, int(max_w)))
    max_h = max(8, min(ABS_MAX_RES, int(max_h)))
    pixels = max_w * max_h
    if pixels > VIDEO_MAX_PIXELS:
        scale = math.sqrt(VIDEO_MAX_PIXELS / pixels)
        max_w = max(8, int(max_w * scale))
        max_h = max(8, int(max_h * scale))
    return max_w, max_h


def resize_frame_to_target(img: Image.Image, max_w: int, max_h: int, target_size: Optional[Tuple[int, int]] = None) -> Image.Image:
    img = img.convert("RGBA")
    if target_size and target_size[0] > 0 and target_size[1] > 0:
        return img.resize((int(target_size[0]), int(target_size[1])), Image.Resampling.BILINEAR)
    w, h = img.size
    scale = min(max_w / max(w, 1), max_h / max(h, 1), 1.0)
    new_w = max(1, int(w * scale))
    new_h = max(1, int(h * scale))
    return img.resize((new_w, new_h), Image.Resampling.BILINEAR)


def frame_to_hex_pixels(img: Image.Image, max_w: int, max_h: int, color_step: int, target_size: Optional[Tuple[int, int]] = None) -> Tuple[int, int, str]:
    img = resize_frame_to_target(img, max_w, max_h, target_size)
    new_w, new_h = img.size
    pixels = img.load()
    chunks: List[str] = []
    for y in range(new_h):
        for x in range(new_w):
            r, g, b, a = pixels[x, y]
            if a <= ALPHA_LIMIT:
                r, g, b = 0, 0, 0
            else:
                r, g, b = quantize_color(r, g, b, color_step)
            chunks.append(f"{r:02X}{g:02X}{b:02X}")
    return new_w, new_h, "".join(chunks)


def is_probably_video(filename: str, mimetype: str) -> bool:
    ext = os.path.splitext(filename or "")[1].lower()
    return (mimetype or "").startswith("video/") or ext in {".mp4", ".mov", ".webm", ".mkv", ".avi", ".m4v", ".wmv"}


def pil_open(raw_or_path: Any) -> Image.Image:
    if isinstance(raw_or_path, (bytes, bytearray)):
        return Image.open(BytesIO(raw_or_path))
    return Image.open(raw_or_path)


def pil_is_animated(path: str) -> bool:
    try:
        img = Image.open(path)
        return bool(getattr(img, "is_animated", False) and getattr(img, "n_frames", 1) > 1)
    except Exception:
        return False


def get_pil_animated_info(path: str, fps: float, max_frames: int, max_w: int, max_h: int) -> Dict[str, Any]:
    img = Image.open(path)
    n_frames = int(getattr(img, "n_frames", 1))
    total_duration = 0.0
    for i in range(min(n_frames, 5000)):
        try:
            img.seek(i)
            total_duration += max(float(img.info.get("duration", 100) or 100) / 1000.0, 0.01)
        except Exception:
            break
    if total_duration <= 0:
        total_duration = n_frames / max(fps, 0.1)
    first = get_pil_animated_frame(path, 0, fps)
    first_resized = resize_frame_to_target(first, max_w, max_h)
    width, height = first_resized.size
    frame_count = max(1, min(max_frames, int(math.ceil(total_duration * fps))))
    return {"width": width, "height": height, "frame_count": frame_count, "source_fps": None, "duration": total_duration}


def get_pil_animated_frame(path: str, sampled_index: int, fps: float) -> Image.Image:
    img = Image.open(path)
    n_frames = int(getattr(img, "n_frames", 1))
    target_t = max(0.0, float(sampled_index) / max(fps, 0.1))
    current_t = 0.0
    last = None
    for i in range(n_frames):
        img.seek(i)
        last = img.convert("RGBA").copy()
        duration = max(float(img.info.get("duration", 100) or 100) / 1000.0, 0.01)
        if current_t + duration >= target_t:
            return last
        current_t += duration
    if last is None:
        img.seek(0)
        return img.convert("RGBA").copy()
    return last


def imageio_reader(path: str):
    import imageio.v2 as imageio  # type: ignore
    try:
        return imageio.get_reader(path, format="FFMPEG")
    except Exception:
        return imageio.get_reader(path)


def get_imageio_info(path: str, fps: float, max_frames: int, max_w: int, max_h: int) -> Dict[str, Any]:
    reader = imageio_reader(path)
    try:
        try:
            meta = reader.get_meta_data() or {}
        except Exception:
            meta = {}
        source_fps = float(meta.get("fps") or 0) or None
        duration = float(meta.get("duration") or 0) or None
        nframes_raw = meta.get("nframes") or meta.get("n_frames") or None
        nframes = None
        try:
            if nframes_raw and math.isfinite(float(nframes_raw)):
                nframes = int(nframes_raw)
        except Exception:
            nframes = None
        frame0 = reader.get_data(0)
        img0 = Image.fromarray(frame0).convert("RGBA")
        width, height = resize_frame_to_target(img0, max_w, max_h).size
        if duration and duration > 0:
            frame_count = int(math.ceil(duration * fps))
        elif nframes and source_fps:
            frame_count = int(math.ceil(nframes / max(source_fps / fps, 1)))
        else:
            frame_count = max_frames
        frame_count = max(1, min(max_frames, frame_count))
        return {"width": width, "height": height, "frame_count": frame_count, "source_fps": source_fps or 30.0, "duration": duration}
    finally:
        try:
            reader.close()
        except Exception:
            pass


def get_imageio_frame(path: str, sampled_index: int, fps: float) -> Image.Image:
    reader = imageio_reader(path)
    try:
        try:
            meta = reader.get_meta_data() or {}
            source_fps = float(meta.get("fps") or 30.0)
        except Exception:
            source_fps = 30.0
        source_index = max(0, int(round(float(sampled_index) * source_fps / max(fps, 0.1))))
        try:
            frame = reader.get_data(source_index)
            return Image.fromarray(frame).convert("RGBA")
        except Exception:
            last = None
            for i, frame in enumerate(reader):
                if i > source_index:
                    break
                last = frame
            if last is None:
                raise
            return Image.fromarray(last).convert("RGBA")
    finally:
        try:
            reader.close()
        except Exception:
            pass


def get_cv2_info(path: str, fps: float, max_frames: int, max_w: int, max_h: int) -> Dict[str, Any]:
    import cv2  # type: ignore
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        raise RuntimeError("cv2 could not open video")
    try:
        source_fps = float(cap.get(cv2.CAP_PROP_FPS) or 30.0)
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        ok, frame = cap.read()
        if not ok:
            raise RuntimeError("cv2 could not read first frame")
        frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        img0 = Image.fromarray(frame).convert("RGBA")
        width, height = resize_frame_to_target(img0, max_w, max_h).size
        if total > 0 and source_fps > 0:
            frame_count = int(math.ceil((total / source_fps) * fps))
        else:
            frame_count = max_frames
        frame_count = max(1, min(max_frames, frame_count))
        return {"width": width, "height": height, "frame_count": frame_count, "source_fps": source_fps, "duration": (total / source_fps) if total and source_fps else None}
    finally:
        cap.release()


def get_cv2_frame(path: str, sampled_index: int, fps: float) -> Image.Image:
    import cv2  # type: ignore
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        raise RuntimeError("cv2 could not open video")
    try:
        source_fps = float(cap.get(cv2.CAP_PROP_FPS) or 30.0)
        source_index = max(0, int(round(float(sampled_index) * source_fps / max(fps, 0.1))))
        cap.set(cv2.CAP_PROP_POS_FRAMES, source_index)
        ok, frame = cap.read()
        if not ok:
            cap.set(cv2.CAP_PROP_POS_FRAMES, max(0, source_index - 1))
            ok, frame = cap.read()
        if not ok:
            raise RuntimeError("cv2 could not read requested frame")
        frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        return Image.fromarray(frame).convert("RGBA")
    finally:
        cap.release()


def get_video_info(port: str, max_w: int, max_h: int, color_step: int, fps: float, max_frames: int) -> Dict[str, Any]:
    original = find_video_original(port)
    if not original:
        return {"ok": False, "error": "No video", "port": clean_port(port), "type": "video"}

    max_w, max_h = clamp_video_size(max_w, max_h)
    fps = max(0.25, min(MAX_VIDEO_FPS, float(fps)))
    max_frames = max(1, min(MAX_VIDEO_FRAMES, int(max_frames)))
    color_step = max(4, min(64, int(color_step)))

    base = {
        "ok": True,
        "type": "video",
        "port": clean_port(port),
        "created_at": int(os.path.getmtime(original)),
        "fps": fps,
        "max_frames": max_frames,
        "color_step": color_step,
        "source_file": os.path.basename(original),
        "lazy": True,
    }

    errors: List[str] = []
    try:
        if pil_is_animated(original):
            info = get_pil_animated_info(original, fps, max_frames, max_w, max_h)
            base.update(info)
            base["decoder"] = "pillow"
            base["pixel_count"] = int(base["width"] * base["height"])
            return base
    except Exception as e:
        errors.append("pillow: " + str(e))

    try:
        info = get_imageio_info(original, fps, max_frames, max_w, max_h)
        base.update(info)
        base["decoder"] = "imageio"
        base["pixel_count"] = int(base["width"] * base["height"])
        return base
    except Exception as e:
        errors.append("imageio: " + str(e))

    try:
        info = get_cv2_info(original, fps, max_frames, max_w, max_h)
        base.update(info)
        base["decoder"] = "cv2"
        base["pixel_count"] = int(base["width"] * base["height"])
        return base
    except Exception as e:
        errors.append("cv2: " + str(e))

    return {
        "ok": False,
        "type": "video",
        "port": clean_port(port),
        "error": "Could not read video. Install imageio + imageio-ffmpeg or upload GIF/WebP/APNG. " + " | ".join(errors[:3]),
    }


def save_video_frame_cache_from_image(port: str, index: int, img: Image.Image, info: Dict[str, Any], max_w: int, max_h: int, color_step: int, fps: float, max_frames: int, decoder: str) -> Dict[str, Any]:
    frame_count = max(1, int(info.get("frame_count") or 1))
    index = int(index) % frame_count
    max_w, max_h = clamp_video_size(max_w, max_h)
    fps = max(0.25, min(MAX_VIDEO_FPS, float(fps)))
    max_frames = max(1, min(MAX_VIDEO_FRAMES, int(max_frames)))
    color_step = max(4, min(64, int(color_step)))

    cache = video_frame_cache_file(port, index, max_w, max_h, color_step, fps, max_frames)
    cached = load_json_file(cache)
    if cached and cached.get("ok"):
        return cached

    target = (int(info.get("width") or 0), int(info.get("height") or 0))
    target = target if target[0] > 0 and target[1] > 0 else None
    w, h, pixels = frame_to_hex_pixels(img, max_w, max_h, color_step, target)

    data = {
        "ok": True,
        "type": "video_frame_cache",
        "port": clean_port(port),
        "index": index,
        "width": w,
        "height": h,
        "fps": fps,
        "frame_count": frame_count,
        "pixel_count": w * h,
        "color_step": color_step,
        "decoder": decoder,
        "pixels": pixels,
    }
    atomic_write_json(cache, data)
    return data


def prepare_video_frames_to_cache(port: str, info: Dict[str, Any], max_w: int, max_h: int, color_step: int, fps: float, max_frames: int, progress_cb=None) -> Dict[str, Any]:
    original = find_video_original(port)
    if not original:
        raise RuntimeError("No video")

    frame_count = max(1, int(info.get("frame_count") or 1))
    max_w, max_h = clamp_video_size(max_w, max_h)
    fps = max(0.25, min(MAX_VIDEO_FPS, float(fps)))
    max_frames = max(1, min(MAX_VIDEO_FRAMES, int(max_frames)))
    color_step = max(4, min(64, int(color_step)))
    decoder = str(info.get("decoder") or "")

    if progress_cb:
        progress_cb(0, frame_count, "starting")

    # Animated GIF/WebP/APNG. Pillow is reliable, but each sampled frame can be slow for huge GIFs;
    # cached output still makes playback fast after this prepare step.
    if decoder == "pillow" or pil_is_animated(original):
        for i in range(frame_count):
            img = get_pil_animated_frame(original, i, fps)
            save_video_frame_cache_from_image(port, i, img, info, max_w, max_h, color_step, fps, max_frames, "pillow")
            if progress_cb:
                progress_cb(i + 1, frame_count, "pillow")
        return {"decoder": "pillow", "frame_count": frame_count}

    errors: List[str] = []

    # Normal MP4/WebM/MOV. Keep one reader open while preparing all sampled frames.
    try:
        reader = imageio_reader(original)
        try:
            try:
                meta = reader.get_meta_data() or {}
                source_fps = float(meta.get("fps") or info.get("source_fps") or 30.0)
            except Exception:
                source_fps = float(info.get("source_fps") or 30.0)
            last_img: Optional[Image.Image] = None
            for i in range(frame_count):
                source_index = max(0, int(round(float(i) * source_fps / max(fps, 0.1))))
                try:
                    frame = reader.get_data(source_index)
                    last_img = Image.fromarray(frame).convert("RGBA")
                except Exception:
                    if last_img is None:
                        raise
                save_video_frame_cache_from_image(port, i, last_img, info, max_w, max_h, color_step, fps, max_frames, "imageio")
                if progress_cb:
                    progress_cb(i + 1, frame_count, "imageio")
            return {"decoder": "imageio", "frame_count": frame_count}
        finally:
            try:
                reader.close()
            except Exception:
                pass
    except Exception as e:
        errors.append("imageio: " + str(e))

    # Fallback for hosts where OpenCV is installed instead.
    try:
        import cv2  # type: ignore
        cap = cv2.VideoCapture(original)
        if not cap.isOpened():
            raise RuntimeError("cv2 could not open video")
        try:
            source_fps = float(cap.get(cv2.CAP_PROP_FPS) or info.get("source_fps") or 30.0)
            last_img = None
            for i in range(frame_count):
                source_index = max(0, int(round(float(i) * source_fps / max(fps, 0.1))))
                cap.set(cv2.CAP_PROP_POS_FRAMES, source_index)
                ok, frame = cap.read()
                if ok:
                    frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                    last_img = Image.fromarray(frame).convert("RGBA")
                elif last_img is None:
                    raise RuntimeError("cv2 could not read frame " + str(source_index))
                save_video_frame_cache_from_image(port, i, last_img, info, max_w, max_h, color_step, fps, max_frames, "cv2")
                if progress_cb:
                    progress_cb(i + 1, frame_count, "cv2")
            return {"decoder": "cv2", "frame_count": frame_count}
        finally:
            cap.release()
    except Exception as e:
        errors.append("cv2: " + str(e))

    raise RuntimeError("Could not prepare video: " + " | ".join(errors[:3]))


def start_video_prepare_job(port: str, max_w: int, max_h: int, color_step: int, fps: float, max_frames: int, force: bool = False) -> Dict[str, Any]:
    port = clean_port(port)
    max_w, max_h = clamp_video_size(max_w, max_h)
    fps = max(0.25, min(MAX_VIDEO_FPS, float(fps)))
    max_frames = max(1, min(MAX_VIDEO_FRAMES, int(max_frames)))
    color_step = max(4, min(64, int(color_step)))
    info = get_video_info(port, max_w, max_h, color_step, fps, max_frames)
    if info.get("ok") is not True:
        return {"ok": False, "type": "video_prepare", "port": port, "error": info.get("error", "Video not ready")}

    frame_count = max(1, int(info.get("frame_count") or 1))
    key = video_cache_job_key(port, max_w, max_h, color_step, fps, max_frames)

    if force:
        for path in glob.glob(os.path.join(VIDEO_CACHE_DIR, f"{key}_i*.json")):
            try:
                os.remove(path)
            except OSError:
                pass

    cached = count_cached_video_frames(port, frame_count, max_w, max_h, color_step, fps, max_frames)
    if cached >= frame_count and not force:
        return {
            "ok": True,
            "type": "video_prepare",
            "port": port,
            "ready": True,
            "status": "ready",
            "cached": cached,
            "frame_count": frame_count,
            "fps": fps,
            "width": info.get("width"),
            "height": info.get("height"),
        }

    with VIDEO_PREPARE_LOCK:
        job = VIDEO_PREPARE_JOBS.get(key)
        if job and job.get("status") in ("starting", "running"):
            return dict(job)

        job = {
            "ok": True,
            "type": "video_prepare",
            "port": port,
            "ready": False,
            "status": "starting",
            "cached": cached,
            "frame_count": frame_count,
            "fps": fps,
            "width": info.get("width"),
            "height": info.get("height"),
            "started_at": int(time.time()),
        }
        VIDEO_PREPARE_JOBS[key] = job

    def worker():
        def progress(done: int, total: int, decoder_name: str):
            with VIDEO_PREPARE_LOCK:
                current = VIDEO_PREPARE_JOBS.get(key, job)
                current.update({
                    "ok": True,
                    "type": "video_prepare",
                    "port": port,
                    "ready": done >= total,
                    "status": "ready" if done >= total else "running",
                    "cached": done,
                    "frame_count": total,
                    "decoder": decoder_name,
                    "updated_at": int(time.time()),
                })

        try:
            progress(cached, frame_count, str(info.get("decoder") or ""))
            result = prepare_video_frames_to_cache(port, info, max_w, max_h, color_step, fps, max_frames, progress)
            final_cached = count_cached_video_frames(port, frame_count, max_w, max_h, color_step, fps, max_frames)
            with VIDEO_PREPARE_LOCK:
                VIDEO_PREPARE_JOBS[key].update({
                    "ok": True,
                    "ready": final_cached >= frame_count,
                    "status": "ready" if final_cached >= frame_count else "partial",
                    "cached": final_cached,
                    "frame_count": frame_count,
                    "decoder": result.get("decoder"),
                    "finished_at": int(time.time()),
                })
        except Exception as e:
            traceback.print_exc()
            with VIDEO_PREPARE_LOCK:
                VIDEO_PREPARE_JOBS[key].update({
                    "ok": False,
                    "ready": False,
                    "status": "error",
                    "error": str(e),
                    "trace": traceback.format_exc()[-1200:],
                    "updated_at": int(time.time()),
                })

    t = threading.Thread(target=worker, daemon=True)
    t.start()

    with VIDEO_PREPARE_LOCK:
        return dict(VIDEO_PREPARE_JOBS[key])


def decode_video_frame(port: str, index: int, info: Dict[str, Any], max_w: int, max_h: int, color_step: int, fps: float, max_frames: int) -> Dict[str, Any]:
    if info.get("ok") is not True:
        return info
    original = find_video_original(port)
    if not original:
        return {"ok": False, "error": "No video", "port": clean_port(port), "type": "video_frame"}

    frame_count = max(1, int(info.get("frame_count") or 1))
    index = int(index) % frame_count
    max_w, max_h = clamp_video_size(max_w, max_h)
    fps = max(0.25, min(MAX_VIDEO_FPS, float(fps)))
    max_frames = max(1, min(MAX_VIDEO_FRAMES, int(max_frames)))
    color_step = max(4, min(64, int(color_step)))

    cache = video_frame_cache_file(port, index, max_w, max_h, color_step, fps, max_frames)
    cached = load_json_file(cache)
    if cached and cached.get("ok"):
        return cached

    errors: List[str] = []
    img: Optional[Image.Image] = None
    decoder = str(info.get("decoder") or "")

    try:
        if decoder == "pillow" or pil_is_animated(original):
            img = get_pil_animated_frame(original, index, fps)
            decoder = "pillow"
    except Exception as e:
        errors.append("pillow: " + str(e))
        img = None

    if img is None:
        try:
            img = get_imageio_frame(original, index, fps)
            decoder = "imageio"
        except Exception as e:
            errors.append("imageio: " + str(e))
            img = None

    if img is None:
        try:
            img = get_cv2_frame(original, index, fps)
            decoder = "cv2"
        except Exception as e:
            errors.append("cv2: " + str(e))
            img = None

    if img is None:
        return {
            "ok": False,
            "type": "video_frame",
            "port": clean_port(port),
            "error": "Could not decode frame: " + " | ".join(errors[:3]),
        }

    return save_video_frame_cache_from_image(port, index, img, info, max_w, max_h, color_step, fps, max_frames, decoder)



def decode_video_frames_batch(port: str, indices: List[int], info: Dict[str, Any], max_w: int, max_h: int, color_step: int, fps: float, max_frames: int) -> Dict[int, Dict[str, Any]]:
    """Decode a small batch with one decoder open, so Roblox does not need one HTTP+FFmpeg open per frame."""
    out: Dict[int, Dict[str, Any]] = {}
    if info.get("ok") is not True:
        return out
    original = find_video_original(port)
    if not original:
        return out

    frame_count = max(1, int(info.get("frame_count") or 1))
    max_w, max_h = clamp_video_size(max_w, max_h)
    fps = max(0.25, min(MAX_VIDEO_FPS, float(fps)))
    max_frames = max(1, min(MAX_VIDEO_FRAMES, int(max_frames)))
    color_step = max(4, min(64, int(color_step)))

    clean_indices: List[int] = []
    for idx in indices:
        idx = int(idx) % frame_count
        if idx in out:
            continue
        cache = video_frame_cache_file(port, idx, max_w, max_h, color_step, fps, max_frames)
        cached = load_json_file(cache)
        if cached and cached.get("ok"):
            out[idx] = cached
        else:
            clean_indices.append(idx)

    if not clean_indices:
        return out

    errors: List[str] = []
    decoder = str(info.get("decoder") or "")

    try:
        if decoder == "pillow" or pil_is_animated(original):
            for idx in clean_indices:
                img = get_pil_animated_frame(original, idx, fps)
                out[idx] = save_video_frame_cache_from_image(port, idx, img, info, max_w, max_h, color_step, fps, max_frames, "pillow")
            return out
    except Exception as e:
        errors.append("pillow: " + str(e))

    try:
        reader = imageio_reader(original)
        try:
            try:
                meta = reader.get_meta_data() or {}
                source_fps = float(meta.get("fps") or info.get("source_fps") or 30.0)
            except Exception:
                source_fps = float(info.get("source_fps") or 30.0)
            last_img: Optional[Image.Image] = None
            for idx in clean_indices:
                source_index = max(0, int(round(float(idx) * source_fps / max(fps, 0.1))))
                try:
                    frame = reader.get_data(source_index)
                    last_img = Image.fromarray(frame).convert("RGBA")
                except Exception:
                    if last_img is None:
                        raise
                out[idx] = save_video_frame_cache_from_image(port, idx, last_img, info, max_w, max_h, color_step, fps, max_frames, "imageio")
            return out
        finally:
            try:
                reader.close()
            except Exception:
                pass
    except Exception as e:
        errors.append("imageio: " + str(e))

    try:
        import cv2  # type: ignore
        cap = cv2.VideoCapture(original)
        if not cap.isOpened():
            raise RuntimeError("cv2 could not open video")
        try:
            source_fps = float(cap.get(cv2.CAP_PROP_FPS) or info.get("source_fps") or 30.0)
            last_img = None
            for idx in clean_indices:
                source_index = max(0, int(round(float(idx) * source_fps / max(fps, 0.1))))
                cap.set(cv2.CAP_PROP_POS_FRAMES, source_index)
                ok, frame = cap.read()
                if ok:
                    frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                    last_img = Image.fromarray(frame).convert("RGBA")
                elif last_img is None:
                    raise RuntimeError("cv2 could not read frame " + str(source_index))
                out[idx] = save_video_frame_cache_from_image(port, idx, last_img, info, max_w, max_h, color_step, fps, max_frames, "cv2")
            return out
        finally:
            cap.release()
    except Exception as e:
        errors.append("cv2: " + str(e))

    # Last safe fallback: decode one-by-one. It is slower, but should return a JSON error instead of 502.
    for idx in clean_indices:
        try:
            out[idx] = decode_video_frame(port, idx, info, max_w, max_h, color_step, fps, max_frames)
        except Exception as e:
            out[idx] = {"ok": False, "type": "video_frame", "port": clean_port(port), "index": idx, "error": "batch decode failed: " + str(e) + " | " + " | ".join(errors[:2])}
    return out

def video_frame_response(current: Dict[str, Any], previous: Optional[Dict[str, Any]], prev_index: Optional[int]) -> Dict[str, Any]:
    if current.get("ok") is not True:
        current["type"] = "video_frame"
        return current

    pixels = str(current.get("pixels") or "")
    total_pixels = int(current.get("pixel_count") or 0)
    out = {
        "ok": True,
        "type": "video_frame",
        "port": current.get("port"),
        "width": current.get("width"),
        "height": current.get("height"),
        "fps": current.get("fps"),
        "frame_count": current.get("frame_count"),
        "index": current.get("index"),
        "pixel_count": total_pixels,
        "color_step": current.get("color_step"),
        "skip_unchanged": True,
        "diff": previous is not None and previous.get("ok") is True,
    }

    if previous is None or previous.get("ok") is not True or int(previous.get("index") or -1) != int(prev_index if prev_index is not None else -2):
        out["full"] = True
        out["pixels"] = pixels
        out["change_count"] = total_pixels
        return out

    prev_pixels = str(previous.get("pixels") or "")
    if len(prev_pixels) != len(pixels):
        out["full"] = True
        out["pixels"] = pixels
        out["change_count"] = total_pixels
        return out

    changes = []
    for i in range(total_pixels):
        start = i * 6
        color = pixels[start:start + 6]
        if color != prev_pixels[start:start + 6]:
            changes.append([i + 1, color])

    if len(changes) > total_pixels * 0.82:
        out["full"] = True
        out["pixels"] = pixels
        out["change_count"] = total_pixels
    else:
        out["full"] = False
        out["changes"] = changes
        out["change_count"] = len(changes)
    return out


@app.route("/", methods=["GET"])
def index():
    port = request.args.get("port", "").strip()
    status = "<span class='bad'>No port</span>"
    if port:
        try:
            clean = clean_port(port)
            parts = []
            image_data = load_cached_latest_image(clean)
            if image_data.get("ok"):
                parts.append(
                    f"<span class='ok'>Image ready: {clean}</span><br>"
                    f"{image_data.get('width')}x{image_data.get('height')} · Rects: {image_data.get('rect_count')}"
                )
            video_path = find_video_original(clean)
            if video_path:
                parts.append(
                    f"<span class='ok'>Video saved: {clean}</span><br>"
                    f"{os.path.basename(video_path)} · open Roblox script and press Video Preview / Build Canvas"
                )
            status = "<br><br>".join(parts) if parts else "<span class='bad'>No image/video on this port</span>"
        except Exception as e:
            status = f"<span class='bad'>{e}</span>"
    return render_template_string(HTML, status=status, port=port, show_key=bool(IMAGE_KEY), default_fps=DEFAULT_VIDEO_FPS, max_frames=MAX_VIDEO_FRAMES)


@app.route("/upload", methods=["POST"])
def upload():
    if not check_key(request):
        return json_error("Bad key", 403)

    uploaded = request.files.get("image") or request.files.get("file") or request.files.get("video")
    if not uploaded:
        return json_error("No file", 400)

    port = clean_port(request.form.get("port", ""))
    raw = uploaded.read()
    filename = uploaded.filename or "upload.bin"
    mimetype = uploaded.mimetype or ""
    if not raw:
        return json_error("Empty file", 400)

    should_video = is_probably_video(filename, mimetype)
    if not should_video:
        try:
            tmp = Image.open(BytesIO(raw))
            should_video = bool(getattr(tmp, "is_animated", False) and getattr(tmp, "n_frames", 1) > 1)
        except Exception:
            should_video = False

    if should_video:
        for old in glob.glob(os.path.join(VIDEO_ORIGINAL_DIR, f"{port}.*")) + glob.glob(os.path.join(VIDEO_CACHE_DIR, f"{port}_*.json")):
            try:
                os.remove(old)
            except OSError:
                pass
        path = video_original_file(port, filename)
        atomic_write(path, raw)
        info = {
            "ok": True,
            "type": "video_saved",
            "port": port,
            "filename": os.path.basename(path),
            "size_bytes": len(raw),
            "created_at": int(time.time()),
            "video_fps": read_float("video_fps", DEFAULT_VIDEO_FPS, 0.25, MAX_VIDEO_FPS),
            "max_frames": read_int("max_frames", MAX_VIDEO_FRAMES, 1, MAX_VIDEO_FRAMES),
        }
        atomic_write_json(video_info_file(port), info)
        return f"""
        <body style="background:#111;color:white;font-family:Arial;padding:24px">
            <h2>Video saved</h2>
            <p>Port: {port}</p>
            <p>Now open Roblox script and press <b>video preview</b> or <b>build canvas</b>.</p>
            <p>Frames will be prepared/cached before playback, so FPS is much faster.</p>
            <p><a style="color:#00aaff" href="/?port={port}">Back</a></p>
        </body>
        """

    atomic_write(image_original_file(port), raw)
    try:
        img = Image.open(BytesIO(raw))
        img.load()
        data = image_to_rects_safe(img, DEFAULT_RES, DEFAULT_RES, DEFAULT_COLOR_STEP)
        save_latest_image(port, data)
    except Exception as e:
        return json_error("Saved, conversion failed: " + str(e), 200, {"port": port, "type": "image"})

    return f"""
    <body style="background:#111;color:white;font-family:Arial;padding:24px">
        <h2>Image uploaded</h2>
        <p>Port: {port}</p>
        <p><a style="color:#00aaff" href="/?port={port}">Back</a></p>
    </body>
    """


@app.route("/latest", methods=["GET"])
def latest():
    if IMAGE_KEY and not check_key(request):
        return json_error("Bad key", 403)
    port = clean_port(request.args.get("port", ""))
    max_w = read_int("max_w", DEFAULT_RES, 8, ABS_MAX_RES)
    max_h = read_int("max_h", DEFAULT_RES, 8, ABS_MAX_RES)
    color_step = read_int("color_step", DEFAULT_COLOR_STEP, 4, 64)
    try:
        data = load_original_image_as_rects(port, max_w, max_h, color_step)
    except Exception as e:
        data = load_cached_latest_image(port)
        data["warning"] = "Cached"
        data["server_error"] = str(e)
    if data is None:
        data = load_cached_latest_image(port)
    return json_response(data)


@app.route("/video/meta", methods=["GET"])
@app.route("/video/latest", methods=["GET"])
def video_meta():
    if IMAGE_KEY and not check_key(request):
        return json_error("Bad key", 403)
    port = clean_port(request.args.get("port", ""))
    max_w = read_int("max_w", DEFAULT_VIDEO_RES, 8, ABS_MAX_RES)
    max_h = read_int("max_h", DEFAULT_VIDEO_RES, 8, ABS_MAX_RES)
    color_step = read_int("color_step", DEFAULT_COLOR_STEP, 4, 64)
    fps = read_float("fps", DEFAULT_VIDEO_FPS, 0.25, MAX_VIDEO_FPS)
    max_frames = read_int("max_frames", MAX_VIDEO_FRAMES, 1, MAX_VIDEO_FRAMES)
    data = get_video_info(port, max_w, max_h, color_step, fps, max_frames)
    return json_response(data)


@app.route("/video/prepare", methods=["GET", "POST"])
def video_prepare():
    # IMPORTANT: Render free instances can crash/502 if we decode/cache the whole video
    # in one background job. This endpoint is now only a lightweight status endpoint.
    # Roblox uses /video/frames to pull tiny batches instead.
    if IMAGE_KEY and not check_key(request):
        return json_error("Bad key", 403)
    port = clean_port(request.values.get("port", ""))
    max_w = read_int("max_w", DEFAULT_VIDEO_RES, 8, ABS_MAX_RES)
    max_h = read_int("max_h", DEFAULT_VIDEO_RES, 8, ABS_MAX_RES)
    color_step = read_int("color_step", DEFAULT_COLOR_STEP, 4, 64)
    fps = read_float("fps", DEFAULT_VIDEO_FPS, 0.25, MAX_VIDEO_FPS)
    max_frames = read_int("max_frames", MAX_VIDEO_FRAMES, 1, MAX_VIDEO_FRAMES)
    info = get_video_info(port, max_w, max_h, color_step, fps, max_frames)
    if info.get("ok") is not True:
        return json_response({"ok": False, "type": "video_prepare", "port": port, "error": info.get("error", "Video not ready")})
    frame_count = max(1, int(info.get("frame_count") or 1))
    cached = count_cached_video_frames(port, frame_count, *clamp_video_size(max_w, max_h), max(4, min(64, int(color_step))), max(0.25, min(MAX_VIDEO_FPS, float(fps))), max(1, min(MAX_VIDEO_FRAMES, int(max_frames))))
    return json_response({
        "ok": True,
        "type": "video_prepare",
        "port": port,
        "ready": False,
        "status": "streaming",
        "cached": cached,
        "frame_count": frame_count,
        "fps": info.get("fps", fps),
        "width": info.get("width"),
        "height": info.get("height"),
        "message": "Whole-video prepare is disabled to prevent Render 502/503. Use /video/frames streaming.",
    })


@app.route("/video/frame", methods=["GET"])
def video_frame():
    if IMAGE_KEY and not check_key(request):
        return json_error("Bad key", 403)
    port = clean_port(request.args.get("port", ""))
    max_w = read_int("max_w", DEFAULT_VIDEO_RES, 8, ABS_MAX_RES)
    max_h = read_int("max_h", DEFAULT_VIDEO_RES, 8, ABS_MAX_RES)
    color_step = read_int("color_step", DEFAULT_COLOR_STEP, 4, 64)
    fps = read_float("fps", DEFAULT_VIDEO_FPS, 0.25, MAX_VIDEO_FPS)
    max_frames = read_int("max_frames", MAX_VIDEO_FRAMES, 1, MAX_VIDEO_FRAMES)
    index = read_int("index", 0, 0, 1000000)
    prev_arg = request.args.get("prev")
    try:
        prev = None if prev_arg in (None, "", "nil", "none") else int(prev_arg)
    except Exception:
        prev = None

    info = get_video_info(port, max_w, max_h, color_step, fps, max_frames)
    if info.get("ok") is not True:
        return json_response({"ok": False, "type": "video_frame", "port": port, "error": info.get("error", "Video not ready")})

    current = decode_video_frame(port, index, info, max_w, max_h, color_step, fps, max_frames)
    previous = None
    if prev is not None and prev >= 0:
        previous = decode_video_frame(port, prev, info, max_w, max_h, color_step, fps, max_frames)
    return json_response(video_frame_response(current, previous, prev))



@app.route("/video/frames", methods=["GET"])
def video_frames():
    if IMAGE_KEY and not check_key(request):
        return json_error("Bad key", 403)
    port = clean_port(request.args.get("port", ""))
    max_w = read_int("max_w", DEFAULT_VIDEO_RES, 8, ABS_MAX_RES)
    max_h = read_int("max_h", DEFAULT_VIDEO_RES, 8, ABS_MAX_RES)
    color_step = read_int("color_step", DEFAULT_COLOR_STEP, 4, 64)
    fps = read_float("fps", DEFAULT_VIDEO_FPS, 0.25, MAX_VIDEO_FPS)
    max_frames = read_int("max_frames", MAX_VIDEO_FRAMES, 1, MAX_VIDEO_FRAMES)
    start = read_int("start", 0, 0, 1000000)
    count = read_int("count", 2, 1, 3)  # tiny batches only: protects Render from 502/503
    prev_arg = request.args.get("prev")
    try:
        prev = None if prev_arg in (None, "", "nil", "none") else int(prev_arg)
    except Exception:
        prev = None

    info = get_video_info(port, max_w, max_h, color_step, fps, max_frames)
    if info.get("ok") is not True:
        return json_response({"ok": False, "type": "video_frames", "port": port, "error": info.get("error", "Video not ready")})

    frame_count = max(1, int(info.get("frame_count") or 1))
    indices = [(start + i) % frame_count for i in range(count)]
    needed = list(indices)
    if prev is not None and prev >= 0:
        needed.insert(0, prev % frame_count)

    decoded = decode_video_frames_batch(port, needed, info, max_w, max_h, color_step, fps, max_frames)
    frames: List[Dict[str, Any]] = []
    previous = decoded.get(prev % frame_count) if prev is not None and prev >= 0 else None
    prev_index = prev

    for idx in indices:
        current = decoded.get(idx)
        if not current:
            current = decode_video_frame(port, idx, info, max_w, max_h, color_step, fps, max_frames)
        response = video_frame_response(current, previous, prev_index)
        frames.append(response)
        if current.get("ok"):
            previous = current
            prev_index = idx

    return json_response({
        "ok": True,
        "type": "video_frames",
        "port": port,
        "start": start,
        "count": len(frames),
        "frame_count": frame_count,
        "fps": info.get("fps", fps),
        "width": info.get("width"),
        "height": info.get("height"),
        "streaming": True,
        "frames": frames,
    })

@app.route("/clear", methods=["POST", "GET"])
def clear():
    if IMAGE_KEY and not check_key(request):
        return json_error("Bad key", 403)
    port = clean_port(request.values.get("port", ""))
    paths = [image_json_file(port), image_original_file(port), video_info_file(port)]
    paths += glob.glob(os.path.join(VIDEO_ORIGINAL_DIR, f"{port}.*"))
    paths += glob.glob(os.path.join(VIDEO_CACHE_DIR, f"{port}_*.json"))
    for path in paths:
        if os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass
    return json_response({"ok": True, "message": "Cleared", "port": port})


@app.route("/ping", methods=["GET"])
def ping():
    return json_response({
        "ok": True,
        "time": int(time.time()),
        "service": "image-video-painter-stream-no502",
        "abs_max_res": ABS_MAX_RES,
        "max_rects": MAX_RECTS,
        "video": True,
        "lazy_video": True,
        "video_prepare_cache": False,
        "server_diff_skip": True,
        "batch_streaming": True,
        "default_video_fps": DEFAULT_VIDEO_FPS,
        "max_video_fps": MAX_VIDEO_FPS,
        "max_video_frames": MAX_VIDEO_FRAMES,
        "video_max_pixels": VIDEO_MAX_PIXELS,
    })


@app.route("/debug", methods=["GET"])
def debug():
    libs = {}
    try:
        import imageio  # type: ignore
        libs["imageio"] = getattr(imageio, "__version__", "installed")
    except Exception as e:
        libs["imageio"] = "missing: " + str(e)
    try:
        import imageio_ffmpeg  # type: ignore
        libs["imageio_ffmpeg"] = getattr(imageio_ffmpeg, "__version__", "installed")
    except Exception as e:
        libs["imageio_ffmpeg"] = "missing: " + str(e)
    try:
        import cv2  # type: ignore
        libs["cv2"] = getattr(cv2, "__version__", "installed")
    except Exception as e:
        libs["cv2"] = "missing: " + str(e)
    return json_response({"ok": True, "libs": libs, "time": int(time.time())})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)

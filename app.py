import os
import glob
import json
import math
import time
import traceback
from io import BytesIO
from typing import Dict, Any, List, Tuple, Optional, Iterable

from flask import Flask, request, jsonify, render_template_string
from PIL import Image, ImageFile

ImageFile.LOAD_TRUNCATED_IMAGES = True
Image.MAX_IMAGE_PIXELS = int(os.environ.get("MAX_IMAGE_PIXELS", "20000000"))

app = Flask(__name__)

IMAGE_KEY = os.environ.get("IMAGE_KEY", "").strip()
DATA_DIR = os.environ.get("DATA_DIR", "image_ports")
ORIGINAL_DIR = os.environ.get("ORIGINAL_DIR", "image_originals")
VIDEO_DIR = os.environ.get("VIDEO_DIR", "video_ports")
VIDEO_ORIGINAL_DIR = os.environ.get("VIDEO_ORIGINAL_DIR", "video_originals")

DEFAULT_RES = int(os.environ.get("DEFAULT_RES", "96"))
DEFAULT_COLOR_STEP = int(os.environ.get("DEFAULT_COLOR_STEP", "16"))
ABS_MAX_RES = int(os.environ.get("ABS_MAX_RES", "160"))
MAX_RECTS = int(os.environ.get("MAX_RECTS", "12000"))
ALPHA_LIMIT = int(os.environ.get("ALPHA_LIMIT", "35"))

DEFAULT_VIDEO_RES = int(os.environ.get("DEFAULT_VIDEO_RES", str(min(DEFAULT_RES, 96))))
DEFAULT_VIDEO_FPS = float(os.environ.get("DEFAULT_VIDEO_FPS", "2"))
MAX_VIDEO_FPS = float(os.environ.get("MAX_VIDEO_FPS", "8"))
MAX_VIDEO_FRAMES = int(os.environ.get("MAX_VIDEO_FRAMES", "120"))
VIDEO_MAX_PIXELS = int(os.environ.get("VIDEO_MAX_PIXELS", "9216"))

for path in (DATA_DIR, ORIGINAL_DIR, VIDEO_DIR, VIDEO_ORIGINAL_DIR):
    os.makedirs(path, exist_ok=True)

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
        Keep video quality low for Roblox playback. 2 FPS and 64-96px usually works best.
    </p>
</div>
</body>
</html>
"""


def json_error(message: str, status: int = 200, extra: Optional[Dict[str, Any]] = None):
    data = {"ok": False, "error": str(message)}
    if extra:
        data.update(extra)
    resp = jsonify(data)
    resp.status_code = status
    resp.headers["Cache-Control"] = "no-store, max-age=0"
    return resp


@app.errorhandler(Exception)
def handle_any_exception(e):
    traceback.print_exc()
    return json_error("Server error: " + str(e), 200)


def clean_port(value: str) -> str:
    value = "".join(ch for ch in str(value or "") if ch.isdigit())
    if len(value) < 3:
        raise ValueError("Bad port")
    return value[:8]


def clean_number_token(value: Any, default: str) -> str:
    raw = str(value if value is not None else default)
    out = []
    for ch in raw:
        if ch.isdigit() or ch in ".-":
            out.append(ch)
    return "".join(out) or str(default)


def port_file(port: str) -> str:
    return os.path.join(DATA_DIR, f"{clean_port(port)}.json")


def original_file(port: str) -> str:
    return os.path.join(ORIGINAL_DIR, f"{clean_port(port)}.bin")


def video_original_file(port: str, filename: str = "") -> str:
    ext = os.path.splitext(filename or "video.bin")[1].lower()
    if not ext or len(ext) > 8:
        ext = ".bin"
    return os.path.join(VIDEO_ORIGINAL_DIR, f"{clean_port(port)}{ext}")


def find_video_original(port: str) -> Optional[str]:
    matches = glob.glob(os.path.join(VIDEO_ORIGINAL_DIR, f"{clean_port(port)}.*"))
    return matches[0] if matches else None


def video_cache_file(port: str, max_w: int, max_h: int, color_step: int, fps: float, max_frames: int) -> str:
    fps_token = clean_number_token(round(float(fps), 3), "2").replace(".", "p")
    return os.path.join(
        VIDEO_DIR,
        f"{clean_port(port)}_{int(max_w)}x{int(max_h)}_c{int(color_step)}_f{fps_token}_m{int(max_frames)}.json",
    )


def atomic_write(path: str, data: bytes) -> None:
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def atomic_write_json(path: str, data: Dict[str, Any]) -> None:
    atomic_write(path, json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


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
    return (
        quantize_channel(r, step),
        quantize_channel(g, step),
        quantize_channel(b, step),
    )


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

            runs.append({
                "x": x,
                "y": y,
                "w": x2 - x,
                "h": 1,
                "r": color[0],
                "g": color[1],
                "b": color[2],
            })
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


def save_latest(port: str, data: Dict[str, Any]) -> None:
    data["port"] = clean_port(port)
    atomic_write_json(port_file(port), data)


def save_original(port: str, raw: bytes) -> None:
    atomic_write(original_file(port), raw)


def load_cached_latest(port: str) -> Dict[str, Any]:
    path = port_file(port)
    if not os.path.exists(path):
        return {"ok": False, "error": "No image", "port": port}

    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_original_as_rects(port: str, max_w: int, max_h: int, color_step: int) -> Optional[Dict[str, Any]]:
    path = original_file(port)
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


def frame_to_hex_pixels(img: Image.Image, max_w: int, max_h: int, color_step: int) -> Tuple[int, int, str]:
    img = img.convert("RGBA")
    w, h = img.size
    scale = min(max_w / max(w, 1), max_h / max(h, 1), 1.0)
    new_w = max(1, int(w * scale))
    new_h = max(1, int(h * scale))
    img = img.resize((new_w, new_h), Image.Resampling.BILINEAR)
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
    return (mimetype or "").startswith("video/") or ext in {
        ".mp4", ".mov", ".webm", ".mkv", ".avi", ".m4v", ".wmv"
    }


def pil_is_animated_image(raw: bytes) -> bool:
    try:
        img = Image.open(BytesIO(raw))
        return bool(getattr(img, "is_animated", False) and getattr(img, "n_frames", 1) > 1)
    except Exception:
        return False


def extract_frames_from_animated_pil(raw: bytes, fps: float, max_frames: int) -> List[Image.Image]:
    img = Image.open(BytesIO(raw))
    frames: List[Image.Image] = []
    interval = 1.0 / max(fps, 0.1)
    current_t = 0.0
    next_t = 0.0
    n_frames = int(getattr(img, "n_frames", 1))

    for i in range(n_frames):
        img.seek(i)
        duration = float(img.info.get("duration", 100) or 100) / 1000.0
        if current_t + 1e-6 >= next_t or i == 0:
            frames.append(img.convert("RGBA").copy())
            next_t += interval
            if len(frames) >= max_frames:
                break
        current_t += max(duration, 0.01)

    if not frames:
        img.seek(0)
        frames.append(img.convert("RGBA").copy())
    return frames


def extract_frames_with_imageio(path: str, fps: float, max_frames: int) -> List[Image.Image]:
    import imageio.v2 as imageio  # type: ignore

    try:
        reader = imageio.get_reader(path, format="FFMPEG")
    except TypeError:
        reader = imageio.get_reader(path)
    except Exception:
        reader = imageio.get_reader(path)

    try:
        meta = reader.get_meta_data() or {}
        source_fps = float(meta.get("fps") or 0)
    except Exception:
        source_fps = 0.0

    step = max(1, int(round((source_fps or 30.0) / max(fps, 0.1))))
    frames: List[Image.Image] = []

    try:
        for i, frame in enumerate(reader):
            if i % step != 0:
                continue
            frames.append(Image.fromarray(frame).convert("RGBA"))
            if len(frames) >= max_frames:
                break
    finally:
        try:
            reader.close()
        except Exception:
            pass

    return frames


def extract_frames_with_cv2(path: str, fps: float, max_frames: int) -> List[Image.Image]:
    import cv2  # type: ignore

    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        return []

    source_fps = float(cap.get(cv2.CAP_PROP_FPS) or 30.0)
    step = max(1, int(round(source_fps / max(fps, 0.1))))
    frames: List[Image.Image] = []
    idx = 0

    try:
        while len(frames) < max_frames:
            ok, frame = cap.read()
            if not ok:
                break
            if idx % step == 0:
                frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                frames.append(Image.fromarray(frame).convert("RGBA"))
            idx += 1
    finally:
        cap.release()

    return frames


def extract_video_frames(path: str, raw: bytes, filename: str, mimetype: str, fps: float, max_frames: int) -> List[Image.Image]:
    if pil_is_animated_image(raw):
        return extract_frames_from_animated_pil(raw, fps, max_frames)

    errors: List[str] = []
    try:
        frames = extract_frames_with_imageio(path, fps, max_frames)
        if frames:
            return frames
    except Exception as e:
        errors.append("imageio: " + str(e))

    try:
        frames = extract_frames_with_cv2(path, fps, max_frames)
        if frames:
            return frames
    except Exception as e:
        errors.append("cv2: " + str(e))

    raise RuntimeError(
        "Could not read video. Use GIF/WebP/APNG or install imageio-ffmpeg / opencv-python-headless. "
        + " | ".join(errors[:2])
    )


def build_video_data_from_frames(
    port: str,
    frames: List[Image.Image],
    max_w: int,
    max_h: int,
    color_step: int,
    fps: float,
    max_frames: int,
) -> Dict[str, Any]:
    if not frames:
        raise RuntimeError("No video frames")

    max_w, max_h = clamp_video_size(max_w, max_h)
    color_step = max(4, min(64, int(color_step)))
    fps = max(0.25, min(MAX_VIDEO_FPS, float(fps)))
    max_frames = max(1, min(MAX_VIDEO_FRAMES, int(max_frames)))

    converted = []
    final_w = final_h = None

    for img in frames[:max_frames]:
        w, h, pixels = frame_to_hex_pixels(img, max_w, max_h, color_step)
        if final_w is None:
            final_w, final_h = w, h
        elif w != final_w or h != final_h:
            fixed = img.convert("RGBA").resize((final_w, final_h), Image.Resampling.BILINEAR)
            w, h, pixels = frame_to_hex_pixels(fixed, final_w, final_h, color_step)
        converted.append({"pixels": pixels})

    return {
        "ok": True,
        "type": "video",
        "created_at": int(time.time()),
        "port": clean_port(port),
        "width": int(final_w or 1),
        "height": int(final_h or 1),
        "fps": fps,
        "frame_count": len(converted),
        "max_frames": max_frames,
        "color_step": color_step,
        "pixel_count": int((final_w or 1) * (final_h or 1)),
        "frames": converted,
    }


def load_or_make_video_data(port: str, max_w: int, max_h: int, color_step: int, fps: float, max_frames: int) -> Dict[str, Any]:
    max_w, max_h = clamp_video_size(max_w, max_h)
    color_step = max(4, min(64, int(color_step)))
    fps = max(0.25, min(MAX_VIDEO_FPS, float(fps)))
    max_frames = max(1, min(MAX_VIDEO_FRAMES, int(max_frames)))

    cache_path = video_cache_file(port, max_w, max_h, color_step, fps, max_frames)
    if os.path.exists(cache_path):
        with open(cache_path, "r", encoding="utf-8") as f:
            return json.load(f)

    original = find_video_original(port)
    if not original:
        return {"ok": False, "error": "No video", "port": clean_port(port)}

    with open(original, "rb") as f:
        raw = f.read()

    frames = extract_video_frames(original, raw, os.path.basename(original), "", fps, max_frames)
    data = build_video_data_from_frames(port, frames, max_w, max_h, color_step, fps, max_frames)
    atomic_write_json(cache_path, data)
    return data


def video_meta_response(data: Dict[str, Any]) -> Dict[str, Any]:
    if data.get("ok") is not True:
        return data
    return {
        "ok": True,
        "type": "video",
        "port": data.get("port"),
        "width": data.get("width"),
        "height": data.get("height"),
        "fps": data.get("fps"),
        "frame_count": data.get("frame_count"),
        "max_frames": data.get("max_frames"),
        "color_step": data.get("color_step"),
        "pixel_count": data.get("pixel_count"),
        "created_at": data.get("created_at"),
    }


def video_frame_response(data: Dict[str, Any], index: int, prev: Optional[int]) -> Dict[str, Any]:
    if data.get("ok") is not True:
        return data

    frames = data.get("frames") or []
    count = len(frames)
    if count <= 0:
        return {"ok": False, "error": "No video frames", "port": data.get("port")}

    index = int(index) % count
    pixels = str(frames[index].get("pixels") or "")
    total_pixels = int(data.get("pixel_count") or 0)

    out = {
        "ok": True,
        "type": "video_frame",
        "port": data.get("port"),
        "width": data.get("width"),
        "height": data.get("height"),
        "fps": data.get("fps"),
        "frame_count": count,
        "index": index,
        "pixel_count": total_pixels,
        "color_step": data.get("color_step"),
    }

    if prev is None or prev < 0 or prev >= count:
        out["full"] = True
        out["pixels"] = pixels
        out["change_count"] = total_pixels
        return out

    prev_pixels = str(frames[int(prev)].get("pixels") or "")
    changes = []
    for i in range(total_pixels):
        a = i * 6
        color = pixels[a:a + 6]
        if color != prev_pixels[a:a + 6]:
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
            image_data = load_cached_latest(clean)
            video_path = find_video_original(clean)
            parts = []
            if image_data.get("ok"):
                parts.append(
                    f"<span class='ok'>Image ready: {image_data.get('port')}</span><br>"
                    f"{image_data.get('width')}x{image_data.get('height')} · Rects: {image_data.get('rect_count')}"
                )
            if video_path:
                try:
                    vdata = load_or_make_video_data(clean, DEFAULT_VIDEO_RES, DEFAULT_VIDEO_RES, DEFAULT_COLOR_STEP, DEFAULT_VIDEO_FPS, MAX_VIDEO_FRAMES)
                    if vdata.get("ok"):
                        parts.append(
                            f"<span class='ok'>Video ready: {clean}</span><br>"
                            f"{vdata.get('width')}x{vdata.get('height')} · Frames: {vdata.get('frame_count')} · FPS: {vdata.get('fps')}"
                        )
                except Exception as e:
                    parts.append(f"<span class='bad'>Video saved, conversion error: {e}</span>")
            if parts:
                status = "<br><br>".join(parts)
            else:
                status = "<span class='bad'>No image/video on this port</span>"
        except Exception as e:
            status = f"<span class='bad'>{e}</span>"

    return render_template_string(
        HTML,
        status=status,
        port=port,
        show_key=bool(IMAGE_KEY),
        default_fps=DEFAULT_VIDEO_FPS,
        max_frames=MAX_VIDEO_FRAMES,
    )


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

    should_video = is_probably_video(filename, mimetype) or pil_is_animated_image(raw)

    if should_video:
        fps = read_float("video_fps", DEFAULT_VIDEO_FPS, 0.25, MAX_VIDEO_FPS)
        max_frames = read_int("max_frames", MAX_VIDEO_FRAMES, 1, MAX_VIDEO_FRAMES)
        max_w, max_h = clamp_video_size(DEFAULT_VIDEO_RES, DEFAULT_VIDEO_RES)
        path = video_original_file(port, filename)

        for old in glob.glob(os.path.join(VIDEO_ORIGINAL_DIR, f"{port}.*")):
            try:
                os.remove(old)
            except OSError:
                pass
        for old in glob.glob(os.path.join(VIDEO_DIR, f"{port}_*.json")):
            try:
                os.remove(old)
            except OSError:
                pass

        atomic_write(path, raw)
        try:
            frames = extract_video_frames(path, raw, filename, mimetype, fps, max_frames)
            data = build_video_data_from_frames(port, frames, max_w, max_h, DEFAULT_COLOR_STEP, fps, max_frames)
            atomic_write_json(video_cache_file(port, max_w, max_h, DEFAULT_COLOR_STEP, fps, max_frames), data)
        except Exception as e:
            return json_error("Video saved, conversion failed: " + str(e), 200, {"port": port, "type": "video"})

        return f"""
        <body style="background:#111;color:white;font-family:Arial;padding:24px">
            <h2>Video uploaded</h2>
            <p>Port: {port}</p>
            <p>Frames: {data.get('frame_count')} · Size: {data.get('width')}x{data.get('height')} · FPS: {data.get('fps')}</p>
            <p><a style="color:#00aaff" href="/?port={port}">Back</a></p>
        </body>
        """

    save_original(port, raw)
    try:
        img = Image.open(BytesIO(raw))
        img.load()
        save_latest(port, image_to_rects_safe(img, DEFAULT_RES, DEFAULT_RES, DEFAULT_COLOR_STEP))
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
        data = load_original_as_rects(port, max_w, max_h, color_step)
    except Exception as e:
        data = load_cached_latest(port)
        data["warning"] = "Cached"
        data["server_error"] = str(e)

    if data is None:
        data = load_cached_latest(port)

    resp = jsonify(data)
    resp.headers["Cache-Control"] = "no-store, max-age=0"
    return resp


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

    data = load_or_make_video_data(port, max_w, max_h, color_step, fps, max_frames)
    resp = jsonify(video_meta_response(data))
    resp.headers["Cache-Control"] = "no-store, max-age=0"
    return resp


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

    prev_arg = request.args.get("prev", None)
    try:
        prev = None if prev_arg in (None, "", "nil", "none") else int(prev_arg)
    except Exception:
        prev = None

    data = load_or_make_video_data(port, max_w, max_h, color_step, fps, max_frames)
    resp = jsonify(video_frame_response(data, index, prev))
    resp.headers["Cache-Control"] = "no-store, max-age=0"
    return resp


@app.route("/clear", methods=["POST", "GET"])
def clear():
    if IMAGE_KEY and not check_key(request):
        return json_error("Bad key", 403)

    port = clean_port(request.values.get("port", ""))
    paths = [port_file(port), original_file(port)]
    paths += glob.glob(os.path.join(VIDEO_ORIGINAL_DIR, f"{port}.*"))
    paths += glob.glob(os.path.join(VIDEO_DIR, f"{port}_*.json"))

    for path in paths:
        if os.path.exists(path):
            os.remove(path)

    return jsonify({"ok": True, "message": "Cleared", "port": port})


@app.route("/ping", methods=["GET"])
def ping():
    return jsonify({
        "ok": True,
        "time": int(time.time()),
        "service": "image-video-painter-minimal",
        "abs_max_res": ABS_MAX_RES,
        "max_rects": MAX_RECTS,
        "video": True,
        "default_video_fps": DEFAULT_VIDEO_FPS,
        "max_video_fps": MAX_VIDEO_FPS,
        "max_video_frames": MAX_VIDEO_FRAMES,
        "video_max_pixels": VIDEO_MAX_PIXELS,
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)

import os
import json
import time
import traceback
from io import BytesIO
from typing import Dict, Any, List, Tuple, Optional

from flask import Flask, request, jsonify, render_template_string
from PIL import Image, ImageFile

ImageFile.LOAD_TRUNCATED_IMAGES = True
Image.MAX_IMAGE_PIXELS = int(os.environ.get("MAX_IMAGE_PIXELS", "20000000"))

app = Flask(__name__)

IMAGE_KEY = os.environ.get("IMAGE_KEY", "").strip()
DATA_DIR = os.environ.get("DATA_DIR", "image_ports")
ORIGINAL_DIR = os.environ.get("ORIGINAL_DIR", "image_originals")

DEFAULT_RES = int(os.environ.get("DEFAULT_RES", "96"))
DEFAULT_COLOR_STEP = int(os.environ.get("DEFAULT_COLOR_STEP", "16"))
ABS_MAX_RES = int(os.environ.get("ABS_MAX_RES", "160"))
MAX_RECTS = int(os.environ.get("MAX_RECTS", "12000"))
ALPHA_LIMIT = int(os.environ.get("ALPHA_LIMIT", "35"))

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(ORIGINAL_DIR, exist_ok=True)

HTML = """
<!doctype html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <title>Image Painter</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        body { background:#111; color:white; font-family:Arial,sans-serif; padding:24px; }
        .box { max-width:520px; margin:auto; background:#1d1d1d; border:1px solid #333; border-radius:12px; padding:18px; }
        h2 { margin-top:0; }
        input, button { width:100%; margin-top:10px; padding:10px; border-radius:8px; border:0; font-size:15px; box-sizing:border-box; }
        button { background:#00aaff; color:white; font-weight:bold; cursor:pointer; }
        .hint { color:#aaa; font-size:13px; }
        code { background:#000; padding:2px 5px; border-radius:4px; }
        .ok { color:#00ff99; }
        .bad { color:#ff7777; }
    </style>
</head>
<body>
<div class="box">
    <h2>Image Painter</h2>

    <form action="/upload" method="post" enctype="multipart/form-data">
        <input name="port" value="{{ port }}" placeholder="Port" required>

        {% if show_key %}
        <input name="key" placeholder="Key">
        {% endif %}

        <input type="file" name="image" accept="image/*" required>
        <button type="submit">Upload</button>
    </form>

    <p>Status: {{ status|safe }}</p>
    <p class="hint"><code>/ping</code> · <code>/latest?port=PORT</code></p>
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


def port_file(port: str) -> str:
    return os.path.join(DATA_DIR, f"{clean_port(port)}.json")


def original_file(port: str) -> str:
    return os.path.join(ORIGINAL_DIR, f"{clean_port(port)}.bin")


def atomic_write(path: str, data: bytes) -> None:
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def atomic_write_json(path: str, data: Dict[str, Any]) -> None:
    atomic_write(path, json.dumps(data, ensure_ascii=False).encode("utf-8"))


def check_key(req) -> bool:
    if not IMAGE_KEY:
        return True
    return (req.values.get("key", "") or req.headers.get("X-Image-Key", "")) == IMAGE_KEY


def read_int(name: str, default: int, lo: int, hi: int) -> int:
    try:
        value = int(request.values.get(name, default))
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


@app.route("/", methods=["GET"])
def index():
    port = request.args.get("port", "").strip()
    status = "<span class='bad'>No port</span>"

    if port:
        try:
            data = load_cached_latest(clean_port(port))
            if data.get("ok"):
                status = (
                    f"<span class='ok'>Ready: {data.get('port')}</span><br>"
                    f"{data.get('width')}x{data.get('height')}<br>"
                    f"Rects: {data.get('rect_count')}"
                )
            else:
                status = f"<span class='bad'>{data.get('error')}</span>"
        except Exception as e:
            status = f"<span class='bad'>{e}</span>"

    return render_template_string(HTML, status=status, port=port, show_key=bool(IMAGE_KEY))


@app.route("/upload", methods=["POST"])
def upload():
    if not check_key(request):
        return json_error("Bad key", 403)

    if "image" not in request.files:
        return json_error("No image", 400)

    port = clean_port(request.form.get("port", ""))
    raw = request.files["image"].read()

    if not raw:
        return json_error("Empty image", 400)

    save_original(port, raw)

    try:
        img = Image.open(BytesIO(raw))
        img.load()
        save_latest(port, image_to_rects_safe(img, DEFAULT_RES, DEFAULT_RES, DEFAULT_COLOR_STEP))
    except Exception as e:
        return json_error("Saved, conversion failed: " + str(e), 200, {"port": port})

    return f"""
    <body style="background:#111;color:white;font-family:Arial;padding:24px">
        <h2>Uploaded</h2>
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


@app.route("/clear", methods=["POST", "GET"])
def clear():
    if IMAGE_KEY and not check_key(request):
        return json_error("Bad key", 403)

    port = clean_port(request.values.get("port", ""))
    for path in (port_file(port), original_file(port)):
        if os.path.exists(path):
            os.remove(path)

    return jsonify({"ok": True, "message": "Cleared", "port": port})


@app.route("/ping", methods=["GET"])
def ping():
    return jsonify({
        "ok": True,
        "time": int(time.time()),
        "service": "image-painter-minimal",
        "abs_max_res": ABS_MAX_RES,
        "max_rects": MAX_RECTS,
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)

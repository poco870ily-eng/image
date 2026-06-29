import os
import json
import time
from io import BytesIO
from typing import Dict, Any, List, Tuple, Optional

from flask import Flask, request, jsonify, render_template_string
from PIL import Image

app = Flask(__name__)

IMAGE_KEY = os.environ.get("IMAGE_KEY", "").strip()
DATA_DIR = os.environ.get("DATA_DIR", "image_ports")
ORIGINAL_DIR = os.environ.get("ORIGINAL_DIR", "image_originals")
MAX_W = int(os.environ.get("MAX_W", "96"))
MAX_H = int(os.environ.get("MAX_H", "96"))
ALPHA_LIMIT = int(os.environ.get("ALPHA_LIMIT", "35"))
COLOR_STEP = int(os.environ.get("COLOR_STEP", "16"))

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(ORIGINAL_DIR, exist_ok=True)

HTML = """
<!doctype html>
<html lang="ru">
<head>
    <meta charset="utf-8">
    <title>BABFT Image Painter</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        body { background:#111; color:white; font-family:Arial,sans-serif; padding:24px; }
        .box { max-width:760px; margin:auto; background:#1d1d1d; border:1px solid #333; border-radius:12px; padding:18px; }
        input, button { width:100%; margin-top:10px; padding:10px; border-radius:8px; border:0; font-size:15px; box-sizing:border-box; }
        button { background:#00aaff; color:white; font-weight:bold; cursor:pointer; }
        .hint { color:#aaa; font-size:13px; line-height:1.45; }
        code { background:#000; padding:2px 5px; border-radius:4px; }
        .ok { color:#00ff99; }
        .bad { color:#ff7777; }
    </style>
</head>
<body>
<div class="box">
    <h2>BABFT Image Painter</h2>
    <p class="hint">
        В Roblox открой вкладку <b>Image</b>, скопируй <b>Port</b> и вставь его сюда.<br>
        Здесь нужно только <b>ввести port</b> и <b>выбрать картинку</b>.<br>
        Размер, чёткость и color step настраиваются уже в самом скрипте.
    </p>
    <form action="/upload" method="post" enctype="multipart/form-data">
        <label>Port из Roblox-скрипта:</label>
        <input name="port" value="{{ port }}" placeholder="Например 54321" required>

        {% if show_key %}
        <label>Secret key:</label>
        <input name="key" placeholder="Если используешь IMAGE_KEY">
        {% endif %}

        <label>Картинка:</label>
        <input type="file" name="image" accept="image/*" required>

        <button type="submit">Загрузить в этот порт</button>
    </form>

    <hr>
    <p>Status: {{ status|safe }}</p>
    <p class="hint">
        Проверка сервера: <code>/ping</code><br>
        Для Roblox: <code>/latest?port=ТВОЙ_ПОРТ&amp;max_w=96&amp;max_h=96&amp;color_step=16</code>
    </p>
</div>
</body>
</html>
"""


def clean_port(value: str) -> str:
    value = "".join(ch for ch in str(value or "") if ch.isdigit())
    if len(value) < 3:
        raise ValueError("Bad port: use at least 3 digits")
    if len(value) > 8:
        value = value[:8]
    return value


def port_file(port: str) -> str:
    return os.path.join(DATA_DIR, f"{clean_port(port)}.json")


def original_file(port: str) -> str:
    return os.path.join(ORIGINAL_DIR, f"{clean_port(port)}.bin")


def check_key(req) -> bool:
    if not IMAGE_KEY:
        return True
    key = req.values.get("key", "") or req.headers.get("X-Image-Key", "")
    return key == IMAGE_KEY


def read_int_from_values(name: str, default: int, lo: int, hi: int) -> int:
    raw = request.values.get(name, default)
    try:
        value = int(raw)
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
    scale = min(max_w / w, max_h / h, 1.0)
    new_w = max(1, int(w * scale))
    new_h = max(1, int(h * scale))
    img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
    pixels = img.load()

    grid: List[List[Any]] = []
    for y in range(new_h):
        row = []
        for x in range(new_w):
            r, g, b, a = pixels[x, y]
            if a <= ALPHA_LIMIT:
                row.append(None)
            else:
                row.append(quantize_color(r, g, b, color_step))
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
                    other["x"] == current["x"] and other["w"] == current["w"]
                    and other["r"] == current["r"] and other["g"] == current["g"] and other["b"] == current["b"]
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


def save_latest(port: str, data: Dict[str, Any]) -> None:
    data["port"] = clean_port(port)
    with open(port_file(port), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


def save_original(port: str, raw: bytes) -> None:
    with open(original_file(port), "wb") as f:
        f.write(raw)


def load_from_original(port: str, max_w: int, max_h: int, color_step: int) -> Optional[Dict[str, Any]]:
    path = original_file(port)
    if not os.path.exists(path):
        return None
    with open(path, "rb") as f:
        raw = f.read()
    img = Image.open(BytesIO(raw))
    data = image_to_rects(img, max_w, max_h, color_step)
    data["port"] = clean_port(port)
    data["dynamic"] = True
    return data


def load_cached_latest(port: str) -> Dict[str, Any]:
    path = port_file(port)
    if not os.path.exists(path):
        return {"ok": False, "error": f"No image uploaded for port {port} yet", "port": port}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


@app.route("/", methods=["GET"])
def index():
    port = request.args.get("port", "").strip()
    status = "<span class='bad'>Вставь port из Roblox и загрузи картинку</span>"
    if port:
        try:
            latest = load_cached_latest(clean_port(port))
            if latest.get("ok"):
                status = (
                    f"<span class='ok'>Есть картинка для порта {latest.get('port')}</span><br>"
                    f"Size: {latest.get('width')}x{latest.get('height')}<br>"
                    f"Rects: {latest.get('rect_count')}<br>"
                    f"Res/Size/Color меняются уже в Roblox скрипте."
                )
            else:
                status = f"<span class='bad'>{latest.get('error')}</span>"
        except Exception as e:
            status = f"<span class='bad'>{e}</span>"
    return render_template_string(HTML, status=status, port=port, show_key=bool(IMAGE_KEY))


@app.route("/upload", methods=["POST"])
def upload():
    if not check_key(request):
        return jsonify({"ok": False, "error": "Bad key"}), 403
    if "image" not in request.files:
        return jsonify({"ok": False, "error": "No image file"}), 400
    try:
        port = clean_port(request.form.get("port", ""))
        raw = request.files["image"].read()
        save_original(port, raw)

        img = Image.open(BytesIO(raw))
        data = image_to_rects(img, MAX_W, MAX_H, COLOR_STEP)
        save_latest(port, data)

        return f"""
        <body style="background:#111;color:white;font-family:Arial;padding:24px">
            <h2>Uploaded to port {port}!</h2>
            <p>Image saved.</p>
            <p>Теперь открой Roblox и жми Preview / Draw.</p>
            <p><a style="color:#00aaff" href="/?port={port}">Back</a></p>
        </body>
        """
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/latest", methods=["GET"])
def latest():
    if IMAGE_KEY and not check_key(request):
        return jsonify({"ok": False, "error": "Bad key"}), 403
    try:
        port = clean_port(request.args.get("port", ""))
        max_w = read_int_from_values("max_w", MAX_W, 1, 256)
        max_h = read_int_from_values("max_h", MAX_H, 1, 256)
        color_step = read_int_from_values("color_step", COLOR_STEP, 1, 64)

        data = None
        try:
            data = load_from_original(port, max_w, max_h, color_step)
        except Exception:
            data = None

        if data is None:
            data = load_cached_latest(port)

        resp = jsonify(data)
        resp.headers["Cache-Control"] = "no-store, max-age=0"
        return resp
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 200


@app.route("/clear", methods=["POST", "GET"])
def clear():
    if IMAGE_KEY and not check_key(request):
        return jsonify({"ok": False, "error": "Bad key"}), 403
    try:
        port = clean_port(request.values.get("port", ""))
        for path in (port_file(port), original_file(port)):
            if os.path.exists(path):
                os.remove(path)
        return jsonify({"ok": True, "message": "cleared", "port": port})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/ping", methods=["GET"])
def ping():
    return jsonify({"ok": True, "time": int(time.time()), "service": "babft-image-painter-port-v3"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)

import os
import json
import time
from io import BytesIO
from typing import Dict, Any, List, Tuple

from flask import Flask, request, jsonify, render_template_string
from PIL import Image

app = Flask(__name__)

IMAGE_KEY = os.environ.get("IMAGE_KEY", "").strip()
LATEST_FILE = "latest_image.json"

MAX_W = int(os.environ.get("MAX_W", "96"))
MAX_H = int(os.environ.get("MAX_H", "96"))
ALPHA_LIMIT = int(os.environ.get("ALPHA_LIMIT", "35"))
COLOR_STEP = int(os.environ.get("COLOR_STEP", "16"))


HTML = """
<!doctype html>
<html lang="ru">
<head>
    <meta charset="utf-8">
    <title>BABFT Image Painter</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        body {
            background: #111;
            color: white;
            font-family: Arial, sans-serif;
            padding: 24px;
        }
        .box {
            max-width: 720px;
            margin: auto;
            background: #1d1d1d;
            border: 1px solid #333;
            border-radius: 12px;
            padding: 18px;
        }
        input, button {
            width: 100%;
            margin-top: 10px;
            padding: 10px;
            border-radius: 8px;
            border: 0;
            font-size: 15px;
        }
        button {
            background: #00aaff;
            color: white;
            font-weight: bold;
            cursor: pointer;
        }
        .hint {
            color: #aaa;
            font-size: 13px;
            line-height: 1.4;
        }
        code {
            background: #000;
            padding: 2px 5px;
            border-radius: 4px;
        }
        .ok {
            color: #00ff99;
        }
        .bad {
            color: #ff7777;
        }
    </style>
</head>
<body>
<div class="box">
    <h2>BABFT Image Painter</h2>
    <p class="hint">
        Загрузи картинку, сервер превратит её в JSON для Roblox-скрипта.
        Потом Lua берёт данные с <code>/latest</code>.
    </p>

    <form action="/upload" method="post" enctype="multipart/form-data">
        <label>Secret key, если включён IMAGE_KEY:</label>
        <input name="key" placeholder="Можно оставить пустым">

        <label>Картинка:</label>
        <input type="file" name="image" accept="image/*" required>

        <label>Макс ширина:</label>
        <input name="max_w" value="{{ max_w }}">

        <label>Макс высота:</label>
        <input name="max_h" value="{{ max_h }}">

        <label>Квантование цвета, например 8 / 16 / 32:</label>
        <input name="color_step" value="{{ color_step }}">

        <button type="submit">Загрузить</button>
    </form>

    <hr>

    <p>Status: {{ status|safe }}</p>
    <p class="hint">
        Проверка: <code>/ping</code><br>
        Последняя картинка: <code>/latest</code>
    </p>
</div>
</body>
</html>
"""


def check_key(req) -> bool:
    if not IMAGE_KEY:
        return True

    key = ""
    if req.method == "GET":
        key = req.args.get("key", "")
    else:
        key = req.form.get("key", "") or req.headers.get("X-Image-Key", "")

    return key == IMAGE_KEY


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

    # 1) Делаем горизонтальные полосы одинакового цвета.
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

    # 2) Склеиваем одинаковые полосы по вертикали.
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

                same_x = other["x"] == current["x"]
                same_w = other["w"] == current["w"]
                same_color = (
                    other["r"] == current["r"]
                    and other["g"] == current["g"]
                    and other["b"] == current["b"]
                )
                touches_below = other["y"] == current["y"] + current["h"]

                if same_x and same_w and same_color and touches_below:
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


def save_latest(data: Dict[str, Any]) -> None:
    with open(LATEST_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


def load_latest() -> Dict[str, Any]:
    if not os.path.exists(LATEST_FILE):
        return {
            "ok": False,
            "error": "No image uploaded yet",
        }

    with open(LATEST_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


@app.route("/", methods=["GET"])
def index():
    latest = load_latest()

    if latest.get("ok"):
        status = (
            f"<span class='ok'>Есть картинка</span><br>"
            f"Size: {latest.get('width')}x{latest.get('height')}<br>"
            f"Rects: {latest.get('rect_count')}"
        )
    else:
        status = "<span class='bad'>Картинка ещё не загружена</span>"

    return render_template_string(
        HTML,
        status=status,
        max_w=MAX_W,
        max_h=MAX_H,
        color_step=COLOR_STEP,
    )


@app.route("/upload", methods=["POST"])
def upload():
    if not check_key(request):
        return jsonify({"ok": False, "error": "Bad key"}), 403

    if "image" not in request.files:
        return jsonify({"ok": False, "error": "No image file"}), 400

    file = request.files["image"]

    try:
        max_w = int(request.form.get("max_w", MAX_W))
        max_h = int(request.form.get("max_h", MAX_H))
        color_step = int(request.form.get("color_step", COLOR_STEP))

        max_w = max(1, min(256, max_w))
        max_h = max(1, min(256, max_h))
        color_step = max(1, min(64, color_step))

        raw = file.read()
        img = Image.open(BytesIO(raw))

        data = image_to_rects(img, max_w, max_h, color_step)
        save_latest(data)

        return f"""
        <body style="background:#111;color:white;font-family:Arial;padding:24px">
            <h2>Uploaded!</h2>
            <p>Size: {data["width"]}x{data["height"]}</p>
            <p>Rects: {data["rect_count"]}</p>
            <p><a style="color:#00aaff" href="/">Back</a></p>
        </body>
        """

    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/latest", methods=["GET"])
def latest():
    if IMAGE_KEY:
        key = request.args.get("key", "")
        if key != IMAGE_KEY:
            return jsonify({"ok": False, "error": "Bad key"}), 403

    return jsonify(load_latest())


@app.route("/clear", methods=["POST"])
def clear():
    if not check_key(request):
        return jsonify({"ok": False, "error": "Bad key"}), 403

    if os.path.exists(LATEST_FILE):
        os.remove(LATEST_FILE)

    return jsonify({"ok": True, "message": "cleared"})


@app.route("/ping", methods=["GET"])
def ping():
    return jsonify({
        "ok": True,
        "time": int(time.time()),
        "service": "babft-image-painter",
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)

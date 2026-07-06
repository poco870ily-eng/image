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
VIDEO_DIR = os.environ.get("VIDEO_DIR", "video_ports")

DEFAULT_RES = int(os.environ.get("DEFAULT_RES", "96"))
DEFAULT_VIDEO_RES = int(os.environ.get("DEFAULT_VIDEO_RES", "64"))
DEFAULT_COLOR_STEP = int(os.environ.get("DEFAULT_COLOR_STEP", "16"))
ABS_MAX_RES = int(os.environ.get("ABS_MAX_RES", "160"))
VIDEO_MAX_RES = int(os.environ.get("VIDEO_MAX_RES", "96"))
VIDEO_MAX_FRAMES = int(os.environ.get("VIDEO_MAX_FRAMES", "120"))
VIDEO_MAX_CHUNK_FRAMES = int(os.environ.get("VIDEO_MAX_CHUNK_FRAMES", "12"))
MAX_RECTS = int(os.environ.get("MAX_RECTS", "12000"))
ALPHA_LIMIT = int(os.environ.get("ALPHA_LIMIT", "35"))

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(ORIGINAL_DIR, exist_ok=True)
os.makedirs(VIDEO_DIR, exist_ok=True)

HTML = r"""
<!doctype html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <title>Image / Video Painter</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        :root { color-scheme: dark; }
        body { background:#0d0d10; color:white; font-family:Arial,sans-serif; padding:24px; }
        .box { max-width:720px; margin:auto; background:#18181c; border:1px solid #303038; border-radius:14px; padding:18px; }
        h2 { margin:0 0 12px; }
        h3 { margin:22px 0 8px; font-size:16px; }
        input, button, select { width:100%; margin-top:10px; padding:10px; border-radius:9px; border:1px solid #34343d; background:#0d0d10; color:white; font-size:15px; box-sizing:border-box; }
        button { background:#0984ff; border:0; color:white; font-weight:bold; cursor:pointer; }
        button:disabled { opacity:.55; cursor:not-allowed; }
        .row { display:grid; grid-template-columns:1fr 1fr; gap:10px; }
        .hint { color:#aaa; font-size:13px; line-height:1.35; }
        code { background:#000; padding:2px 5px; border-radius:4px; }
        .ok { color:#00ff99; }
        .bad { color:#ff7777; }
        .warn { color:#ffd36a; }
        .bar { height:10px; background:#09090b; border-radius:999px; overflow:hidden; border:1px solid #303038; margin-top:12px; }
        .fill { height:100%; width:0%; background:#00aaff; transition:width .15s linear; }
        canvas { display:none; }
    </style>
</head>
<body>
<div class="box">
    <h2>Image / Video Painter</h2>

    <h3>Image upload</h3>
    <form action="/upload" method="post" enctype="multipart/form-data">
        <input name="port" value="{{ port }}" placeholder="Port" required>
        {% if show_key %}<input name="key" placeholder="Key">{% endif %}
        <input type="file" name="image" accept="image/*" required>
        <button type="submit">Upload image</button>
    </form>

    <h3>Video upload</h3>
    <p class="hint">
        Video is converted in your browser, not on Render. This avoids 502/503 and makes Roblox playback smoother.
        Recommended: <code>64</code> res, <code>2</code> FPS, <code>40-80</code> frames.
    </p>
    <input id="vPort" value="{{ port }}" placeholder="Port" required>
    {% if show_key %}<input id="vKey" placeholder="Key">{% endif %}
    <input id="vFile" type="file" accept="video/*,.gif,.webp,.apng">
    <div class="row">
        <input id="vRes" type="number" min="16" max="96" value="64" placeholder="Resolution">
        <input id="vFps" type="number" min="0.25" max="8" step="0.25" value="2" placeholder="FPS">
    </div>
    <div class="row">
        <input id="vFrames" type="number" min="1" max="120" value="60" placeholder="Max frames">
        <input id="vStep" type="number" min="4" max="64" value="16" placeholder="Color step">
    </div>
    <button id="convertBtn" type="button">Convert video in browser</button>
    <div class="bar"><div id="progressFill" class="fill"></div></div>
    <p id="videoStatus" class="hint">Waiting for video.</p>

    <p>Status: {{ status|safe }}</p>
    <p class="hint"><code>/ping</code> · <code>/latest?port=PORT</code> · <code>/video/meta?port=PORT</code></p>
</div>

<video id="video" muted playsinline preload="auto" style="display:none"></video>
<canvas id="canvas"></canvas>

<script>
const $ = (id) => document.getElementById(id);
const statusEl = $("videoStatus");
const fillEl = $("progressFill");

function cleanPort(v) {
    const p = String(v || "").replace(/\D+/g, "").slice(0, 8);
    if (p.length < 3) throw new Error("Bad port");
    return p;
}
function clamp(n, lo, hi, d) {
    n = Number(n);
    if (!Number.isFinite(n)) n = d;
    return Math.max(lo, Math.min(hi, n));
}
function q(v, step) {
    return Math.max(0, Math.min(255, Math.round(v / step) * step));
}
function hex2(r,g,b) {
    return [r,g,b].map(v => Math.max(0, Math.min(255, v|0)).toString(16).padStart(2,"0")).join("").toUpperCase();
}
function setProgress(done, total, text) {
    const pct = total > 0 ? Math.max(0, Math.min(100, Math.round(done / total * 100))) : 0;
    fillEl.style.width = pct + "%";
    statusEl.textContent = text || (pct + "%");
}
async function postJSON(url, data) {
    const res = await fetch(url, {
        method: "POST",
        headers: {"Content-Type":"application/json", "Accept":"application/json"},
        body: JSON.stringify(data)
    });
    const text = await res.text();
    let json;
    try { json = JSON.parse(text); } catch (e) { throw new Error("Bad server body: " + text.slice(0, 180)); }
    if (!json.ok) throw new Error(json.error || "Server error");
    return json;
}
function waitEvent(el, name, timeoutMs) {
    return new Promise((resolve, reject) => {
        let done = false;
        const timer = setTimeout(() => {
            if (done) return;
            done = true;
            cleanup();
            reject(new Error("Timeout waiting for " + name));
        }, timeoutMs || 12000);
        function cleanup() {
            clearTimeout(timer);
            el.removeEventListener(name, ok);
            el.removeEventListener("error", bad);
        }
        function ok() { if (done) return; done = true; cleanup(); resolve(); }
        function bad() { if (done) return; done = true; cleanup(); reject(new Error("Video decode error")); }
        el.addEventListener(name, ok, {once:true});
        el.addEventListener("error", bad, {once:true});
    });
}
async function seekVideo(video, t) {
    const safeT = Math.max(0, Math.min((video.duration || 0) - 0.035, t));
    if (Math.abs(video.currentTime - safeT) < 0.015) return;
    const p = waitEvent(video, "seeked", 15000);
    video.currentTime = safeT;
    await p;
}
function frameToHexes(ctx, w, h, colorStep) {
    const data = ctx.getImageData(0, 0, w, h).data;
    const hexes = new Array(w * h);
    for (let i = 0, p = 0; i < hexes.length; i++, p += 4) {
        hexes[i] = hex2(q(data[p], colorStep), q(data[p+1], colorStep), q(data[p+2], colorStep));
    }
    return hexes;
}

$("convertBtn").onclick = async () => {
    const btn = $("convertBtn");
    btn.disabled = true;
    let objectUrl = null;
    try {
        const port = cleanPort($("vPort").value);
        const keyBox = $("vKey");
        const key = keyBox ? keyBox.value : "";
        const file = $("vFile").files[0];
        if (!file) throw new Error("Choose a video first");

        const maxSide = Math.floor(clamp($("vRes").value, 16, {{ video_max_res }}, 64));
        const fps = clamp($("vFps").value, 0.25, 8, 2);
        const maxFrames = Math.floor(clamp($("vFrames").value, 1, {{ video_max_frames }}, 60));
        const colorStep = Math.floor(clamp($("vStep").value, 4, 64, 16));
        const chunkSize = 6;

        const video = $("video");
        objectUrl = URL.createObjectURL(file);
        video.src = objectUrl;
        video.load();
        setProgress(0, 1, "Loading video metadata...");
        await waitEvent(video, "loadedmetadata", 25000);

        const srcW = video.videoWidth || 1;
        const srcH = video.videoHeight || 1;
        const scale = Math.min(maxSide / Math.max(srcW, 1), maxSide / Math.max(srcH, 1), 1);
        const w = Math.max(1, Math.round(srcW * scale));
        const h = Math.max(1, Math.round(srcH * scale));
        const duration = Number.isFinite(video.duration) ? video.duration : (maxFrames / fps);
        const frameCount = Math.max(1, Math.min(maxFrames, Math.floor(duration * fps) + 1));

        const canvas = $("canvas");
        canvas.width = w;
        canvas.height = h;
        const ctx = canvas.getContext("2d", {willReadFrequently:true});
        ctx.imageSmoothingEnabled = true;

        await postJSON("/video_json/start", {port, key, width:w, height:h, fps, frame_count:frameCount, color_step:colorStep, chunk_size:chunkSize});

        let prev = null;
        let chunk = [];
        let chunkIndex = 0;
        let totalChanges = 0;
        for (let i = 0; i < frameCount; i++) {
            const t = Math.min(duration, i / fps);
            await seekVideo(video, t);
            ctx.clearRect(0, 0, w, h);
            ctx.drawImage(video, 0, 0, w, h);
            const hexes = frameToHexes(ctx, w, h, colorStep);
            let frame;
            if (i === 0 || !prev) {
                frame = {index:i, pixels:hexes.join(""), change_count:w*h, full:true};
                totalChanges += w*h;
            } else {
                const changes = [];
                for (let n = 0; n < hexes.length; n++) {
                    if (hexes[n] !== prev[n]) changes.push([n + 1, hexes[n]]);
                }
                frame = {index:i, changes, change_count:changes.length, full:false};
                totalChanges += changes.length;
            }
            chunk.push(frame);
            prev = hexes;

            if (chunk.length >= chunkSize || i === frameCount - 1) {
                await postJSON("/video_json/chunk", {port, key, chunk:chunkIndex, frames:chunk});
                chunk = [];
                chunkIndex++;
            }
            setProgress(i + 1, frameCount, `Converted ${i + 1}/${frameCount} frames · ${w}x${h} · changes ${totalChanges}`);
            await new Promise(r => setTimeout(r, 1));
        }

        await postJSON("/video_json/finish", {port, key});
        setProgress(frameCount, frameCount, `Ready. Port ${port} · ${w}x${h} · ${frameCount} frames · ${fps} FPS`);
    } catch (e) {
        statusEl.textContent = "Video error: " + (e && e.message ? e.message : e);
        fillEl.style.width = "0%";
    } finally {
        btn.disabled = false;
        if (objectUrl) URL.revokeObjectURL(objectUrl);
    }
};
</script>
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


def video_port_dir(port: str) -> str:
    return os.path.join(VIDEO_DIR, clean_port(port))


def video_meta_file(port: str) -> str:
    return os.path.join(video_port_dir(port), "meta.json")


def video_chunk_file(port: str, chunk: int) -> str:
    return os.path.join(video_port_dir(port), f"chunk_{max(0, int(chunk)):04d}.json")


def atomic_write(path: str, data: bytes) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def atomic_write_json(path: str, data: Dict[str, Any]) -> None:
    atomic_write(path, json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def read_json_body() -> Dict[str, Any]:
    try:
        data = request.get_json(silent=True)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def check_key(req) -> bool:
    if not IMAGE_KEY:
        return True
    body = read_json_body()
    return (
        (req.values.get("key", "") or req.headers.get("X-Image-Key", "") or body.get("key", ""))
        == IMAGE_KEY
    )


def read_int(name: str, default: int, lo: int, hi: int) -> int:
    try:
        value = int(request.values.get(name, default))
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
        return {"ok": False, "error": "No image", "port": clean_port(port)}
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


def load_video_meta(port: str) -> Dict[str, Any]:
    path = video_meta_file(port)
    if not os.path.exists(path):
        return {"ok": False, "error": "No browser-converted video on this port", "port": clean_port(port), "type": "video"}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_video_chunk(port: str, chunk: int) -> Dict[str, Any]:
    path = video_chunk_file(port, chunk)
    if not os.path.exists(path):
        return {"ok": False, "error": "Missing video chunk", "chunk": int(chunk), "port": clean_port(port)}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def normalize_frame(frame: Dict[str, Any], meta: Dict[str, Any]) -> Dict[str, Any]:
    index = int(frame.get("index", 0))
    width = int(meta.get("width", 1))
    height = int(meta.get("height", 1))
    out = {
        "ok": True,
        "type": "video_frame",
        "index": index,
        "width": width,
        "height": height,
        "pixel_count": width * height,
        "frame_count": int(meta.get("frame_count", 1)),
        "fps": float(meta.get("fps", 2)),
        "color_step": int(meta.get("color_step", 16)),
        "port": meta.get("port"),
        "change_count": int(frame.get("change_count", 0)),
    }
    if "pixels" in frame:
        out["pixels"] = str(frame.get("pixels", ""))
        out["full"] = True
        out["change_count"] = width * height
    else:
        out["changes"] = frame.get("changes", [])
        out["full"] = False
    return out


def find_frame(port: str, index: int, meta: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    frame_count = max(1, int(meta.get("frame_count", 1)))
    chunk_size = max(1, int(meta.get("chunk_size", 6)))
    index = int(index) % frame_count
    chunk_id = index // chunk_size
    chunk = load_video_chunk(port, chunk_id)
    frames = chunk.get("frames", []) if isinstance(chunk, dict) else []
    for frame in frames:
        if int(frame.get("index", -1)) == index:
            return normalize_frame(frame, meta)
    return None


@app.route("/", methods=["GET"])
def index():
    port = request.args.get("port", "").strip()
    status = "<span class='bad'>No port</span>"
    if port:
        try:
            p = clean_port(port)
            img = load_cached_latest(p)
            vid = load_video_meta(p)
            parts = []
            if img.get("ok"):
                parts.append(f"<span class='ok'>Image ready: {p}</span><br>{img.get('width')}x{img.get('height')} · Rects: {img.get('rect_count')}")
            if vid.get("ok"):
                parts.append(f"<span class='ok'>Video ready: {p}</span><br>{vid.get('width')}x{vid.get('height')} · Frames: {vid.get('frame_count')} · FPS: {vid.get('fps')}")
            status = "<br><br>".join(parts) if parts else "<span class='bad'>Nothing uploaded for this port</span>"
        except Exception as e:
            status = f"<span class='bad'>{e}</span>"
    return render_template_string(HTML, status=status, port=port, show_key=bool(IMAGE_KEY), video_max_res=VIDEO_MAX_RES, video_max_frames=VIDEO_MAX_FRAMES)


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


@app.route("/video_json/start", methods=["POST"])
def video_json_start():
    if not check_key(request):
        return json_error("Bad key", 403)
    data = read_json_body()
    port = clean_port(data.get("port", ""))
    width = max(1, min(VIDEO_MAX_RES, int(data.get("width", DEFAULT_VIDEO_RES))))
    height = max(1, min(VIDEO_MAX_RES, int(data.get("height", DEFAULT_VIDEO_RES))))
    fps = max(0.25, min(8.0, float(data.get("fps", 2))))
    frame_count = max(1, min(VIDEO_MAX_FRAMES, int(data.get("frame_count", 60))))
    color_step = max(4, min(64, int(data.get("color_step", DEFAULT_COLOR_STEP))))
    chunk_size = max(1, min(VIDEO_MAX_CHUNK_FRAMES, int(data.get("chunk_size", 6))))
    pdir = video_port_dir(port)
    os.makedirs(pdir, exist_ok=True)
    for name in os.listdir(pdir):
        if name.endswith(".json"):
            try:
                os.remove(os.path.join(pdir, name))
            except Exception:
                pass
    meta = {
        "ok": True,
        "type": "video",
        "port": port,
        "width": width,
        "height": height,
        "pixel_count": width * height,
        "fps": fps,
        "frame_count": frame_count,
        "color_step": color_step,
        "chunk_size": chunk_size,
        "chunk_count": int((frame_count + chunk_size - 1) // chunk_size),
        "ready": False,
        "browser_converted": True,
        "created_at": int(time.time()),
        "version": str(int(time.time() * 1000)),
    }
    atomic_write_json(video_meta_file(port), meta)
    return jsonify({"ok": True, "port": port, "type": "video_start", "meta": meta})


@app.route("/video_json/chunk", methods=["POST"])
def video_json_chunk():
    if not check_key(request):
        return json_error("Bad key", 403)
    data = read_json_body()
    port = clean_port(data.get("port", ""))
    meta = load_video_meta(port)
    if not meta.get("ok"):
        return json_error(meta.get("error", "No active video upload"), 200, {"port": port})
    chunk = max(0, int(data.get("chunk", 0)))
    frames = data.get("frames", [])
    if not isinstance(frames, list) or not frames:
        return json_error("No frames in chunk", 200, {"port": port})
    cleaned = []
    width = int(meta.get("width", 1))
    height = int(meta.get("height", 1))
    full_len = width * height * 6
    for frame in frames:
        if not isinstance(frame, dict):
            continue
        idx = int(frame.get("index", 0))
        out = {"index": idx}
        if isinstance(frame.get("pixels"), str):
            pixels = frame.get("pixels", "")[:full_len]
            out["pixels"] = pixels
            out["change_count"] = width * height
            out["full"] = True
        else:
            changes = frame.get("changes", [])
            if not isinstance(changes, list):
                changes = []
            safe_changes = []
            for item in changes:
                if isinstance(item, list) and len(item) >= 2:
                    try:
                        px = int(item[0])
                        hx = str(item[1]).upper()[:6]
                    except Exception:
                        continue
                    if 1 <= px <= width * height and len(hx) == 6:
                        safe_changes.append([px, hx])
            out["changes"] = safe_changes
            out["change_count"] = len(safe_changes)
            out["full"] = False
        cleaned.append(out)
    payload = {"ok": True, "type": "video_chunk", "port": port, "chunk": chunk, "frames": cleaned, "saved_at": int(time.time())}
    atomic_write_json(video_chunk_file(port, chunk), payload)
    return jsonify({"ok": True, "port": port, "type": "video_chunk_saved", "chunk": chunk, "frames": len(cleaned)})


@app.route("/video_json/finish", methods=["POST"])
def video_json_finish():
    if not check_key(request):
        return json_error("Bad key", 403)
    data = read_json_body()
    port = clean_port(data.get("port", ""))
    meta = load_video_meta(port)
    if not meta.get("ok"):
        return json_error(meta.get("error", "No active video upload"), 200, {"port": port})
    existing = 0
    for i in range(int(meta.get("chunk_count", 0))):
        if os.path.exists(video_chunk_file(port, i)):
            existing += 1
    meta["ready"] = existing >= int(meta.get("chunk_count", 0))
    meta["saved_chunks"] = existing
    meta["finished_at"] = int(time.time())
    meta["version"] = str(int(time.time() * 1000))
    atomic_write_json(video_meta_file(port), meta)
    return jsonify({"ok": True, "port": port, "type": "video_finished", "ready": meta["ready"], "chunks": existing, "chunk_count": meta.get("chunk_count")})


@app.route("/video/meta", methods=["GET"])
def video_meta():
    if IMAGE_KEY and not check_key(request):
        return json_error("Bad key", 403)
    port = clean_port(request.args.get("port", ""))
    meta = load_video_meta(port)
    resp = jsonify(meta)
    resp.headers["Cache-Control"] = "no-store, max-age=0"
    return resp


@app.route("/video/frame", methods=["GET"])
def video_frame():
    if IMAGE_KEY and not check_key(request):
        return json_error("Bad key", 403)
    port = clean_port(request.args.get("port", ""))
    meta = load_video_meta(port)
    if not meta.get("ok"):
        return jsonify(meta)
    if not meta.get("ready", False):
        return json_error("Video is not finished yet", 200, {"port": port, "type": "video_frame"})
    index = read_int("index", 0, 0, max(0, int(meta.get("frame_count", 1)) - 1))
    frame = find_frame(port, index, meta)
    if not frame:
        return json_error("Frame not found", 200, {"port": port, "index": index, "type": "video_frame"})
    resp = jsonify(frame)
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp


@app.route("/video/frames", methods=["GET"])
def video_frames():
    if IMAGE_KEY and not check_key(request):
        return json_error("Bad key", 403)
    port = clean_port(request.args.get("port", ""))
    meta = load_video_meta(port)
    if not meta.get("ok"):
        return jsonify(meta)
    if not meta.get("ready", False):
        return json_error("Video is not finished yet", 200, {"port": port, "type": "video_frames"})
    frame_count = max(1, int(meta.get("frame_count", 1)))
    start = read_int("start", 0, 0, frame_count - 1)
    count = read_int("count", 4, 1, VIDEO_MAX_CHUNK_FRAMES)
    frames = []
    for offset in range(count):
        idx = start + offset
        if idx >= frame_count:
            break
        frame = find_frame(port, idx, meta)
        if frame:
            frames.append(frame)
    payload = {
        "ok": True,
        "type": "video_frames",
        "port": port,
        "start": start,
        "count": len(frames),
        "frames": frames,
        "frame_count": frame_count,
        "width": int(meta.get("width", 1)),
        "height": int(meta.get("height", 1)),
        "fps": float(meta.get("fps", 2)),
    }
    resp = jsonify(payload)
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp


@app.route("/clear", methods=["POST", "GET"])
def clear():
    if IMAGE_KEY and not check_key(request):
        return json_error("Bad key", 403)
    port = clean_port(request.values.get("port", ""))
    for path in (port_file(port), original_file(port)):
        if os.path.exists(path):
            os.remove(path)
    pdir = video_port_dir(port)
    if os.path.isdir(pdir):
        for name in os.listdir(pdir):
            if name.endswith(".json"):
                try:
                    os.remove(os.path.join(pdir, name))
                except Exception:
                    pass
    return jsonify({"ok": True, "message": "Cleared", "port": port})


@app.route("/ping", methods=["GET"])
def ping():
    return jsonify({
        "ok": True,
        "time": int(time.time()),
        "service": "image-video-browser-convert",
        "abs_max_res": ABS_MAX_RES,
        "video_max_res": VIDEO_MAX_RES,
        "video_max_frames": VIDEO_MAX_FRAMES,
        "mode": "browser converts video; server stores JSON chunks",
    })


@app.route("/debug", methods=["GET"])
def debug():
    return jsonify({
        "ok": True,
        "time": int(time.time()),
        "data_dir": DATA_DIR,
        "video_dir": VIDEO_DIR,
        "image_key_enabled": bool(IMAGE_KEY),
        "ports": sorted(os.listdir(VIDEO_DIR))[:50] if os.path.isdir(VIDEO_DIR) else [],
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)

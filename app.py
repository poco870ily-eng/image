import hmac
import json
import os
import re
import time
import traceback
from urllib.parse import quote_plus, urlparse
from io import BytesIO
from typing import Any, Dict, List, Optional, Tuple

import requests
from flask import Flask, Response, jsonify, render_template_string, request
from PIL import Image, ImageFile

ImageFile.LOAD_TRUNCATED_IMAGES = True
Image.MAX_IMAGE_PIXELS = int(os.environ.get("MAX_IMAGE_PIXELS", "20000000"))

app = Flask(__name__)
APP_VERSION = "model-search-v9-query-2026-07-11"

IMAGE_KEY = os.environ.get("IMAGE_KEY", "").strip()
ADMIN_KEY = os.environ.get("ADMIN_KEY", "admin123").strip() or "admin123"
ROBLOX_OPEN_CLOUD_KEY = os.environ.get("ROBLOX_OPEN_CLOUD_KEY", "").strip()
ROBLOX_HTTP_TIMEOUT = max(5, min(60, int(os.environ.get("ROBLOX_HTTP_TIMEOUT", "25"))))
ROBLOX_MAX_MODEL_BYTES = max(1_000_000, min(100_000_000, int(os.environ.get("ROBLOX_MAX_MODEL_BYTES", "50000000"))))
DATA_DIR = os.environ.get("DATA_DIR", "image_ports")
ORIGINAL_DIR = os.environ.get("ORIGINAL_DIR", "image_originals")
VIDEO_DIR = os.environ.get("VIDEO_DIR", "video_ports")
IMAGE_SETTINGS_DIR = os.environ.get("IMAGE_SETTINGS_DIR", "image_settings")

DEFAULT_RES = int(os.environ.get("DEFAULT_RES", "96"))
DEFAULT_VIDEO_RES = int(os.environ.get("DEFAULT_VIDEO_RES", "64"))
DEFAULT_COLOR_STEP = int(os.environ.get("DEFAULT_COLOR_STEP", "16"))
ABS_MAX_RES = int(os.environ.get("ABS_MAX_RES", "160"))
VIDEO_MAX_RES = int(os.environ.get("VIDEO_MAX_RES", "96"))
VIDEO_MAX_FRAMES = int(os.environ.get("VIDEO_MAX_FRAMES", "120"))
VIDEO_MAX_CHUNK_FRAMES = int(os.environ.get("VIDEO_MAX_CHUNK_FRAMES", "12"))
MAX_RECTS = int(os.environ.get("MAX_RECTS", "12000"))
ALPHA_LIMIT = int(os.environ.get("ALPHA_LIMIT", "35"))

for path in (DATA_DIR, ORIGINAL_DIR, VIDEO_DIR, IMAGE_SETTINGS_DIR):
    os.makedirs(path, exist_ok=True)

HTML = r'''
<!doctype html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <title>Image / Video Painter</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        :root { color-scheme: dark; }
        * { box-sizing: border-box; }
        body { margin: 0; background: #0c0c10; color: #f4f4f7; font-family: Arial, sans-serif; }
        .wrap { max-width: 860px; margin: 0 auto; padding: 22px; }
        .box { background: #17171d; border: 1px solid #2d2d36; border-radius: 16px; padding: 18px; }
        h1 { font-size: 24px; margin: 0 0 18px; }
        h2 { font-size: 18px; margin: 0 0 12px; }
        .section { margin-top: 18px; padding-top: 18px; border-top: 1px solid #2a2a33; }
        .section:first-of-type { margin-top: 0; padding-top: 0; border-top: 0; }
        .grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
        .grid4 { display: grid; grid-template-columns: 1fr 1fr 1fr 1fr; gap: 10px; }
        @media (max-width: 720px) {
            .grid2, .grid4 { grid-template-columns: 1fr; }
        }
        label { display: block; font-size: 13px; color: #b7b7c4; margin: 10px 0 6px; }
        input, button { width: 100%; border-radius: 10px; border: 1px solid #333441; background: #0e0e14; color: #fff; padding: 11px 12px; font-size: 14px; }
        input[type=file] { padding: 9px 10px; }
        button { cursor: pointer; border: 0; background: #1388ff; font-weight: 700; }
        button.secondary { background: #262833; }
        button:disabled { opacity: .58; cursor: not-allowed; }
        .hint { margin-top: 6px; font-size: 12px; color: #9898a7; line-height: 1.45; }
        .status { margin-top: 12px; min-height: 20px; font-size: 13px; color: #d8d8e2; white-space: pre-wrap; }
        .ok { color: #77f2a7; }
        .bad { color: #ff8d8d; }
        .bar { margin-top: 12px; height: 10px; background: #0c0c12; border: 1px solid #2f3040; border-radius: 999px; overflow: hidden; }
        .fill { height: 100%; width: 0%; background: linear-gradient(90deg, #1388ff, #1cb8ff); transition: width .12s linear; }
        .topline { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-bottom: 8px; }
        @media (max-width: 720px) { .topline { grid-template-columns: 1fr; } }
        .mini { margin-top: 8px; font-size: 12px; color: #a9a9b8; }
        .tabs { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin: 0 0 18px; padding: 5px; border-radius: 12px; background: #0e0e14; border: 1px solid #2b2c36; }
        .tab-btn { background: transparent; color: #a8a8b5; border: 1px solid transparent; }
        .tab-btn.active { background: #262833; color: #fff; border-color: #353744; }
        .tab-panel { display: none; }
        .tab-panel.active { display: block; }
        .model-actions { display: grid; grid-template-columns: 1fr auto; gap: 10px; align-items: end; }
        .model-actions button { width: auto; min-width: 120px; }
        .model-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; margin-top: 14px; }
        .model-card { min-width: 0; overflow: hidden; border-radius: 14px; border: 1px solid #2d2e39; background: #111117; }
        .model-thumb { display: block; width: 100%; aspect-ratio: 1 / 1; object-fit: cover; background: #09090d; }
        .model-body { padding: 12px; }
        .model-name { font-size: 14px; font-weight: 700; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        .model-meta { min-height: 34px; margin: 6px 0 10px; color: #9797a6; font-size: 12px; line-height: 1.4; overflow-wrap: anywhere; }
        .model-card button { padding: 10px; }
        .empty-models { grid-column: 1 / -1; padding: 28px 14px; text-align: center; color: #9797a6; border: 1px dashed #333441; border-radius: 14px; }
        .admin-row { display: flex; gap: 10px; align-items: center; justify-content: space-between; }
        .admin-row button { width: auto; padding: 9px 12px; }
        .modal-backdrop { position: fixed; inset: 0; z-index: 1000; display: none; align-items: center; justify-content: center; padding: 18px; background: rgba(0,0,0,.72); backdrop-filter: blur(8px); }
        .modal-backdrop.open { display: flex; }
        .modal-card { width: min(410px, 100%); padding: 18px; border-radius: 16px; border: 1px solid #333441; background: #17171d; box-shadow: 0 24px 80px rgba(0,0,0,.55); }
        .modal-card h2 { margin-bottom: 6px; }
        .modal-buttons { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-top: 12px; }
        .download-note { margin-top: 12px; font-size: 12px; line-height: 1.45; color: #9292a1; }
        @media (max-width: 720px) {
            .model-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
            .model-actions { grid-template-columns: 1fr; }
            .model-actions button { width: 100%; }
        }
        @media (max-width: 430px) { .model-grid { grid-template-columns: 1fr; } }
    </style>
</head>
<body>
<div class="wrap">
    <div class="box">
        <h1>Nameless Tools</h1>
        <div class="mini">Build {{ app_version }}</div>
        <div class="tabs">
            <button id="painterTabBtn" class="tab-btn active" type="button">Image / Video</button>
            <button id="modelsTabBtn" class="tab-btn" type="button">Model Browser</button>
        </div>

        <div id="painterPanel" class="tab-panel active">
        <div class="topline">
            <div>
                <label for="sharedPort">Port</label>
                <input id="sharedPort" value="{{ port }}" placeholder="Port">
            </div>
            {% if show_key %}
            <div>
                <label for="sharedKey">Key</label>
                <input id="sharedKey" placeholder="Key">
            </div>
            {% endif %}
        </div>

        <div class="section">
            <h2>Image</h2>
            <div class="grid2">
                <div>
                    <label for="imgFile">Choose image</label>
                    <input id="imgFile" type="file" accept="image/*">
                </div>
                <div>
                    <label>&nbsp;</label>
                    <button id="imgUploadBtn" type="button">Upload image</button>
                </div>
            </div>
            <div class="grid2">
                <div>
                    <label for="imgRes">Image resolution</label>
                    <input id="imgRes" type="number" min="8" max="{{ abs_max_res }}" value="96">
                    <div class="hint">Bigger value = more detail and more blocks.</div>
                </div>
                <div>
                    <label for="imgStep">Image color step</label>
                    <input id="imgStep" type="number" min="4" max="64" value="16">
                    <div class="hint">Smaller value = more accurate colors.</div>
                </div>
            </div>
            <div id="imgStatus" class="status">Choose an image, upload it once, then changing image settings updates it automatically.</div>
        </div>

        <div class="section">
            <h2>Video</h2>
            <div class="grid2">
                <div>
                    <label for="vidFile">Choose video</label>
                    <input id="vidFile" type="file" accept="video/*,.gif,.webp,.apng">
                </div>
                <div>
                    <label>&nbsp;</label>
                    <button id="videoConvertBtn" type="button">Convert video</button>
                </div>
            </div>
            <div class="grid4">
                <div>
                    <label for="vidRes">Video resolution</label>
                    <input id="vidRes" type="number" min="16" max="{{ video_max_res }}" value="64">
                    <div class="hint">Smaller value = smoother playback.</div>
                </div>
                <div>
                    <label for="vidFps">Video FPS</label>
                    <input id="vidFps" type="number" min="0.25" max="8" step="0.25" value="2">
                    <div class="hint">How many frames are saved per second.</div>
                </div>
                <div>
                    <label for="vidFrames">Max frames</label>
                    <input id="vidFrames" type="number" min="1" max="{{ video_max_frames }}" value="60">
                    <div class="hint">Total frames to keep from the video.</div>
                </div>
                <div>
                    <label for="vidStep">Video color step</label>
                    <input id="vidStep" type="number" min="4" max="64" value="16">
                    <div class="hint">Smaller value = more accurate colors.</div>
                </div>
            </div>
            <div class="bar"><div id="videoFill" class="fill"></div></div>
            <div id="videoStatus" class="status">Choose a video. Changing video settings automatically rebuilds the current selected video.</div>
            <div class="mini" id="pageStatus"></div>
        </div>
        </div>

        <div id="modelsPanel" class="tab-panel">
            <div class="admin-row">
                <div>
                    <h2>Roblox Model Browser</h2>
                    <div class="hint">Search public Creator Store models and download the model file returned by Roblox.</div>
                </div>
                <button id="adminLogoutBtn" type="button" class="secondary">Lock</button>
            </div>
            <div class="section">
                <div class="model-actions">
                    <div>
                        <label for="modelQuery">Model name</label>
                        <input id="modelQuery" autocomplete="off" placeholder="castle, car, house...">
                    </div>
                    <button id="modelSearchBtn" type="button">Search</button>
                </div>
                <div id="modelStatus" class="status">Enter a model name.</div>
                <div id="modelResults" class="model-grid"></div>
                <div class="download-note">Roblox can refuse private, paid, deleted, moderated, or permission-restricted assets. XML models are returned as .rbxmx; binary models are returned as .rbxm.</div>
            </div>
        </div>
    </div>
</div>

<div id="adminModal" class="modal-backdrop" aria-hidden="true">
    <div class="modal-card">
        <h2>Admin access</h2>
        <div class="hint">Enter the admin key to open Model Browser.</div>
        <label for="adminKeyInput">Admin key</label>
        <input id="adminKeyInput" type="password" autocomplete="current-password" placeholder="Admin key">
        <div id="adminLoginStatus" class="status"></div>
        <div class="modal-buttons">
            <button id="adminCancelBtn" type="button" class="secondary">Cancel</button>
            <button id="adminLoginBtn" type="button">Open</button>
        </div>
    </div>
</div>

<video id="hiddenVideo" muted playsinline preload="auto" style="display:none"></video>
<canvas id="hiddenCanvas" style="display:none"></canvas>

<script>
const $ = id => document.getElementById(id);
const state = {
    imageUploaded: false,
    videoConverted: false,
    adminKey: '',
    modelBusy: false,
    imageSettingsTimer: null,
    videoSettingsTimer: null,
    busyImage: false,
    busyVideo: false,
};


function switchPanel(name) {
    const painter = name === 'painter';
    $('painterPanel').classList.toggle('active', painter);
    $('modelsPanel').classList.toggle('active', !painter);
    $('painterTabBtn').classList.toggle('active', painter);
    $('modelsTabBtn').classList.toggle('active', !painter);
    if (painter) state.adminKey = '';
}
function showAdminModal() {
    $('adminLoginStatus').textContent = '';
    $('adminKeyInput').value = '';
    $('adminModal').classList.add('open');
    $('adminModal').setAttribute('aria-hidden', 'false');
    setTimeout(() => $('adminKeyInput').focus(), 30);
}
function hideAdminModal() {
    $('adminModal').classList.remove('open');
    $('adminModal').setAttribute('aria-hidden', 'true');
}
function adminHeaders(extra) {
    return Object.assign({ 'X-Admin-Key': state.adminKey }, extra || {});
}
async function openModelBrowser() {
    const key = String($('adminKeyInput').value || '');
    if (!key) {
        $('adminLoginStatus').textContent = 'Enter the admin key.';
        return;
    }
    $('adminLoginBtn').disabled = true;
    $('adminLoginStatus').textContent = 'Checking key...';
    try {
        const res = await fetch('/admin/login', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ key })
        });
        const data = await parseJson(res);
        if (!res.ok || !data.ok) throw new Error(data.error || 'Bad admin key');
        state.adminKey = key;
        hideAdminModal();
        switchPanel('models');
        if (!data.roblox_key_configured) {
            $('modelStatus').textContent = 'Server setup required: add ROBLOX_OPEN_CLOUD_KEY in Render Environment.';
        } else {
            $('modelStatus').textContent = 'Enter a model name.';
        }
        $('modelQuery').focus();
    } catch (e) {
        state.adminKey = '';
        $('adminLoginStatus').textContent = e && e.message ? e.message : String(e);
    } finally {
        $('adminLoginBtn').disabled = false;
    }
}
function safeText(value) {
    return String(value == null ? '' : value);
}
function cardElement(model) {
    const card = document.createElement('div');
    card.className = 'model-card';

    const img = document.createElement('img');
    img.className = 'model-thumb';
    img.loading = 'lazy';
    img.alt = safeText(model.name || 'Roblox model');
    if (model.thumbnail) img.src = model.thumbnail;

    const body = document.createElement('div');
    body.className = 'model-body';
    const title = document.createElement('div');
    title.className = 'model-name';
    title.title = safeText(model.name || 'Untitled model');
    title.textContent = safeText(model.name || 'Untitled model');

    const meta = document.createElement('div');
    meta.className = 'model-meta';
    meta.textContent = 'by ' + safeText(model.creator || 'Unknown') + ' · ID ' + safeText(model.id);

    const button = document.createElement('button');
    button.type = 'button';
    button.textContent = 'Download RBXM';
    button.onclick = () => downloadModel(model, button);

    body.append(title, meta, button);
    card.append(img, body);
    return card;
}
async function searchModels() {
    if (state.modelBusy) return;
    const query = String($('modelQuery').value || '').trim();
    if (query.length < 2) {
        $('modelStatus').textContent = 'Enter at least 2 characters.';
        return;
    }
    state.modelBusy = true;
    $('modelSearchBtn').disabled = true;
    $('modelStatus').textContent = 'Searching Roblox Creator Store...';
    $('modelResults').replaceChildren();
    try {
        const res = await fetch('/admin/models/search?q=' + encodeURIComponent(query) + '&limit=24&_=' + Date.now(), {
            cache: 'no-store',
            headers: adminHeaders({ 'Accept': 'application/json' })
        });
        const data = await parseJson(res);
        if (res.status === 401 || res.status === 403) {
            state.adminKey = '';
            switchPanel('painter');
            showAdminModal();
            throw new Error(data.error || 'Admin access expired');
        }
        if (!res.ok || !data.ok) throw new Error(data.error || 'Search failed');
        const models = Array.isArray(data.models) ? data.models : [];
        $('modelStatus').textContent = models.length ? ('Found ' + models.length + ' models.') : 'No models found.';
        if (!models.length) {
            const empty = document.createElement('div');
            empty.className = 'empty-models';
            empty.textContent = 'No matching public models.';
            $('modelResults').appendChild(empty);
        } else {
            models.forEach(model => $('modelResults').appendChild(cardElement(model)));
        }
    } catch (e) {
        $('modelStatus').textContent = 'Search error: ' + (e && e.message ? e.message : e);
    } finally {
        state.modelBusy = false;
        $('modelSearchBtn').disabled = false;
    }
}
function responseFilename(res, fallback) {
    const value = res.headers.get('Content-Disposition') || '';
    const utf = value.match(/filename\*=UTF-8''([^;]+)/i);
    if (utf) {
        try { return decodeURIComponent(utf[1]); } catch (e) {}
    }
    const normal = value.match(/filename="?([^";]+)"?/i);
    return normal ? normal[1] : fallback;
}
async function downloadModel(model, button) {
    const oldText = button.textContent;
    button.disabled = true;
    button.textContent = 'Downloading...';
    try {
        const res = await fetch('/admin/models/download/' + encodeURIComponent(model.id), {
            headers: adminHeaders()
        });
        if (!res.ok) {
            let message = 'Download failed';
            try {
                const data = await parseJson(res);
                message = data.error || message;
            } catch (e) {}
            throw new Error(message);
        }
        const blob = await res.blob();
        const filename = responseFilename(res, 'model_' + model.id + '.rbxm');
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = filename;
        document.body.appendChild(a);
        a.click();
        a.remove();
        setTimeout(() => URL.revokeObjectURL(url), 1500);
        $('modelStatus').textContent = 'Downloaded: ' + filename;
    } catch (e) {
        $('modelStatus').textContent = 'Download error: ' + (e && e.message ? e.message : e);
    } finally {
        button.disabled = false;
        button.textContent = oldText;
    }
}
$('painterTabBtn').onclick = () => switchPanel('painter');
$('modelsTabBtn').onclick = showAdminModal;
$('adminCancelBtn').onclick = hideAdminModal;
$('adminLoginBtn').onclick = openModelBrowser;
$('adminLogoutBtn').onclick = () => switchPanel('painter');
$('modelSearchBtn').onclick = searchModels;
$('modelQuery').addEventListener('keydown', e => { if (e.key === 'Enter') searchModels(); });
$('adminKeyInput').addEventListener('keydown', e => { if (e.key === 'Enter') openModelBrowser(); });
$('adminModal').addEventListener('click', e => { if (e.target === $('adminModal')) hideAdminModal(); });

function cleanPort(v) {
    const p = String(v || '').replace(/\D+/g, '').slice(0, 8);
    if (p.length < 3) throw new Error('Bad port');
    return p;
}
function getKey() {
    const el = $('sharedKey');
    return el ? String(el.value || '') : '';
}
function clamp(n, lo, hi, d) {
    n = Number(n);
    if (!Number.isFinite(n)) n = d;
    return Math.max(lo, Math.min(hi, n));
}
function setVideoProgress(done, total, text) {
    const pct = total > 0 ? Math.max(0, Math.min(100, Math.round(done / total * 100))) : 0;
    $('videoFill').style.width = pct + '%';
    if (text) $('videoStatus').textContent = text;
}
async function parseJson(res) {
    const text = await res.text();
    try { return JSON.parse(text); }
    catch (e) { throw new Error(text.slice(0, 220) || 'Bad JSON'); }
}
async function postJSON(url, data) {
    const res = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
        body: JSON.stringify(data)
    });
    const json = await parseJson(res);
    if (!json.ok) throw new Error(json.error || 'Request failed');
    return json;
}
function debounce(fn, key, delay) {
    clearTimeout(state[key]);
    state[key] = setTimeout(fn, delay);
}
function q(v, step) {
    return Math.max(0, Math.min(255, Math.round(v / step) * step));
}
function hex2(r, g, b) {
    return [r, g, b].map(v => Math.max(0, Math.min(255, v|0)).toString(16).padStart(2, '0')).join('').toUpperCase();
}
async function waitEvent(el, name, timeoutMs) {
    return new Promise((resolve, reject) => {
        let done = false;
        const timer = setTimeout(() => {
            if (done) return;
            done = true;
            cleanup();
            reject(new Error('Timeout waiting for ' + name));
        }, timeoutMs || 12000);
        function cleanup() {
            clearTimeout(timer);
            el.removeEventListener(name, ok);
            el.removeEventListener('error', bad);
        }
        function ok() { if (done) return; done = true; cleanup(); resolve(); }
        function bad() { if (done) return; done = true; cleanup(); reject(new Error('Video decode error')); }
        el.addEventListener(name, ok, { once: true });
        el.addEventListener('error', bad, { once: true });
    });
}
async function seekVideo(video, t) {
    const safeT = Math.max(0, Math.min((video.duration || 0) - 0.035, t));
    if (Math.abs(video.currentTime - safeT) < 0.015) return;
    const p = waitEvent(video, 'seeked', 15000);
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

function imageSettingsPayload() {
    return {
        port: cleanPort($('sharedPort').value),
        key: getKey(),
        max_side: Math.floor(clamp($('imgRes').value, 8, {{ abs_max_res }}, 96)),
        color_step: Math.floor(clamp($('imgStep').value, 4, 64, 16))
    };
}

async function reprocessImageSettings() {
    if (state.busyImage) return;
    try {
        state.busyImage = true;
        $('imgStatus').textContent = 'Updating image settings...';
        const data = await postJSON('/image/settings', imageSettingsPayload());
        state.imageUploaded = !!data.image_ready;
        $('imgStatus').textContent = data.summary || ('Image updated: ' + data.port);
    } catch (e) {
        $('imgStatus').textContent = 'Image error: ' + (e && e.message ? e.message : e);
    } finally {
        state.busyImage = false;
    }
}

$('imgUploadBtn').onclick = async () => {
    if (state.busyImage) return;
    const file = $('imgFile').files[0];
    if (!file) {
        $('imgStatus').textContent = 'Choose an image first.';
        return;
    }
    try {
        state.busyImage = true;
        $('imgStatus').textContent = 'Uploading image...';
        const fd = new FormData();
        const cfg = imageSettingsPayload();
        fd.append('port', cfg.port);
        if (cfg.key) fd.append('key', cfg.key);
        fd.append('max_side', cfg.max_side);
        fd.append('color_step', cfg.color_step);
        fd.append('image', file);
        const res = await fetch('/image/upload', { method: 'POST', body: fd });
        const json = await parseJson(res);
        if (!json.ok) throw new Error(json.error || 'Upload failed');
        state.imageUploaded = true;
        $('imgStatus').textContent = json.summary || ('Image uploaded: ' + json.port);
    } catch (e) {
        $('imgStatus').textContent = 'Image error: ' + (e && e.message ? e.message : e);
    } finally {
        state.busyImage = false;
    }
};

['imgRes', 'imgStep', 'sharedPort'].forEach(id => {
    $(id).addEventListener('input', () => debounce(() => {
        if (state.imageUploaded) reprocessImageSettings();
    }, 'imageSettingsTimer', 500));
});

function videoSettingsPayload() {
    return {
        port: cleanPort($('sharedPort').value),
        key: getKey(),
        max_side: Math.floor(clamp($('vidRes').value, 16, {{ video_max_res }}, 64)),
        fps: clamp($('vidFps').value, 0.25, 8, 2),
        max_frames: Math.floor(clamp($('vidFrames').value, 1, {{ video_max_frames }}, 60)),
        color_step: Math.floor(clamp($('vidStep').value, 4, 64, 16))
    };
}

async function convertVideo(autoTriggered) {
    if (state.busyVideo) return;
    const file = $('vidFile').files[0];
    if (!file) {
        if (!autoTriggered) $('videoStatus').textContent = 'Choose a video first.';
        return;
    }
    state.busyVideo = true;
    $('videoConvertBtn').disabled = true;
    let objectUrl = null;
    try {
        const cfg = videoSettingsPayload();
        const chunkSize = 6;
        const video = $('hiddenVideo');
        const canvas = $('hiddenCanvas');
        objectUrl = URL.createObjectURL(file);
        video.src = objectUrl;
        video.load();
        setVideoProgress(0, 1, 'Loading video...');
        await waitEvent(video, 'loadedmetadata', 25000);

        const srcW = video.videoWidth || 1;
        const srcH = video.videoHeight || 1;
        const scale = Math.min(cfg.max_side / Math.max(srcW, 1), cfg.max_side / Math.max(srcH, 1), 1);
        const w = Math.max(1, Math.round(srcW * scale));
        const h = Math.max(1, Math.round(srcH * scale));
        const duration = Number.isFinite(video.duration) ? video.duration : (cfg.max_frames / cfg.fps);
        const frameCount = Math.max(1, Math.min(cfg.max_frames, Math.floor(duration * cfg.fps) + 1));

        canvas.width = w;
        canvas.height = h;
        const ctx = canvas.getContext('2d', { willReadFrequently: true });
        ctx.imageSmoothingEnabled = true;

        await postJSON('/video_json/start', {
            port: cfg.port,
            key: cfg.key,
            width: w,
            height: h,
            fps: cfg.fps,
            frame_count: frameCount,
            color_step: cfg.color_step,
            chunk_size: chunkSize
        });

        let prev = null;
        let chunk = [];
        let chunkIndex = 0;
        let totalChanges = 0;

        for (let i = 0; i < frameCount; i++) {
            const t = Math.min(duration, i / cfg.fps);
            await seekVideo(video, t);
            ctx.clearRect(0, 0, w, h);
            ctx.drawImage(video, 0, 0, w, h);
            const hexes = frameToHexes(ctx, w, h, cfg.color_step);
            let frame;
            if (i === 0 || !prev) {
                frame = { index: i, pixels: hexes.join(''), change_count: w * h, full: true };
                totalChanges += w * h;
            } else {
                const changes = [];
                for (let n = 0; n < hexes.length; n++) {
                    if (hexes[n] !== prev[n]) changes.push([n + 1, hexes[n]]);
                }
                frame = { index: i, changes, change_count: changes.length, full: false };
                totalChanges += changes.length;
            }
            chunk.push(frame);
            prev = hexes;

            if (chunk.length >= chunkSize || i === frameCount - 1) {
                await postJSON('/video_json/chunk', { port: cfg.port, key: cfg.key, chunk: chunkIndex, frames: chunk });
                chunk = [];
                chunkIndex++;
            }
            setVideoProgress(i + 1, frameCount, 'Converting video... ' + (i + 1) + '/' + frameCount + ' frames');
            await new Promise(r => setTimeout(r, 1));
        }

        await postJSON('/video_json/finish', { port: cfg.port, key: cfg.key });
        state.videoConverted = true;
        setVideoProgress(frameCount, frameCount, 'Video ready: ' + w + 'x' + h + ' · ' + frameCount + ' frames · ' + cfg.fps + ' FPS');
        $('pageStatus').textContent = 'Changes saved for this port.';
    } catch (e) {
        $('videoStatus').textContent = 'Video error: ' + (e && e.message ? e.message : e);
        $('videoFill').style.width = '0%';
    } finally {
        state.busyVideo = false;
        $('videoConvertBtn').disabled = false;
        if (objectUrl) URL.revokeObjectURL(objectUrl);
    }
}

$('videoConvertBtn').onclick = () => convertVideo(false);
['vidRes', 'vidFps', 'vidFrames', 'vidStep', 'sharedPort'].forEach(id => {
    $(id).addEventListener('input', () => debounce(() => {
        if ($('vidFile').files[0]) convertVideo(true);
    }, 'videoSettingsTimer', 700));
});
$('vidFile').addEventListener('change', () => debounce(() => convertVideo(true), 'videoSettingsTimer', 250));

async function loadCurrentMeta() {
    try {
        const port = cleanPort($('sharedPort').value || '{{ port }}');
        const imageMeta = await fetch('/image/meta?port=' + encodeURIComponent(port)).then(parseJson);
        if (imageMeta.ok && imageMeta.settings) {
            $('imgRes').value = imageMeta.settings.max_side || 96;
            $('imgStep').value = imageMeta.settings.color_step || 16;
            if (imageMeta.image_ready) {
                state.imageUploaded = true;
                $('imgStatus').textContent = imageMeta.summary || 'Image ready on this port.';
            }
        }
        const videoMeta = await fetch('/video/meta?port=' + encodeURIComponent(port)).then(parseJson);
        if (videoMeta.ok) {
            $('vidRes').value = videoMeta.width || 64;
            $('vidFps').value = videoMeta.fps || 2;
            $('vidFrames').value = videoMeta.frame_count || 60;
            $('vidStep').value = videoMeta.color_step || 16;
            state.videoConverted = !!videoMeta.ready;
            $('videoStatus').textContent = 'Video ready: ' + (videoMeta.width || 0) + 'x' + (videoMeta.height || 0) + ' · ' + (videoMeta.frame_count || 0) + ' frames · ' + (videoMeta.fps || 0) + ' FPS';
            $('videoFill').style.width = videoMeta.ready ? '100%' : '0%';
        }
    } catch (e) {
    }
}

loadCurrentMeta();
</script>
</body>
</html>
'''


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


# ---------- utils ----------
def clean_port(value: str) -> str:
    value = "".join(ch for ch in str(value or "") if ch.isdigit())
    if len(value) < 3:
        raise ValueError("Bad port")
    return value[:8]


def port_file(port: str) -> str:
    return os.path.join(DATA_DIR, f"{clean_port(port)}.json")


def original_file(port: str) -> str:
    return os.path.join(ORIGINAL_DIR, f"{clean_port(port)}.bin")


def image_settings_file(port: str) -> str:
    return os.path.join(IMAGE_SETTINGS_DIR, f"{clean_port(port)}.json")


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
        body = request.get_json(silent=True)
        if isinstance(body, dict):
            return body
    except Exception:
        pass
    return {}


def check_key(req) -> bool:
    if not IMAGE_KEY:
        return True
    body = read_json_body()
    return (req.values.get("key", "") or req.headers.get("X-Image-Key", "") or body.get("key", "")) == IMAGE_KEY


def read_int_arg(args, name: str, default: int, lo: int, hi: int) -> int:
    try:
        value = int(args.get(name, default))
    except Exception:
        value = default
    return max(lo, min(hi, value))


def quantize_channel(v: int, step: int) -> int:
    step = max(1, min(255, int(step)))
    return max(0, min(255, round(v / step) * step))


def quantize_color(r: int, g: int, b: int, step: int) -> Tuple[int, int, int]:
    return (quantize_channel(r, step), quantize_channel(g, step), quantize_channel(b, step))


# ---------- image processing ----------
def default_image_settings() -> Dict[str, Any]:
    return {
        "max_side": DEFAULT_RES,
        "color_step": DEFAULT_COLOR_STEP,
        "updated_at": int(time.time()),
    }


def load_image_settings(port: str) -> Dict[str, Any]:
    path = image_settings_file(port)
    if not os.path.exists(path):
        return default_image_settings()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        out = default_image_settings()
        out["max_side"] = max(8, min(ABS_MAX_RES, int(data.get("max_side", DEFAULT_RES))))
        out["color_step"] = max(4, min(64, int(data.get("color_step", DEFAULT_COLOR_STEP))))
        out["updated_at"] = int(data.get("updated_at", int(time.time())))
        return out
    except Exception:
        return default_image_settings()


def save_image_settings(port: str, max_side: int, color_step: int) -> Dict[str, Any]:
    settings = {
        "max_side": max(8, min(ABS_MAX_RES, int(max_side))),
        "color_step": max(4, min(64, int(color_step))),
        "updated_at": int(time.time()),
        "port": clean_port(port),
    }
    atomic_write_json(image_settings_file(port), settings)
    return settings


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


def reprocess_image_for_port(port: str, max_side: int, color_step: int) -> Dict[str, Any]:
    data = load_original_as_rects(port, max_side, max_side, color_step)
    if data is None:
        return {"ok": False, "error": "No image uploaded on this port", "port": clean_port(port)}
    save_latest(port, data)
    return data


# ---------- video storage ----------
def load_video_meta(port: str) -> Dict[str, Any]:
    path = video_meta_file(port)
    if not os.path.exists(path):
        return {"ok": False, "error": "No video on this port", "port": clean_port(port), "type": "video"}
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



# ---------- Roblox model browser ----------
def admin_key_from_request() -> str:
    key = request.headers.get("X-Admin-Key", "")
    if key:
        return key
    body = request.get_json(silent=True)
    if isinstance(body, dict):
        return str(body.get("key", ""))
    return str(request.values.get("key", ""))


def admin_authorized() -> bool:
    supplied = admin_key_from_request()
    return bool(supplied) and hmac.compare_digest(supplied, ADMIN_KEY)


def require_admin_response():
    if admin_authorized():
        return None
    return json_error("Bad admin key", 403)


def roblox_headers(require_open_cloud: bool = False) -> Dict[str, str]:
    if require_open_cloud and not ROBLOX_OPEN_CLOUD_KEY:
        raise RuntimeError(
            "Creator Store search requires ROBLOX_OPEN_CLOUD_KEY. "
            "Add a Roblox Open Cloud API key in Render Environment and redeploy."
        )
    headers = {
        "Accept": "application/json",
        "User-Agent": "NamelessTools/1.0",
    }
    if ROBLOX_OPEN_CLOUD_KEY:
        headers["x-api-key"] = ROBLOX_OPEN_CLOUD_KEY
    return headers


def roblox_json_request(method: str, url: str, require_open_cloud: bool = False, **kwargs) -> Dict[str, Any]:
    headers = dict(roblox_headers(require_open_cloud=require_open_cloud))
    headers.update(kwargs.pop("headers", {}) or {})
    response = requests.request(
        method,
        url,
        headers=headers,
        timeout=(7, ROBLOX_HTTP_TIMEOUT),
        **kwargs,
    )
    if response.status_code >= 400:
        message = response.text[:300].strip()
        if response.status_code in (401, 403) and require_open_cloud:
            raise RuntimeError(
                "Open Cloud key was rejected. Check that ROBLOX_OPEN_CLOUD_KEY is valid, active, "
                "and has permission to search Creator Store assets."
            )
        raise RuntimeError(f"Roblox returned HTTP {response.status_code}: {message or 'request failed'}")
    try:
        data = response.json()
    except Exception as e:
        raise RuntimeError("Roblox returned invalid JSON") from e
    if not isinstance(data, dict):
        raise RuntimeError("Unexpected Roblox response")
    return data


def first_nonempty(mapping: Dict[str, Any], names: Tuple[str, ...], default: Any = None) -> Any:
    for name in names:
        value = mapping.get(name)
        if value is not None and value != "":
            return value
    return default


def find_asset_list(payload: Any) -> List[Dict[str, Any]]:
    if isinstance(payload, list):
        dicts = [item for item in payload if isinstance(item, dict)]
        if dicts and any(
            first_nonempty(item, ("id", "assetId", "assetID")) is not None or isinstance(item.get("asset"), dict)
            for item in dicts
        ):
            return dicts
        for item in payload:
            found = find_asset_list(item)
            if found:
                return found
    elif isinstance(payload, dict):
        for key in ("data", "assets", "items", "results", "creatorStoreAssets", "searchResults"):
            if key in payload:
                found = find_asset_list(payload[key])
                if found:
                    return found
        for value in payload.values():
            if isinstance(value, (dict, list)):
                found = find_asset_list(value)
                if found:
                    return found
    return []


def normalize_creator(value: Any) -> str:
    if isinstance(value, dict):
        return str(first_nonempty(value, ("name", "displayName", "username"), "Unknown"))
    if value is None:
        return "Unknown"
    return str(value)


def normalize_model_items(payload: Dict[str, Any], limit: int) -> List[Dict[str, Any]]:
    items = find_asset_list(payload)
    models: List[Dict[str, Any]] = []
    seen = set()
    for item in items:
        asset = item.get("asset") if isinstance(item.get("asset"), dict) else item
        asset_id = first_nonempty(asset, ("id", "assetId", "assetID", "asset_id"))
        if asset_id is None:
            asset_id = first_nonempty(item, ("id", "assetId", "assetID", "asset_id"))
        try:
            asset_id = int(asset_id)
        except Exception:
            continue
        if asset_id <= 0 or asset_id in seen:
            continue
        type_id = first_nonempty(asset, ("typeId", "assetTypeId", "assetType"))
        if isinstance(type_id, int) and type_id != 10:
            continue
        type_text = str(type_id or "").upper()
        if type_text and "MODEL" not in type_text and type_text != "10":
            continue
        name = str(first_nonempty(asset, ("name", "displayName", "title"), f"Model {asset_id}"))
        creator_value = asset.get("creator", item.get("creator"))
        creator = normalize_creator(creator_value)
        description = str(first_nonempty(asset, ("description", "summary"), ""))[:500]
        models.append({
            "id": asset_id,
            "name": name,
            "creator": creator,
            "description": description,
            "thumbnail": "",
        })
        seen.add(asset_id)
        if len(models) >= limit:
            break
    return models


def fetch_model_thumbnails(asset_ids: List[int]) -> Dict[int, str]:
    if not asset_ids:
        return {}
    response = requests.get(
        "https://thumbnails.roblox.com/v1/assets",
        params={
            "assetIds": ",".join(str(v) for v in asset_ids[:100]),
            "size": "420x420",
            "format": "Png",
            "isCircular": "false",
        },
        headers={"Accept": "application/json", "User-Agent": "NamelessTools/1.0"},
        timeout=(7, ROBLOX_HTTP_TIMEOUT),
    )
    if response.status_code >= 400:
        return {}
    try:
        data = response.json().get("data", [])
    except Exception:
        return {}
    result: Dict[int, str] = {}
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, dict):
            continue
        try:
            target_id = int(item.get("targetId"))
        except Exception:
            continue
        image_url = item.get("imageUrl")
        if isinstance(image_url, str) and image_url.startswith("https://"):
            result[target_id] = image_url
    return result


def search_creator_store_models(query: str, limit: int) -> List[Dict[str, Any]]:
    if not ROBLOX_OPEN_CLOUD_KEY:
        raise RuntimeError(
            "Creator Store search is not configured. Set ROBLOX_OPEN_CLOUD_KEY in Render Environment."
        )

    page_size = max(1, min(100, int(limit)))
    endpoint = "https://apis.roblox.com/toolbox-service/v2/assets:search"
    search_params = {
        "searchCategoryType": "Model",
        "maxPageSize": page_size,
        "query": query,
    }
    response = requests.get(
        endpoint,
        params=search_params,
        headers={
            "Accept": "application/json",
            "x-api-key": ROBLOX_OPEN_CLOUD_KEY,
            "User-Agent": "NamelessTools/1.0",
        },
        timeout=(7, ROBLOX_HTTP_TIMEOUT),
        allow_redirects=False,
    )

    if response.status_code in (301, 302, 303, 307, 308):
        location = response.headers.get("Location", "")
        if not location.startswith("https://apis.roblox.com/"):
            raise RuntimeError("Roblox returned an unsafe redirect")
        response = requests.get(
            location,
            headers={
                "Accept": "application/json",
                "x-api-key": ROBLOX_OPEN_CLOUD_KEY,
                "User-Agent": "NamelessTools/1.0",
            },
            timeout=(7, ROBLOX_HTTP_TIMEOUT),
            allow_redirects=False,
        )

    if response.status_code in (401, 403):
        raise RuntimeError(
            "Open Cloud key was rejected. Check the key and its Creator Store permissions."
        )
    if response.status_code >= 400:
        message = response.text[:500].strip()
        raise RuntimeError(
            f"Roblox returned HTTP {response.status_code}: {message or 'request failed'} "
            f"[build={APP_VERSION}; method=GET; category=Model]"
        )

    try:
        payload = response.json()
    except Exception as e:
        raise RuntimeError("Roblox returned invalid JSON") from e
    if not isinstance(payload, dict):
        raise RuntimeError("Unexpected Roblox response")

    models = normalize_model_items(payload, limit)
    thumbs = fetch_model_thumbnails([item["id"] for item in models])
    for item in models:
        item["thumbnail"] = thumbs.get(item["id"], "")
    return models


def read_limited_response(response: requests.Response) -> bytes:
    declared = response.headers.get("Content-Length")
    if declared:
        try:
            if int(declared) > ROBLOX_MAX_MODEL_BYTES:
                raise RuntimeError("Model is too large")
        except ValueError:
            pass
    chunks = []
    total = 0
    for chunk in response.iter_content(chunk_size=65536):
        if not chunk:
            continue
        total += len(chunk)
        if total > ROBLOX_MAX_MODEL_BYTES:
            raise RuntimeError("Model is too large")
        chunks.append(chunk)
    return b"".join(chunks)


def trusted_roblox_location(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    host = (parsed.hostname or "").lower()
    return parsed.scheme == "https" and (
        host == "roblox.com"
        or host.endswith(".roblox.com")
        or host == "rbxcdn.com"
        or host.endswith(".rbxcdn.com")
        or host.endswith(".robloxusercontent.com")
    )


def find_download_location(payload: Any) -> Optional[str]:
    if isinstance(payload, dict):
        for key in ("location", "url", "downloadUrl", "downloadURL"):
            value = payload.get(key)
            if isinstance(value, str) and trusted_roblox_location(value):
                return value
        for value in payload.values():
            found = find_download_location(value)
            if found:
                return found
    elif isinstance(payload, list):
        for value in payload:
            found = find_download_location(value)
            if found:
                return found
    return None


def classify_model_bytes(raw: bytes) -> Tuple[str, str]:
    head = raw[:256].lstrip()
    lower = head.lower()
    if head.startswith(b"<roblox!"):
        return ".rbxm", "application/octet-stream"
    if lower.startswith(b"<?xml") or lower.startswith(b"<roblox"):
        return ".rbxmx", "application/xml"
    if lower.startswith(b"{") or lower.startswith(b"["):
        raise RuntimeError("Roblox returned JSON instead of a model file")
    if b"<html" in lower or lower.startswith(b"<!doctype"):
        raise RuntimeError("Roblox returned an HTML error page")
    raise RuntimeError("The downloaded asset is not a recognized RBXM/RBXMX model")


def fetch_model_file(asset_id: int) -> Tuple[bytes, str, str]:
    candidates = []
    if ROBLOX_OPEN_CLOUD_KEY:
        candidates.append((
            f"https://apis.roblox.com/asset-delivery-api/v1/assetId/{asset_id}",
            roblox_headers(),
        ))
    candidates.extend([
        (f"https://assetdelivery.roblox.com/v2/assetId/{asset_id}", {"User-Agent": "NamelessTools/1.0"}),
        (f"https://assetdelivery.roblox.com/v1/assetId/{asset_id}", {"User-Agent": "NamelessTools/1.0"}),
    ])
    errors = []
    for url, headers in candidates:
        try:
            response = requests.get(
                url,
                headers=headers,
                timeout=(7, ROBLOX_HTTP_TIMEOUT),
                allow_redirects=True,
                stream=True,
            )
            if response.status_code >= 400:
                errors.append(f"HTTP {response.status_code}")
                response.close()
                continue
            content_type = response.headers.get("Content-Type", "").lower()
            raw = read_limited_response(response)
            response.close()
            if "json" in content_type or raw[:1] in (b"{", b"["):
                try:
                    payload = json.loads(raw.decode("utf-8", errors="replace"))
                except Exception:
                    payload = None
                location = find_download_location(payload)
                if location:
                    follow = requests.get(
                        location,
                        headers={"User-Agent": "NamelessTools/1.0"},
                        timeout=(7, ROBLOX_HTTP_TIMEOUT),
                        allow_redirects=True,
                        stream=True,
                    )
                    if follow.status_code >= 400:
                        errors.append(f"CDN HTTP {follow.status_code}")
                        follow.close()
                        continue
                    raw = read_limited_response(follow)
                    follow.close()
            extension, mime = classify_model_bytes(raw)
            return raw, extension, mime
        except Exception as e:
            errors.append(str(e))
    message = errors[-1] if errors else "Roblox did not return the model"
    raise RuntimeError(message)


def safe_download_name(asset_id: int, extension: str) -> str:
    return f"roblox_model_{asset_id}{extension}"


@app.route("/admin/login", methods=["POST"])
def admin_login():
    if not admin_authorized():
        time.sleep(0.15)
        return json_error("Bad admin key", 403)
    return jsonify({
        "ok": True,
        "version": APP_VERSION,
        "roblox_key_configured": bool(ROBLOX_OPEN_CLOUD_KEY),
    })


@app.route("/admin/models/search", methods=["GET"])
def admin_models_search():
    denied = require_admin_response()
    if denied:
        return denied
    query = re.sub(r"\s+", " ", request.args.get("q", "")).strip()[:80]
    if len(query) < 2:
        return json_error("Enter at least 2 characters", 400)
    limit = read_int_arg(request.args, "limit", 24, 1, 30)
    if not ROBLOX_OPEN_CLOUD_KEY:
        return json_error(
            "Creator Store search requires ROBLOX_OPEN_CLOUD_KEY in Render Environment.",
            503,
            {"setup_required": True},
        )
    try:
        models = search_creator_store_models(query, limit)
    except Exception as e:
        return json_error("Roblox search failed: " + str(e), 502)
    response = jsonify({
        "ok": True,
        "query": query,
        "models": models,
        "count": len(models),
        "version": APP_VERSION,
        "upstream_query_parameter": "query",
    })
    response.headers["Cache-Control"] = "no-store, max-age=0"
    return response


@app.route("/admin/models/download/<asset_id>", methods=["GET"])
def admin_models_download(asset_id: str):
    denied = require_admin_response()
    if denied:
        return denied
    if not asset_id.isdigit():
        return json_error("Bad asset ID", 400)
    numeric_id = int(asset_id)
    if numeric_id <= 0:
        return json_error("Bad asset ID", 400)
    try:
        raw, extension, mime = fetch_model_file(numeric_id)
    except Exception as e:
        return json_error("Roblox download failed: " + str(e), 502)
    filename = safe_download_name(numeric_id, extension)
    response = Response(raw, status=200, mimetype=mime)
    response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    response.headers["Content-Length"] = str(len(raw))
    response.headers["Cache-Control"] = "private, no-store, max-age=0"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@app.route("/version", methods=["GET"])
def version():
    return jsonify({
        "ok": True,
        "version": APP_VERSION,
        "search_endpoint": "/toolbox-service/v2/assets:search",
        "search_method": "GET",
        "search_query_template": "?searchCategoryType=Model&maxPageSize=24&query=castle",
        "search_category_type": "Model",
        "model_asset_type": 10,
        "open_cloud_key_configured": bool(ROBLOX_OPEN_CLOUD_KEY),
    })


@app.after_request
def add_version_header(response):
    response.headers["X-App-Version"] = APP_VERSION
    return response


# ---------- routes ----------
@app.route("/", methods=["GET"])
def index():
    port = request.args.get("port", "").strip()
    status_port = ""
    if port:
        try:
            status_port = clean_port(port)
        except Exception:
            status_port = ""
    return render_template_string(
        HTML,
        port=status_port,
        show_key=bool(IMAGE_KEY),
        abs_max_res=ABS_MAX_RES,
        video_max_res=VIDEO_MAX_RES,
        video_max_frames=VIDEO_MAX_FRAMES,
        app_version=APP_VERSION,
    )


@app.route("/image/meta", methods=["GET"])
def image_meta():
    if IMAGE_KEY and not check_key(request):
        return json_error("Bad key", 403)
    port = clean_port(request.args.get("port", ""))
    settings = load_image_settings(port)
    latest = load_cached_latest(port)
    image_ready = bool(latest.get("ok"))
    summary = None
    if image_ready:
        summary = f"Image ready: {latest.get('width')}x{latest.get('height')} · rects {latest.get('rect_count')}"
    else:
        summary = "No image uploaded on this port yet."
    resp = jsonify({
        "ok": True,
        "type": "image_meta",
        "port": port,
        "settings": settings,
        "image_ready": image_ready,
        "summary": summary,
    })
    resp.headers["Cache-Control"] = "no-store, max-age=0"
    return resp


@app.route("/image/upload", methods=["POST"])
def image_upload():
    if not check_key(request):
        return json_error("Bad key", 403)
    if "image" not in request.files:
        return json_error("No image", 400)
    port = clean_port(request.form.get("port", ""))
    max_side = read_int_arg(request.form, "max_side", DEFAULT_RES, 8, ABS_MAX_RES)
    color_step = read_int_arg(request.form, "color_step", DEFAULT_COLOR_STEP, 4, 64)
    raw = request.files["image"].read()
    if not raw:
        return json_error("Empty image", 400)
    save_original(port, raw)
    settings = save_image_settings(port, max_side, color_step)
    try:
        data = reprocess_image_for_port(port, settings["max_side"], settings["color_step"])
        if not data.get("ok"):
            return jsonify(data)
        return jsonify({
            "ok": True,
            "type": "image_upload",
            "port": port,
            "image": data,
            "settings": settings,
            "summary": f"Image ready: {data.get('width')}x{data.get('height')} · rects {data.get('rect_count')}",
        })
    except Exception as e:
        return json_error("Saved, conversion failed: " + str(e), 200, {"port": port})


@app.route("/upload", methods=["POST"])
def upload_legacy():
    return image_upload()


@app.route("/image/settings", methods=["POST"])
def image_settings_update():
    if not check_key(request):
        return json_error("Bad key", 403)
    body = read_json_body()
    port = clean_port(body.get("port", ""))
    max_side = max(8, min(ABS_MAX_RES, int(body.get("max_side", DEFAULT_RES))))
    color_step = max(4, min(64, int(body.get("color_step", DEFAULT_COLOR_STEP))))
    settings = save_image_settings(port, max_side, color_step)
    result = reprocess_image_for_port(port, settings["max_side"], settings["color_step"])
    image_ready = bool(result.get("ok"))
    summary = result.get("error") if not image_ready else f"Image updated: {result.get('width')}x{result.get('height')} · rects {result.get('rect_count')}"
    return jsonify({
        "ok": True,
        "type": "image_settings",
        "port": port,
        "settings": settings,
        "image_ready": image_ready,
        "summary": summary,
    })


@app.route("/latest", methods=["GET"])
def latest():
    if IMAGE_KEY and not check_key(request):
        return json_error("Bad key", 403)
    port = clean_port(request.args.get("port", ""))
    settings = load_image_settings(port)
    if "max_w" in request.args or "max_h" in request.args or "color_step" in request.args:
        max_w = read_int_arg(request.args, "max_w", settings["max_side"], 8, ABS_MAX_RES)
        max_h = read_int_arg(request.args, "max_h", settings["max_side"], 8, ABS_MAX_RES)
        color_step = read_int_arg(request.args, "color_step", settings["color_step"], 4, 64)
    else:
        max_w = settings["max_side"]
        max_h = settings["max_side"]
        color_step = settings["color_step"]
    try:
        data = load_original_as_rects(port, max_w, max_h, color_step)
    except Exception as e:
        data = load_cached_latest(port)
        data["warning"] = "Cached"
        data["server_error"] = str(e)
    if data is None:
        data = load_cached_latest(port)
    if data.get("ok"):
        data["settings"] = settings
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
    index = read_int_arg(request.args, "index", 0, 0, max(0, int(meta.get("frame_count", 1)) - 1))
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
    start = read_int_arg(request.args, "start", 0, 0, frame_count - 1)
    count = read_int_arg(request.args, "count", 4, 1, VIDEO_MAX_CHUNK_FRAMES)
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
    for path in (port_file(port), original_file(port), image_settings_file(port)):
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
        "service": "image-video-painter",
        "abs_max_res": ABS_MAX_RES,
        "video_max_res": VIDEO_MAX_RES,
        "video_max_frames": VIDEO_MAX_FRAMES,
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)

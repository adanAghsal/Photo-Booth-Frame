from __future__ import annotations

import os
import io
import time
import base64
import copy
import json
import secrets
import sqlite3
import threading
import subprocess
import shutil
import re
from contextlib import closing
from typing import Any
from urllib.parse import quote, unquote, urlsplit

import cv2
import numpy as np
from PIL import Image
import qrcode
from flask import Flask, render_template, request, jsonify, send_from_directory, abort, url_for

# =========================
# Paths / Config
# =========================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

APP_PORT = 8000
OUTPUT_DIR = os.path.join(BASE_DIR, "output")
FRAME_DIR = os.path.join(BASE_DIR, "frames")
FALLBACK_FRAME_PATH = os.path.join(BASE_DIR, "frame.png")  # optional
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").strip().rstrip("/")
APP_BASE_PATH = os.environ.get("APP_BASE_PATH", "").strip().strip("/")
APP_BASE_PATH = f"/{APP_BASE_PATH}" if APP_BASE_PATH else ""
PUBLIC_PHOTO_FILENAME_RE = re.compile(r"photo_[0-9a-f]{64}\.jpg", re.ASCII)
SESSION_ID_RE = re.compile(r"[A-Za-z0-9_-]{43}", re.ASCII)
SESSION_FILE_RE = re.compile(
    r"(?:photo_[0-9a-f]{64}\.(?:jpg|png)|shot_[0-9]{2}\.jpg)",
    re.ASCII,
)

os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(os.path.join(OUTPUT_DIR, "sessions"), exist_ok=True)

JPEG_QUALITY_SAVE = 95

# Green key (slot detection + knock-out alpha) kalau frame slot-nya hijau
GREEN_KEY_HEX = "#01e678"


app = Flask(__name__, template_folder=os.path.join(BASE_DIR, "templates"))

ACTIVE_FRAME: str | None = None


def _app_path(path: str) -> str:
    return f"{APP_BASE_PATH}{path}"


# =========================
# SQLite-backed sessions with a bounded in-memory cache
# =========================
SESSION_JOBS: dict[str, dict[str, Any]] = {}
SESSION_LOCK = threading.Lock()
SESSION_MAX = 300
SESSION_DB_PATH = os.path.join(OUTPUT_DIR, "photobooth.sqlite3")


def _session_job_json(job: dict[str, Any]) -> str:
    stored_job = copy.deepcopy(job)
    stored_job.pop("drive", None)
    stored_job.pop("access_token", None)
    stored_job.pop("access_token_hash", None)
    stored_job.pop("email", None)
    stored_job.pop("participant", None)
    local = stored_job.get("local")
    if isinstance(local, dict):
        local.pop("photo_qr_data_url", None)
        local.pop("session_qr_data_url", None)
    return json.dumps(stored_job, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _restore_session_qr_data(job: dict[str, Any], session_id: str) -> None:
    job.pop("drive", None)
    job.pop("access_token", None)
    job.pop("access_token_hash", None)
    job.pop("email", None)
    job.pop("participant", None)
    local = job.get("local")
    if not isinstance(local, dict):
        return
    if not local.get("whatsapp_photo_filename") and local.get("whatsapp_photo_view_url"):
        local["whatsapp_photo_filename"] = unquote(
            os.path.basename(urlsplit(local["whatsapp_photo_view_url"]).path)
        )
    if not local.get("session_view_url") and local.get("photo_view_url"):
        photo_url = urlsplit(local["photo_view_url"])
        if photo_url.scheme and photo_url.netloc:
            local["session_view_url"] = (
                f"{photo_url.scheme}://{photo_url.netloc}/session-results/{quote(session_id)}"
            )
    if local.get("session_view_url"):
        local["session_qr_data_url"] = make_qr_data_url(local["session_view_url"])


def _init_session_db() -> None:
    os.makedirs(os.path.dirname(SESSION_DB_PATH), exist_ok=True)
    with closing(sqlite3.connect(SESSION_DB_PATH, timeout=10)) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS session_jobs (
                session_id TEXT PRIMARY KEY,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                data_json TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_session_jobs_updated_at "
            "ON session_jobs(updated_at)"
        )
        connection.commit()


def _write_session_job(session_id: str, job: dict[str, Any]) -> None:
    now = time.time()
    created_at = float(job.get("created_at", now))
    data_json = _session_job_json(job)
    with closing(sqlite3.connect(SESSION_DB_PATH, timeout=10)) as connection:
        connection.execute(
            """
            INSERT INTO session_jobs (session_id, created_at, updated_at, data_json)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                created_at = excluded.created_at,
                updated_at = excluded.updated_at,
                data_json = excluded.data_json
            """,
            (session_id, created_at, now, data_json),
        )
        connection.commit()


def _persist_session_locked(session_id: str) -> None:
    job = SESSION_JOBS.get(session_id)
    if job is not None:
        _write_session_job(session_id, job)


def _prune_sessions_locked(protected_session_id: str | None = None) -> None:
    while len(SESSION_JOBS) > SESSION_MAX:
        candidates = [
            session_id
            for session_id in SESSION_JOBS
            if session_id != protected_session_id
        ]
        if not candidates:
            return
        oldest = min(
            candidates,
            key=lambda session_id: SESSION_JOBS[session_id].get("created_at", 0),
        )
        SESSION_JOBS.pop(oldest, None)


def _get_session_job_locked(session_id: str) -> dict[str, Any] | None:
    job = SESSION_JOBS.get(session_id)
    if job is not None:
        return job

    with closing(sqlite3.connect(SESSION_DB_PATH, timeout=10)) as connection:
        row = connection.execute(
            "SELECT data_json FROM session_jobs WHERE session_id = ?",
            (session_id,),
        ).fetchone()
    if row is None:
        return None

    job = json.loads(row[0])
    if not isinstance(job, dict):
        raise ValueError(f"Session record '{session_id}' is not a JSON object")
    _restore_session_qr_data(job, session_id)
    SESSION_JOBS[session_id] = job
    _prune_sessions_locked(protected_session_id=session_id)
    return job


def _load_recent_sessions() -> None:
    with closing(sqlite3.connect(SESSION_DB_PATH, timeout=10)) as connection:
        rows = connection.execute(
            """
            SELECT session_id, data_json
            FROM session_jobs
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (SESSION_MAX,),
        ).fetchall()

    with SESSION_LOCK:
        SESSION_JOBS.clear()
        for session_id, data_json in reversed(rows):
            job = json.loads(data_json)
            if not isinstance(job, dict):
                raise ValueError(f"Session record '{session_id}' is not a JSON object")
            _restore_session_qr_data(job, session_id)
            SESSION_JOBS[session_id] = job


def _prune_sessions() -> None:
    with SESSION_LOCK:
        _prune_sessions_locked()


# =========================
# QR helper
# =========================
def make_qr_data_url(url: str) -> str:
    qr_img = qrcode.make(url).convert("RGB")
    buf = io.BytesIO()
    qr_img.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
    return f"data:image/png;base64,{b64}"


# =========================
# Utils
# =========================
def hex_to_rgb_tuple(hex_color: str):
    s = hex_color.strip().lstrip("#")
    if len(s) != 6:
        raise ValueError("Hex color must be 6 chars, e.g. #01e678")
    return int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)


def cover_resize_center_crop(
    img: Image.Image,
    target_w: int,
    target_h: int,
    position: tuple[float, float] = (0.5, 0.5),
    zoom: float = 1.0,
) -> Image.Image:
    iw, ih = img.size
    scale = max(target_w / max(1, iw), target_h / max(1, ih)) * zoom
    nw, nh = int(iw * scale), int(ih * scale)
    resized = img.resize((nw, nh), Image.LANCZOS)
    left = round((nw - target_w) * position[0])
    top = round((nh - target_h) * position[1])
    return resized.crop((left, top, left + target_w, top + target_h))


def dominant_frame_edge_color(frame_rgba: Image.Image) -> tuple[int, int, int]:
    rgba = np.array(frame_rgba.convert("RGBA"))
    height, width = rgba.shape[:2]
    opaque = rgba[..., 3] >= 200

    for fraction in (0.08, 0.16, 0.28, 0.45):
        band = max(1, round(min(width, height) * fraction))
        edge = np.zeros((height, width), dtype=bool)
        edge[:band, :] = True
        edge[-band:, :] = True
        edge[:, :band] = True
        edge[:, -band:] = True
        colors = rgba[edge & opaque, :3]
        if not len(colors):
            continue

        color_bins = colors >> 4
        unique_bins, counts = np.unique(color_bins, axis=0, return_counts=True)
        dominant_bin = unique_bins[int(np.argmax(counts))]
        dominant_colors = colors[np.all(color_bins == dominant_bin, axis=1)]
        return tuple(int(np.median(dominant_colors[:, channel])) for channel in range(3))

    raise ValueError("Frame tidak memiliki warna tepi opaque untuk versi WhatsApp.")


def flatten_rgba_on_color(image: Image.Image, color: tuple[int, int, int]) -> Image.Image:
    background = Image.new("RGBA", image.size, (*color, 255))
    return Image.alpha_composite(background, image.convert("RGBA")).convert("RGB")


# =========================
# Slot detection (alpha OR green)
# =========================
def detect_holes_from_alpha(frame_rgba: Image.Image, alpha_thresh: int = 32):
    W, H = frame_rgba.size
    alpha = np.array(frame_rgba.getchannel("A"))

    holes_bin = (alpha < alpha_thresh).astype(np.uint8) * 255
    k = np.ones((5, 5), np.uint8)
    holes_bin = cv2.morphologyEx(holes_bin, cv2.MORPH_CLOSE, k, iterations=2)

    contours, _ = cv2.findContours(holes_bin, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    holes = []
    for cnt in contours:
        x, y, w, h = cv2.boundingRect(cnt)
        area = w * h
        if area < 0.02 * W * H:
            continue

        touches = (x <= 1) or (y <= 1) or (x + w >= W - 1) or (y + h >= H - 1)
        if touches and area > 0.65 * W * H:
            continue

        cnt_local = cnt.copy()
        cnt_local[:, 0, 0] -= x
        cnt_local[:, 0, 1] -= y
        mask_local = np.zeros((h, w), dtype=np.uint8)
        cv2.drawContours(mask_local, [cnt_local], -1, 255, thickness=-1)

        cx = (x + w / 2) / float(W)
        cy = (y + h / 2) / float(H)

        holes.append({
            "bbox_px": (x, y, x + w, y + h),
            "mask": Image.fromarray(mask_local, mode="L"),
            "area": area,
            "cx": cx,
            "cy": cy,
        })

    if not holes:
        count, labels, stats, _ = cv2.connectedComponentsWithStats(holes_bin, connectivity=4)
        for label in range(1, count):
            x, y, w, h, _ = (int(value) for value in stats[label])
            area = w * h
            if area < 0.02 * W * H:
                continue

            touches = (x <= 1) or (y <= 1) or (x + w >= W - 1) or (y + h >= H - 1)
            if touches and area > 0.65 * W * H:
                continue

            mask_local = (labels[y:y + h, x:x + w] == label).astype(np.uint8) * 255
            holes.append({
                "bbox_px": (x, y, x + w, y + h),
                "mask": Image.fromarray(mask_local, mode="L"),
                "area": area,
                "cx": (x + w / 2) / float(W),
                "cy": (y + h / 2) / float(H),
            })

    holes = sorted(holes, key=lambda h: (h["cy"], h["cx"]))
    return holes


def detect_holes_from_green(frame_rgba: Image.Image, key_hex: str = GREEN_KEY_HEX, tol: int = 40):
    W, H = frame_rgba.size
    key = np.array(hex_to_rgb_tuple(key_hex), dtype=np.int16)

    arr = np.array(frame_rgba.convert("RGBA")).astype(np.int16)
    rgb = arr[..., :3]
    dist = np.linalg.norm(rgb - key, axis=2)

    mask = (dist <= tol).astype(np.uint8) * 255
    k = np.ones((9, 9), np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, k, iterations=2)

    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    holes = []
    for cnt in contours:
        x, y, w, h = cv2.boundingRect(cnt)
        area = w * h
        if area < 0.03 * W * H:
            continue

        cnt_local = cnt.copy()
        cnt_local[:, 0, 0] -= x
        cnt_local[:, 0, 1] -= y
        mask_local = np.zeros((h, w), dtype=np.uint8)
        cv2.drawContours(mask_local, [cnt_local], -1, 255, thickness=-1)

        cx = (x + w / 2) / float(W)
        cy = (y + h / 2) / float(H)

        holes.append({
            "bbox_px": (x, y, x + w, y + h),
            "mask": Image.fromarray(mask_local, mode="L"),
            "area": area,
            "cx": cx,
            "cy": cy,
        })

    holes = sorted(holes, key=lambda h: (h["cy"], h["cx"]))

    # knock out alpha in green areas
    out_arr = np.array(frame_rgba.convert("RGBA")).astype(np.uint8)
    full = np.zeros((H, W), dtype=np.uint8)
    cv2.drawContours(full, contours, -1, 255, thickness=-1)
    out_arr[full == 255, 3] = 0
    out_arr[full == 255, 0:3] = 0
    knocked = Image.fromarray(out_arr, mode="RGBA")

    return holes, knocked


def detect_slots_auto(frame_rgba: Image.Image):
    holes = detect_holes_from_alpha(frame_rgba, alpha_thresh=32)
    if holes:
        return holes, frame_rgba
    holes2, knocked = detect_holes_from_green(frame_rgba, key_hex=GREEN_KEY_HEX, tol=40)
    return holes2, knocked


# =========================
# Compose photo
# =========================
def compose_strip_photo(
    frame_rgba: Image.Image,
    shots_rgb: list[Image.Image],
    holes,
    crop_positions: list[tuple[float, float]] | None = None,
    crop_zooms: list[float] | None = None,
):
    W, H = frame_rgba.size
    base = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    if crop_positions is None:
        crop_positions = [(0.5, 0.5)] * len(shots_rgb)
    if crop_zooms is None:
        crop_zooms = [1.0] * len(shots_rgb)

    if len(holes) > 1:
        for i, hinfo in enumerate(holes):
            x1, y1, x2, y2 = hinfo["bbox_px"]
            tile = cover_resize_center_crop(
                shots_rgb[i],
                x2 - x1,
                y2 - y1,
                crop_positions[i],
                crop_zooms[i],
            ).convert("RGBA")
            base.paste(tile, (x1, y1), hinfo["mask"])
        return Image.alpha_composite(base, frame_rgba)

    if len(shots_rgb) > 1:
        x1, y1, x2, y2 = holes[0]["bbox_px"]
        sw, sh = x2 - x1, y2 - y1
        gap = max(4, min(12, sw // 100))
        collage = Image.new("RGBA", (sw, sh), (0, 0, 0, 0))

        for i, shot in enumerate(shots_rgb):
            y0 = round(i * sh / len(shots_rgb))
            y_next = round((i + 1) * sh / len(shots_rgb))
            top = y0 + (gap // 2 if i else 0)
            bottom = y_next - (gap - gap // 2 if i < len(shots_rgb) - 1 else 0)
            tile = cover_resize_center_crop(
                shot, sw, bottom - top, crop_positions[i], crop_zooms[i]
            ).convert("RGBA")
            collage.paste(tile, (0, top))

        base.paste(collage, (x1, y1), holes[0]["mask"])
        return Image.alpha_composite(base, frame_rgba)

    for i, hinfo in enumerate(holes):
        if i >= len(shots_rgb):
            break
        x1, y1, x2, y2 = hinfo["bbox_px"]
        sw, sh = (x2 - x1), (y2 - y1)

        shot = cover_resize_center_crop(
            shots_rgb[i], sw, sh, crop_positions[i], crop_zooms[i]
        ).convert("RGBA")
        base.paste(shot, (x1, y1), hinfo["mask"])

    return Image.alpha_composite(base, frame_rgba)


# =========================
# Video compose + transcode
# =========================
def find_ffmpeg() -> str | None:
    ff = shutil.which("ffmpeg")
    if ff:
        return ff
    local = os.path.join(BASE_DIR, "_ffmpeg", "ffmpeg.exe")
    if os.path.exists(local):
        return local
    return None


def transcode_to_h264_mp4(in_path: str, out_path: str, fps: int = 20) -> str | None:
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        return None
    cmd = [
        ffmpeg, "-y",
        "-i", in_path,
        "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
        "-r", str(fps),
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        out_path
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return out_path


def compose_strip_live_video(frame_rgba: Image.Image, clip_paths: list[str], holes, out_path: str, fps_out: int = 20) -> str:
    W, H = frame_rgba.size
    caps = []
    try:
        for p in clip_paths:
            cap = cv2.VideoCapture(p)
            if not cap.isOpened():
                raise RuntimeError(f"Cannot open clip: {p}")
            caps.append(cap)

        counts = []
        for cap in caps:
            c = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
            counts.append(c if c > 0 else 999999)
        nframes = max(1, min(counts))

        # write mp4v first (fast), then try transcode
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        vw = cv2.VideoWriter(out_path, fourcc, fps_out, (W, H))
        if not vw.isOpened():
            out_path_avi = os.path.splitext(out_path)[0] + ".avi"
            fourcc = cv2.VideoWriter_fourcc(*"XVID")
            vw = cv2.VideoWriter(out_path_avi, fourcc, fps_out, (W, H))
            if not vw.isOpened():
                raise RuntimeError("VideoWriter cannot be opened (mp4/avi).")
            out_path = out_path_avi

        frame_rgba_local = frame_rgba.convert("RGBA")

        for _ in range(nframes):
            base = Image.new("RGBA", (W, H), (255, 255, 255, 255))

            for i, hinfo in enumerate(holes):
                if i >= len(caps):
                    break
                ok, fr = caps[i].read()
                if not ok or fr is None:
                    break

                fr_rgb = cv2.cvtColor(fr, cv2.COLOR_BGR2RGB)
                shot_img = Image.fromarray(fr_rgb, mode="RGB")

                x1, y1, x2, y2 = hinfo["bbox_px"]
                sw, sh = (x2 - x1), (y2 - y1)

                shot_fit = cover_resize_center_crop(shot_img, sw, sh).convert("RGBA")
                base.paste(shot_fit, (x1, y1), hinfo["mask"])

            comp = Image.alpha_composite(base, frame_rgba_local).convert("RGB")
            out_bgr = cv2.cvtColor(np.array(comp), cv2.COLOR_RGB2BGR)
            vw.write(out_bgr)

        vw.release()
        return out_path

    finally:
        for cap in caps:
            try:
                cap.release()
            except Exception:
                pass


# =========================
# Load frames
# =========================
FRAMES: dict[str, dict[str, Any]] = {}

def load_all_frames():
    global ACTIVE_FRAME
    FRAMES.clear()

    if os.path.isdir(FRAME_DIR):
        for fn in sorted(os.listdir(FRAME_DIR)):
            if not fn.lower().endswith(".png"):
                continue
            path = os.path.join(FRAME_DIR, fn)
            frame_id = os.path.splitext(fn)[0]
            img_raw = Image.open(path).convert("RGBA")

            holes, img = detect_slots_auto(img_raw)

            if len(holes) < 1:
                raise RuntimeError(
                    f"Frame '{fn}' gagal detect slot. Pastikan slot transparan atau pakai hijau key {GREEN_KEY_HEX}"
                )

            file_stat = os.stat(path)
            FRAMES[frame_id] = {
                "path": path,
                "img": img,
                "holes": holes,
                "preview_version": f"{file_stat.st_mtime_ns}-{file_stat.st_size}",
            }
            print(f"[OK] Frame '{frame_id}' slots={len(holes)} ({fn})")

    if not FRAMES and os.path.exists(FALLBACK_FRAME_PATH):
        img_raw = Image.open(FALLBACK_FRAME_PATH).convert("RGBA")
        holes, img = detect_slots_auto(img_raw)
        if len(holes) < 1:
            raise RuntimeError("Fallback frame.png gagal detect slot.")
        file_stat = os.stat(FALLBACK_FRAME_PATH)
        FRAMES["default"] = {
            "path": FALLBACK_FRAME_PATH,
            "img": img,
            "holes": holes,
            "preview_version": f"{file_stat.st_mtime_ns}-{file_stat.st_size}",
        }

    if not FRAMES:
        raise FileNotFoundError("No frames found. Put PNGs in frames/ or provide frame.png")

    ACTIVE_FRAME = list(FRAMES.keys())[0]
    print(f"[OK] Loaded frames: {list(FRAMES.keys())}. Active={ACTIVE_FRAME}")

load_all_frames()
_init_session_db()
_load_recent_sessions()


# =========================
# Routes
# =========================
@app.get("/")
def home():
    return render_template("index.html", app_base_path=APP_BASE_PATH)


@app.get("/assets/logo")
def logo_asset():
    return send_from_directory(os.path.join(BASE_DIR, "assets"), "LOGOS (3).png")


@app.get("/file/<path:filename>")
def file_view(filename):
    if not PUBLIC_PHOTO_FILENAME_RE.fullmatch(filename):
        abort(404)
    return send_from_directory(OUTPUT_DIR, filename, as_attachment=False)


@app.get("/session-file/<session_id>/<filename>")
def session_file_view(session_id, filename):
    if not SESSION_ID_RE.fullmatch(session_id) or not SESSION_FILE_RE.fullmatch(filename):
        abort(404)
    session_dir = os.path.join(OUTPUT_DIR, "sessions", session_id)
    download = request.args.get("download") == "1"
    return send_from_directory(
        session_dir,
        filename,
        as_attachment=download,
        download_name=filename if download else None,
    )


@app.get("/session-results/<session_id>")
def session_results(session_id):
    if not SESSION_ID_RE.fullmatch(session_id):
        abort(404)
    with SESSION_LOCK:
        stored_job = _get_session_job_locked(session_id)
        job = copy.deepcopy(stored_job) if stored_job is not None else None
    if job is None:
        abort(404)
    try:
        files = _session_gallery_files(session_id, job)
    except (FileNotFoundError, ValueError):
        abort(404)
    for item in files:
        item["view_url"] = _app_path(
            url_for(
                "session_file_view",
                session_id=session_id,
                filename=item["filename"],
            )
        )
        item["download_url"] = f'{item["view_url"]}?download=1'
    return render_template("session_results.html", files=files)


@app.get("/download/<path:filename>")
def download(filename):
    if not PUBLIC_PHOTO_FILENAME_RE.fullmatch(filename):
        abort(404)
    return send_from_directory(OUTPUT_DIR, filename, as_attachment=True, download_name=filename)


@app.get("/frame_preview/<path:frame_id>")
def frame_preview(frame_id):
    return _frame_image_response(frame_id)


@app.get("/frame_thumbnail/<path:frame_id>")
def frame_thumbnail(frame_id):
    return _frame_image_response(frame_id, (384, 576))


def _frame_image_response(frame_id: str, max_size: tuple[int, int] | None = None):
    frame = FRAMES.get(frame_id)
    if frame is None:
        abort(404)

    cache_key = "thumbnail_png" if max_size else "preview_png"
    image_bytes = frame.get(cache_key)
    if image_bytes is None:
        image = frame["img"]
        if max_size:
            image = image.copy()
            image.thumbnail(max_size, Image.Resampling.LANCZOS)
        preview = io.BytesIO()
        image.save(preview, format="PNG", optimize=bool(max_size))
        image_bytes = preview.getvalue()
        frame[cache_key] = image_bytes

    return app.response_class(
        image_bytes,
        mimetype="image/png",
        headers={"Cache-Control": "public, max-age=86400"},
    )


def _session_gallery_files(session_id: str, job: dict[str, Any]) -> list[dict[str, str]]:
    if not SESSION_ID_RE.fullmatch(session_id):
        raise ValueError("Invalid session id")
    local = job.get("local")
    if not isinstance(local, dict):
        raise ValueError("Session has no local output metadata")

    session_dir = os.path.abspath(os.path.join(OUTPUT_DIR, "sessions", session_id))
    files: list[dict[str, str]] = []

    def add_file(filename: str, label: str) -> None:
        if not isinstance(filename, str) or not SESSION_FILE_RE.fullmatch(filename):
            raise ValueError(f"Invalid session output filename: {filename}")
        path = os.path.abspath(os.path.join(session_dir, filename))
        if os.path.commonpath([session_dir, path]) != session_dir:
            raise ValueError("Session output path is outside its session directory")
        if not os.path.isfile(path):
            raise FileNotFoundError(f"Session output file is missing: {filename}")
        files.append({"filename": filename, "label": label, "path": path})

    photo_filename = local.get("photo_filename")
    if not isinstance(photo_filename, str):
        raise ValueError("Session has no composed photo")
    add_file(photo_filename, "Hasil gabungan")

    whatsapp_filename = local.get("whatsapp_photo_filename")
    if isinstance(whatsapp_filename, str) and whatsapp_filename != photo_filename:
        add_file(whatsapp_filename, "Hasil berlatar warna frame")

    slots = job.get("slots")
    if not isinstance(slots, int) or not 1 <= slots <= 6:
        raise ValueError("Session has an invalid photo count")
    for index in range(1, slots + 1):
        add_file(f"shot_{index:02d}.jpg", f"Foto satuan {index}")
    return files


@app.get("/api/frames")
def api_frames():
    assert ACTIVE_FRAME is not None
    return jsonify({
        "ok": True,
        "active": ACTIVE_FRAME,
        "active_slots": len(FRAMES[ACTIVE_FRAME]["holes"]),
        "frames": [
            {
                "id": k,
                "label": k,
                "slots": len(v["holes"]),
                "size": {
                    "width": v["img"].width,
                    "height": v["img"].height,
                },
                "preview_version": v["preview_version"],
                "slot_bounds": [
                    {
                        "x": hole["bbox_px"][0],
                        "y": hole["bbox_px"][1],
                        "width": hole["bbox_px"][2] - hole["bbox_px"][0],
                        "height": hole["bbox_px"][3] - hole["bbox_px"][1],
                    }
                    for hole in v["holes"]
                ],
            }
            for k, v in FRAMES.items()
        ]
    })


@app.post("/api/set_frame")
def api_set_frame():
    global ACTIVE_FRAME
    data = request.get_json(silent=True) or {}
    fid = (data.get("frame") or "").strip()
    if fid not in FRAMES:
        return jsonify({"ok": False, "error": "unknown frame", "frames": list(FRAMES.keys())}), 400
    ACTIVE_FRAME = fid
    return jsonify({"ok": True, "active": ACTIVE_FRAME, "slots": len(FRAMES[ACTIVE_FRAME]["holes"])})


@app.post("/api/compose")
def api_compose():
    frame_id = (request.form.get("frame", "") or "").strip() or ACTIVE_FRAME
    files = request.files.getlist("shots")
    if not files:
        return jsonify({"ok": False, "error": "No shots uploaded"}), 400
    if not 1 <= len(files) <= 6:
        return jsonify({"ok": False, "error": "Provide between 1 and 6 shots"}), 400
    crop_x_values = request.form.getlist("crop_x")
    crop_y_values = request.form.getlist("crop_y")
    crop_zoom_values = request.form.getlist("crop_zoom")
    if not crop_x_values and not crop_y_values:
        crop_positions = [(0.5, 0.5)] * len(files)
    elif len(crop_x_values) != len(files) or len(crop_y_values) != len(files):
        return jsonify({"ok": False, "error": "Provide crop positions for every shot"}), 400
    else:
        try:
            crop_positions = [
                (float(x), float(y))
                for x, y in zip(crop_x_values, crop_y_values)
            ]
        except ValueError:
            return jsonify({"ok": False, "error": "Crop positions must be numbers"}), 400
        if any(not 0 <= value <= 1 for position in crop_positions for value in position):
            return jsonify({"ok": False, "error": "Crop positions must be between 0 and 1"}), 400
    if crop_zoom_values and len(crop_zoom_values) != len(files):
        return jsonify({"ok": False, "error": "Provide a zoom value for every shot"}), 400
    try:
        crop_zooms = [float(value) for value in crop_zoom_values] if crop_zoom_values else [1.0] * len(files)
    except ValueError:
        return jsonify({"ok": False, "error": "Crop zoom values must be numbers"}), 400
    if any(not 1 <= zoom <= 3 for zoom in crop_zooms):
        return jsonify({"ok": False, "error": "Crop zoom values must be between 1 and 3"}), 400

    if frame_id not in FRAMES:
        frame_id = ACTIVE_FRAME
    assert frame_id is not None

    frame_rgba = FRAMES[frame_id]["img"]
    holes = FRAMES[frame_id]["holes"]
    if len(files) != len(holes):
        return jsonify({
            "ok": False,
            "error": f"This frame requires exactly {len(holes)} photos",
        }), 400

    shots = [Image.open(f.stream).convert("RGB") for f in files]
    need = len(shots)

    session_id = secrets.token_urlsafe(32)
    session_dir = os.path.join(OUTPUT_DIR, "sessions", session_id)
    os.makedirs(session_dir, exist_ok=False)

    for index, shot in enumerate(shots, start=1):
        shot_path = os.path.join(session_dir, f"shot_{index:02d}.jpg")
        shot.save(shot_path, format="JPEG", quality=JPEG_QUALITY_SAVE)

    final_rgba = compose_strip_photo(frame_rgba, shots, holes, crop_positions, crop_zooms)
    has_transparency = final_rgba.getchannel("A").getextrema()[0] < 255
    photo_extension = ".png" if has_transparency else ".jpg"
    photo_name = f"photo_{secrets.token_hex(32)}{photo_extension}"
    photo_path = os.path.join(session_dir, photo_name)
    if photo_extension == ".png":
        final_rgba.save(photo_path, format="PNG", optimize=True)
    else:
        final_rgba.convert("RGB").save(photo_path, format="JPEG", quality=JPEG_QUALITY_SAVE)

    if has_transparency:
        whatsapp_photo_name = f"photo_{secrets.token_hex(32)}.jpg"
        whatsapp_photo_path = os.path.join(session_dir, whatsapp_photo_name)
        edge_color = dominant_frame_edge_color(frame_rgba)
        whatsapp_photo = flatten_rgba_on_color(final_rgba, edge_color)
        whatsapp_photo.save(whatsapp_photo_path, format="JPEG", quality=JPEG_QUALITY_SAVE)
    else:
        whatsapp_photo_name = photo_name
        whatsapp_photo_path = photo_path

    public_base_url = PUBLIC_BASE_URL or request.host_url.rstrip("/")
    local_photo_view = (
        f"{public_base_url}/session-file/{quote(session_id)}/{quote(photo_name)}"
    )
    local_whatsapp_photo_view = (
        f"{public_base_url}/session-file/{quote(session_id)}/{quote(whatsapp_photo_name)}"
    )
    session_view_url = f"{public_base_url}/session-results/{quote(session_id)}"
    session_qr = make_qr_data_url(session_view_url)

    with SESSION_LOCK:
        SESSION_JOBS[session_id] = {
            "created_at": time.time(),
            "frame": frame_id,
            "slots": need,
            "local": {
                "photo_view_url": local_photo_view,
                "photo_filename": photo_name,
                "whatsapp_photo_filename": whatsapp_photo_name,
                "whatsapp_photo_view_url": local_whatsapp_photo_view,
                "session_view_url": session_view_url,
                "session_qr_data_url": session_qr,
            },
        }
        _persist_session_locked(session_id)
    _prune_sessions()

    return jsonify({
        "ok": True,
        "id": session_id,
        "local": {
            "photo_view_url": local_photo_view,
            "session_view_url": session_view_url,
            "session_qr_data_url": session_qr,
            "photo_filename": photo_name,
            "whatsapp_photo_filename": whatsapp_photo_name,
        },
        "frame": frame_id,
        "slots": need
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=APP_PORT, debug=False, threaded=True)

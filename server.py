#!/usr/bin/env python3
"""
vthumb NAS: WebUI + HTTP API + CLI for PotPlayer-style video contact sheets.

Features:
  - Web UI at / (single page, Apple-style dark/light toggle)
  - HTTP API:
      GET  /                       -> WebUI HTML
      GET  /static/<file>          -> static assets (css/js)
      GET  /api/browse?path=...    -> list folder
      POST /api/generate           -> generate thumbnails (single or batch)
      GET  /api/preview?v=...      -> single-video thumbnail (streamed JPEG)
      GET  /api/thumb/<path:url>   -> cached <video>.jpg
      WS   /ws/progress            -> real-time progress for batch jobs
  - CLI: thumb <folder> [--cols N --count N --width N --force --recurse]

Default browse root is `/mnt/media` (override via `BROWSE_ROOT` env var).
"""
from __future__ import annotations

import argparse
import asyncio
import io
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Iterable, Optional

from aiohttp import web, WSMsgType

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("vthumb")


# ── defaults / config ─────────────────────────────────────────────────────────

BROWSE_ROOT = Path(os.environ.get("BROWSE_ROOT", "/mnt/media")).resolve()
MEDIA_ROOT = Path(os.environ.get("MEDIA_ROOT", "/mnt/media")).resolve()

# Tiny single-frame thumbnails for the WebUI file list/grid.
# Keyed on (path, mtime, width) so changing the source video invalidates the cache.
SPRITE_CACHE_DIR = Path(os.environ.get(
    "SPRITE_CACHE_DIR",
    str(Path(__file__).parent / "cache" / "sprite"),
)).resolve()
SPRITE_CACHE_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_COUNT = int(os.environ.get("DEFAULT_COUNT", "16"))
DEFAULT_COLS = int(os.environ.get("DEFAULT_COLS", "4"))
DEFAULT_WIDTH = int(os.environ.get("DEFAULT_WIDTH", "1920"))
DEFAULT_THUMB_W = int(os.environ.get("DEFAULT_THUMB_W", "640"))
JPEG_QUALITY = int(os.environ.get("JPEG_QUALITY", "95"))
DEFAULT_LABEL_LANG = os.environ.get("DEFAULT_LABEL_LANG", "en")  # en | zh

VIDEO_EXTS = {
    ".3g2", ".3gp", ".asf", ".avi", ".divx", ".flv", ".m2ts", ".m4v", ".mkv",
    ".mov", ".mp4", ".mpeg", ".mpg", ".mts", ".ogm", ".ogv", ".rm", ".rmvb",
    ".ts", ".vob", ".webm", ".wmv",
}

# Label strings for the header. lang defaults to en, can be set to "zh".
LABELS = {
    "en": {
        "file": "File",
        "size": "Size",
        "resolution": "Resolution",
        "video": "Video",
        "audio": "Audio",
        "duration": "Duration",
    },
    "zh": {
        "file": "文件名",
        "size": "大小",
        "resolution": "分辨率",
        "video": "视频编码",
        "audio": "音频编码",
        "duration": "时长",
    },
}

# Theme constants used in the generated image:
SHEET_BG = (255, 255, 255)          # white canvas
HEADER_BG = (255, 255, 255)         # white header strip (no dark strip)
HEADER_FG = (255, 255, 255)         # white text
HEADER_OUTLINE = (0, 0, 0)          # black outline for legibility on white bg
BORDER = (60, 60, 60)
STAMP_BG = (0, 0, 0, 120)           # lighter translucent stamp bg (was 190)
STAMP_FG = (255, 255, 255)


# ── video info ───────────────────────────────────────────────────────────────

@dataclass
class VideoInfo:
    duration: float
    width: int
    height: int
    fps: float
    vcodec: str
    acodec: str

    @property
    def res_label(self) -> str:
        return f"{self.width}x{self.height}"

    @property
    def time_label(self) -> str:
        s = max(0, int(round(self.duration)))
        h, rem = divmod(s, 3600)
        m, s = divmod(rem, 60)
        return f"{h:02d}:{m:02d}:{s:02d}"


def _run(cmd: list[str], timeout: int = 30) -> str:
    log.info("EXEC %s", " ".join(cmd))
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError(f"cmd failed: {' '.join(cmd)} :: {p.stderr[-200:]}")
    return p.stdout


def probe(path: Path) -> VideoInfo:
    out = _run([
        "ffprobe", "-v", "error",
        "-print_format", "json",
        "-show_format", "-show_streams",
        str(path),
    ], timeout=30)
    data = json.loads(out)
    fmt = data.get("format", {})
    duration = float(fmt.get("duration", 0.0))

    streams = data.get("streams", [])
    vstream = next((s for s in streams if s.get("codec_type") == "video"), {})
    astream = next((s for s in streams if s.get("codec_type") == "audio"), {})

    if duration <= 0 and vstream.get("duration"):
        duration = float(vstream["duration"])

    width = int(vstream.get("width", 0))
    height = int(vstream.get("height", 0))

    fps_raw = vstream.get("avg_frame_rate", "0/1")
    try:
        num, den = fps_raw.split("/")
        fps = float(num) / float(den) if float(den) else 0.0
    except Exception:
        fps = 0.0

    return VideoInfo(
        duration=duration,
        width=width,
        height=height,
        fps=fps,
        vcodec=(vstream.get("codec_name") or "UNKNOWN").upper(),
        acodec=(astream.get("codec_name") or "NONE").upper(),
    )


# ── frame extraction + composition ──────────────────────────────────────────

def extract_frames(path: Path, info: VideoInfo, count: int, tile_w: int, tile_h: int) -> tuple[list[Path], list[float]]:
    """Extract count evenly-spaced frames scaled to tile_w (keeping aspect).
    Letterbox padding to (tile_w, tile_h) is added later in compose_sheet via
    Pillow, because ffmpeg's pad filter rejects inputs whose dimensions
    don't cleanly match. Returning only scaled (not padded) frames keeps the
    ffmpeg pipeline simple and lets Python handle the centering.
    """
    ts_list = [info.duration * (i + 1) / (count + 1) for i in range(count)]
    tmpdir = Path(tempfile.mkdtemp(prefix="vthumbs-"))
    outputs: list[Optional[Path]] = [None] * count
    try:
        for i, ts in enumerate(ts_list):
            out = tmpdir / f"{i:03d}.jpg"
            # Retry at earlier positions if the frame fails (e.g. corrupt tail)
            delays = [5.0, 30.0, 120.0, 300.0]
            ok = False
            for d in delays:
                seek_ts = max(0.0, ts - d)
                cmd = [
                    "ffmpeg", "-y", "-loglevel", "error",
                    "-ss", f"{seek_ts:.3f}",
                    "-i", str(path),
                    "-t", str(d + 10),
                    "-frames:v", "1",
                    "-vf", f"scale={tile_w}:-1",
                    "-q:v", "3",
                    str(out),
                ]
                try:
                    _run(cmd, timeout=30)
                except Exception:
                    continue
                if out.exists() and out.stat().st_size > 0:
                    ok = True
                    break
                # clean up empty file for retry
                try:
                    out.unlink()
                except OSError:
                    pass
            if ok:
                outputs[i] = out
        missing = [i for i, p in enumerate(outputs) if p is None or not p.exists()]
        if missing:
            raise RuntimeError(f"ffmpeg failed for {len(missing)} frames: {missing}")
        return [Path(p) for p in outputs], ts_list
    except Exception:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise


def _load_font(size: int):
    from PIL import ImageFont
    candidates = [
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
    ]
    for c in candidates:
        if os.path.exists(c):
            return ImageFont.truetype(c, size)
    return ImageFont.load_default()


def _text_width(draw, text, font):
    """Return pixel width of *text* when rendered with *font* on *draw*."""
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0]


def _draw_outlined(draw, text, font, x, y, fill, outline=(0, 0, 0), outline_w=3):
    """Draw text with outline (like Windows tool's Draw-OutlinedText)."""
    for dx in range(-outline_w, outline_w + 1):
        for dy in range(-outline_w, outline_w + 1):
            if dx == 0 and dy == 0:
                continue
            draw.text((x + dx, y + dy), text, fill=outline, font=font)
    draw.text((x, y), text, fill=fill, font=font)


def compose_sheet(
    frames: list[Path],
    timestamps: list[float],
    info: VideoInfo,
    src_path: Path,
    sheet_width: int,
    cols: int,
    lang: str = "en",
    show_info: bool = True,
) -> bytes:
    from PIL import Image, ImageDraw

    L = LABELS.get(lang, LABELS["en"])

    count = len(frames)
    rows = (count + cols - 1) // cols

    margin = 10
    gap = 8
    # Larger header font (was sheet_width // 85). Bigger so labels read
    # clearly at any device DPI. stamp_font also bumped for legibility.
    header_font_size = max(20, sheet_width // 55)
    stamp_font_size = max(18, sheet_width // 64)
    line_height = int(header_font_size * 1.35)

    # ── text measurement pass: wrap long filenames ──────────────────────────
    # Create a scratch image just for text measurement so we can compute the
    # real header height before allocating the final canvas.
    scratch = Image.new("RGB", (sheet_width, 100))
    scratch_draw = ImageDraw.Draw(scratch)
    hdr_font = _load_font(header_font_size)

    def _wrap(draw, text, font, max_w):
        """Split text into lines that fit within max_w (character by character)."""
        lines = []
        cur = ""
        for ch in text:
            test = cur + ch
            if _text_width(draw, test, font) > max_w and cur:
                lines.append(cur)
                cur = ch
            else:
                cur = test
        if cur:
            lines.append(cur)
        return lines

    max_text_w = sheet_width - 32  # 16 px padding on each side

    # Build label strings from L (lang) once
    file_label = f"{L['file']}: "
    size_label_text = f"{L['size']}: "
    res_label_text = f"{L['resolution']}: "
    video_label = L['video']
    audio_label = L['audio']
    duration_label = f"{L['duration']}: "

    # Wrap the filename line; the label+name can span multiple lines.
    full_name = f"{file_label}{src_path.name}"
    name_lines = _wrap(scratch_draw, full_name, hdr_font, max_text_w)

    # Bottom 4 lines are fixed.
    file_size = src_path.stat().st_size
    mb = file_size / (1024 * 1024)
    size_str = f"{size_label_text}{mb:.2f}MB ({file_size:,} bytes)"
    fps_str = f"{info.fps:.2f}" if info.fps else "?"
    res_str = f"{res_label_text}{info.res_label} ({fps_str} fps)"
    codec_str = f"{video_label}: {info.vcodec}    {audio_label}: {info.acodec}"
    dur_str = f"{duration_label}{info.time_label}"

    fixed_lines = [size_str, res_str, codec_str, dur_str]
    total_lines = len(name_lines) + len(fixed_lines)
    header_height = line_height * total_lines + 28

    # Also wrap the fixed lines if they exceed max width (unlikely but safe).
    all_lines = list(name_lines)
    for fl in fixed_lines:
        if _text_width(scratch_draw, fl, hdr_font) > max_text_w:
            all_lines.extend(_wrap(scratch_draw, fl, hdr_font, max_text_w))
        else:
            all_lines.append(fl)
    total_lines = len(all_lines)
    header_height = line_height * total_lines + 28

    scratch.close()  # release the measurement image

    tile_w = (sheet_width - margin * 2 - gap * (cols - 1)) // cols
    # Auto-detect orientation to pick cell height
    video_ratio = info.width / max(info.height, 1)
    if video_ratio >= 1:
        tile_h = int(round(tile_w * 9 / 16))   # landscape cell
    else:
        tile_h = int(round(tile_w * 16 / 9))   # portrait cell (vertical video)

    # Apply show_info toggle BEFORE sheet_height calc
    if not show_info:
        header_height = 0

    sheet_height = header_height + margin + tile_h * rows + gap * (rows - 1) + margin

    img = Image.new("RGB", (sheet_width, sheet_height), SHEET_BG)
    draw = ImageDraw.Draw(img)

    header_font = _load_font(header_font_size)
    stamp_font = _load_font(stamp_font_size)

    # Draw header text only if show_info
    if show_info:
        for i, line in enumerate(all_lines):
            _draw_outlined(draw, line, header_font, 16, 14 + i * line_height,
                           fill=HEADER_FG, outline=HEADER_OUTLINE, outline_w=2)

    for i, frame_path in enumerate(frames):
        row = i // cols
        col = i % cols
        x = margin + col * (tile_w + gap)
        y = header_height + margin + row * (tile_h + gap)

        # Always fill the cell with SHEET_BG (white) BEFORE pasting the
        # frame. The gap area between cells stays white because of this,
        # and any rounding in ffmpeg's scale or paste can't bleed into
        # adjacent cells.
        draw.rectangle([x, y, x + tile_w - 1, y + tile_h - 1], fill=SHEET_BG)

        with Image.open(frame_path) as fr:
            # Resize down to tile_w keeping aspect (Pillow honours aspect if
            # we only pass one size). The ffmpeg step already produced
            # exactly tile_w wide, so this should be a no-op in practice.
            fw, fh = fr.size
            if fw != tile_w:
                ratio = tile_w / fw
                fh = int(round(fh * ratio))
                fr = fr.resize((tile_w, fh), Image.LANCZOS)
            # Letterbox to tile_h (center vertically on a white tile).
            if fh < tile_h:
                # Black bars top and bottom of the frame, not white — that
                # way the actual picture area is visually distinguishable
                # from the white gutter. Use paste onto a black canvas.
                frame_canvas = Image.new("RGB", (tile_w, tile_h), (0, 0, 0))
                paste_y = (tile_h - fh) // 2
                frame_canvas.paste(fr, (0, paste_y))
                fr = frame_canvas
            elif fh > tile_h:
                # Source aspect is wider than 16:9 (unusual). Crop center.
                top = (fh - tile_h) // 2
                fr = fr.crop((0, top, tile_w, top + tile_h))
            else:
                pass
            img.paste(fr, (x, y))

        # 1-px grey border around each cell — clarifies the grid in light theme.
        draw.rectangle([x, y, x + tile_w - 1, y + tile_h - 1], outline=BORDER, width=1)

        ts_label = time.strftime(
            f"{int(timestamps[i]) // 3600:02d}:{int(timestamps[i]) % 3600 // 60:02d}:{int(timestamps[i]) % 60:02d}",
            time.gmtime(timestamps[i]),
        )
        bbox = draw.textbbox((0, 0), ts_label, font=stamp_font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        # Bottom-center: text centered horizontally. Push well up into the
        # frame area (8 px above the cell's bottom edge) so the stamp
        # sits on actual video content, not on the white gutter.
        pad_x, pad_y = 12, 6
        edge_margin = 12  # breathing room from cell bottom — anchors stamp to frame
        tx = x + (tile_w - tw) // 2
        ty = y + tile_h - th - pad_y - edge_margin
        _draw_outlined(draw, ts_label, stamp_font, tx, ty,
                       fill=STAMP_FG, outline=(0, 0, 0), outline_w=2)

    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=JPEG_QUALITY, optimize=True)
    return buf.getvalue()


def output_path_for(video: Path) -> Path:
    """Returns <video>.jpg (same dir, same stem, .jpg suffix)."""
    return video.with_name(video.stem + ".jpg")


def generate_one(video: Path, count: int, cols: int, width: int,
                 force: bool, lang: str = DEFAULT_LABEL_LANG,
                 show_info: bool = True) -> tuple[str, str]:
    out = output_path_for(video)
    if out.exists() and not force:
        return "skip", f"exists: {out.name}"
    try:
        info = probe(video)
    except Exception as e:
        return "err", f"probe failed: {e}"
    if info.duration <= 0:
        return "err", "duration=0 (corrupt or audio-only)"
    # Compute tile dimensions matching what compose_sheet will lay out,
    # so ffmpeg produces frames at exactly the right size (no paste overflow,
    # no letterbox collapse that visually merges adjacent cells).
    margin, gap = 10, 8
    tile_w = (width - margin * 2 - gap * (cols - 1)) // cols
    # Auto-detect orientation: landscape/wide → 16:9 cell, portrait → 9:16 cell
    video_ratio = info.width / max(info.height, 1)
    if video_ratio >= 1:
        tile_h = int(round(tile_w * 9 / 16))   # landscape
    else:
        tile_h = int(round(tile_w * 16 / 9))   # portrait (vertical video)
    try:
        frames, ts_list = extract_frames(video, info, count, tile_w, tile_h)
    except Exception as e:
        return "err", f"extract failed: {e}"
    try:
        jpeg = compose_sheet(frames, ts_list, info, video, width, cols, lang=lang, show_info=show_info)
        out.write_bytes(jpeg)
    except Exception as e:
        return "err", f"compose/write failed: {e}"
    finally:
        for f in frames:
            try:
                shutil.rmtree(f.parent, ignore_errors=True)
            except Exception:
                pass
    size_kb = len(jpeg) // 1024
    return "ok", f"{out.name}  ({size_kb} KB)"

# ── browse API ──────────────────────────────────────────────────────────────

def iter_videos(folder: Path, recursive: bool = False) -> Iterable[Path]:
    if recursive:
        yield from (p for p in folder.rglob("*")
                    if p.is_file() and p.suffix.lower() in VIDEO_EXTS)
    else:
        for p in sorted(folder.iterdir()):
            if p.is_file() and p.suffix.lower() in VIDEO_EXTS:
                yield p


def safe_resolve(p: str) -> Optional[Path]:
    """Resolve p under BROWSE_ROOT, ensuring no escape."""
    if not p:
        return None
    base = BROWSE_ROOT
    candidate = (base / p.lstrip("/")).resolve(strict=False)
    try:
        candidate.relative_to(base)
    except ValueError:
        return None
    return candidate


COVER_NAMES = ("folder.jpg", "poster.jpg", "cover.jpg", "backdrop.jpg", "fanart.jpg")
COVER_EXTS = (".jpg", ".jpeg", ".png", ".webp")


def find_local_cover(target: Path) -> Optional[str]:
    """Return the relative path to a cover image, or None.

    Videos only: look for `<stem>.jpg`, `<stem>-poster.jpg`, `<stem>.cover.jpg`
                 beside the video file. Never fall back to the parent directory's
                 folder.jpg — that would mismatch if two movies share a folder.

    Directories: always return None — folders render as a folder icon only.
    """
    if not target.is_file() or target.suffix.lower() not in VIDEO_EXTS:
        return None
    parent = target.parent
    stem = target.stem
    candidates: list[Path] = []
    for ext in COVER_EXTS:
        candidates.append(parent / f"{stem}{ext}")
    for n in (f"{stem}-poster.jpg", f"{stem}.folder.jpg", f"{stem}.cover.jpg"):
        candidates.append(parent / n)
    for c in candidates:
        if c.is_file():
            try:
                return str(c.relative_to(BROWSE_ROOT))
            except ValueError:
                continue
    return None


def browse(path: str) -> dict:
    target = safe_resolve(path) or BROWSE_ROOT
    target = target.resolve(strict=False)
    if not target.exists():
        target = BROWSE_ROOT

    entries = []
    try:
        items = sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
    except PermissionError:
        items = []

    for p in items:
        try:
            if p.is_dir():
                # Try to count videos inside (non-recursive, fast)
                try:
                    vids = sum(1 for q in p.iterdir()
                               if q.is_file() and q.suffix.lower() in VIDEO_EXTS)
                except Exception:
                    vids = 0
                entries.append({
                    "name": p.name,
                    "type": "dir",
                    "path": str(p.relative_to(BROWSE_ROOT)),
                    "video_count": vids,
                })
            elif p.is_file() and p.suffix.lower() in VIDEO_EXTS:
                entries.append({
                    "name": p.name,
                    "type": "video",
                    "path": str(p.relative_to(BROWSE_ROOT)),
                    "size": p.stat().st_size,
                    "cover": find_local_cover(p),
                })
        except Exception:
            continue

    rel = target.relative_to(BROWSE_ROOT)
    return {
        "path": str(rel) if str(rel) != "." else "",
        "parent": str(rel.parent) if rel.parent != rel and str(rel.parent) != "." else "",
        "entries": entries,
        "browse_root": str(BROWSE_ROOT),
    }


# ── HTTP handlers ───────────────────────────────────────────────────────────

WS_CLIENTS: set = set()


async def ws_broadcast(msg: dict):
    """Fan out progress messages to all WS clients."""
    dead = set()
    payload = json.dumps(msg)
    for ws in WS_CLIENTS:
        try:
            await ws.send_str(payload)
        except Exception:
            dead.add(ws)
    WS_CLIENTS.difference_update(dead)


async def handle_index(_request: web.Request) -> web.Response:
    html = (Path(__file__).parent / "templates" / "index.html").read_text(encoding="utf-8")
    return web.Response(
        text=html,
        content_type="text/html",
        headers={"Cache-Control": "no-cache, must-revalidate"},
    )


async def handle_static(request: web.Request) -> web.Response:
    name = request.match_info["name"]
    base = Path(__file__).parent / "static"
    target = (base / name).resolve()
    try:
        target.relative_to(base.resolve())
    except ValueError:
        return web.Response(status=403)
    if not target.is_file():
        return web.Response(status=404)
    ct = "text/css" if name.endswith(".css") else \
         "application/javascript" if name.endswith(".js") else \
         "image/svg+xml"
    return web.Response(text=target.read_text(encoding="utf-8") if not name.endswith((".png", ".jpg")) else target.read_bytes(),
                        content_type=ct)


async def handle_raw(request: web.Request) -> web.Response:
    """Serve a file from BROWSE_ROOT (jpg/png/webp cover images).

    Used by the WebUI to render pre-existing cover art (Jellyfin folder.jpg,
    etc.) without going through ffmpeg.
    """
    rel = request.query.get("p", "")
    rp = safe_resolve(rel)
    if rp is None or not rp.is_file():
        return web.Response(status=404)
    if rp.suffix.lower() not in (".jpg", ".jpeg", ".png", ".webp"):
        return web.Response(status=403)
    ext = rp.suffix.lower()
    ct = {".jpg": "image/jpeg", ".jpeg": "image/jpeg",
          ".png": "image/png", ".webp": "image/webp"}.get(ext, "application/octet-stream")
    return web.Response(
        body=rp.read_bytes(),
        content_type=ct,
        headers={"Cache-Control": "public, max-age=604800"},
    )


async def handle_browse(request: web.Request) -> web.Response:
    path = request.query.get("path", "")
    return web.json_response(browse(path))


async def handle_generate(request: web.Request) -> web.Response:
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)

    paths = data.get("paths", [])
    if isinstance(paths, str):
        paths = [paths]
    recursive = bool(data.get("recursive", False))
    force = bool(data.get("force", False))
    cols = int(data.get("cols", DEFAULT_COLS))
    count = int(data.get("count", DEFAULT_COUNT))
    width = int(data.get("width", DEFAULT_WIDTH))
    async_mode = bool(data.get("async", False))
    show_info = data.get("show_info", True)
    if isinstance(show_info, str):
        show_info = show_info.lower() not in ("false", "0", "no")
    else:
        show_info = bool(show_info)
    lang = data.get("lang", DEFAULT_LABEL_LANG)
    if lang not in LABELS:
        lang = "en"

    if not paths:
        return web.json_response({"error": "no paths"}, status=400)

    # Validate paths and expand folders
    targets: list[Path] = []
    for p in paths:
        rp = safe_resolve(p)
        if rp is None:
            return web.json_response({"error": f"unsafe path: {p}"}, status=400)
        if not rp.exists():
            return web.json_response({"error": f"not found: {p}"}, status=404)
        if rp.is_dir():
            targets.extend(iter_videos(rp, recursive=recursive))
        elif rp.is_file():
            if rp.suffix.lower() in VIDEO_EXTS:
                targets.append(rp)

    targets = sorted(set(targets))
    if not targets:
        return web.json_response({"error": "no videos to process"}, status=400)

    if async_mode:
        asyncio.create_task(_run_batch(targets, count, cols, width, force, lang, show_info))
        return web.json_response({"queued": len(targets)})

    # sync mode: process all and return summary
    results = []
    for v in targets:
        status, msg = generate_one(v, count, cols, width, force, lang=lang, show_info=show_info)
        results.append({"path": str(v.relative_to(BROWSE_ROOT)), "status": status, "msg": msg})
    ok = sum(1 for r in results if r["status"] == "ok")
    skip = sum(1 for r in results if r["status"] == "skip")
    err = sum(1 for r in results if r["status"] == "err")
    return web.json_response({"results": results, "ok": ok, "skip": skip, "err": err})


async def _run_batch(targets: list[Path], count: int, cols: int, width: int, force: bool, lang: str = "en", show_info: bool = True):
    await ws_broadcast({"type": "batch_start", "total": len(targets)})
    for i, v in enumerate(targets, 1):
        await ws_broadcast({
            "type": "item_start",
            "index": i,
            "total": len(targets),
            "path": str(v.relative_to(BROWSE_ROOT)),
            "name": v.name,
        })
        status, msg = await asyncio.get_event_loop().run_in_executor(
            None, lambda: generate_one(v, count, cols, width, force, lang=lang)
        )
        await ws_broadcast({
            "type": "item_done",
            "index": i,
            "total": len(targets),
            "path": str(v.relative_to(BROWSE_ROOT)),
            "name": v.name,
            "status": status,
            "msg": msg,
        })
    await ws_broadcast({"type": "batch_done", "total": len(targets)})


async def handle_preview(request: web.Request) -> web.Response:
    rel = request.query.get("v", "")
    rp = safe_resolve(rel)
    if rp is None or not rp.is_file():
        return web.Response(status=404)
    count = int(request.query.get("count", DEFAULT_COUNT))
    cols = int(request.query.get("cols", DEFAULT_COLS))
    width = int(request.query.get("width", DEFAULT_WIDTH))
    force = request.query.get("force", "0") == "1"
    lang = request.query.get("lang", DEFAULT_LABEL_LANG)
    if lang not in LABELS:
        lang = "en"
    show_info = request.query.get("show_info", "1") != "0"

    # Compatibility shim: list/grid thumbnails used to call /api/preview with
    # count=1&cols=1&width<=640. Detect that pattern and route to the cheap
    # sprite path so old cached pages still get a fast single-frame thumbnail.
    # The show_info flag is intentionally ignored here: count=1 implies a
    # single frame with no room for an info block anyway.
    if count <= 1 and cols <= 1 and width <= 640:
        try:
            loop = asyncio.get_event_loop()
            data = await loop.run_in_executor(None, _sprite_cached, rp, width, force)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)
        return web.Response(
            body=data,
            content_type="image/jpeg",
            headers={"Cache-Control": "public, max-age=604800"},
        )

    out = output_path_for(rp)
    if not out.exists() or force:
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None, lambda: generate_one(rp, count, cols, width, force, lang=lang, show_info=show_info)
            )
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    return web.Response(body=out.read_bytes(), content_type="image/jpeg",
                        headers={"Cache-Control": "public, max-age=86400"})


# Tiny single-frame thumbnail for the file list/grid. Cached on disk keyed by
# (path, mtime, width). Bypasses the full generate_one pipeline so listing a
# folder with 50 videos doesn't trigger 50 full sheet renders.
def _sprite_cache_path(video: Path, width: int) -> Path:
    try:
        st = video.stat()
        mtime_ns = st.st_mtime_ns
    except OSError:
        mtime_ns = 0
    # Two-level fanout: hash(abs path) -> first 2 chars to avoid huge dirs.
    import hashlib
    key = hashlib.sha1(str(video.resolve()).encode("utf-8")).hexdigest()[:16]
    bucket = SPRITE_CACHE_DIR / key[:2]
    bucket.mkdir(parents=True, exist_ok=True)
    return bucket / f"{key}_{width}_{mtime_ns}.jpg"


def _ffmpeg_sprite(video: Path, width: int) -> bytes:
    """Extract one frame near 10% of duration, scale to width, JPEG q=4."""
    # Probe duration cheaply so we don't grab the first black frame.
    try:
        info = probe(video)
        dur = max(0.0, float(info.duration))
        # Seek to 10% but fall back to earlier positions for corrupt tails.
        seek_targets = [dur * 0.10, dur * 0.05, 1.0, 0.0]
    except Exception:
        seek_targets = [1.0, 0.0]
    out_path: Optional[Path] = None
    for ts in seek_targets:
        out_path = SPRITE_CACHE_DIR / f"_probe_{os.getpid()}_{ts:.3f}.jpg"
        cmd = [
            "ffmpeg", "-y", "-loglevel", "error",
            "-ss", f"{ts:.3f}",
            "-i", str(video),
            "-frames:v", "1",
            "-vf", f"scale={width}:-2",
            "-q:v", "4",
            str(out_path),
        ]
        try:
            _run(cmd, timeout=20)
        except Exception:
            continue
        if out_path.exists() and out_path.stat().st_size > 0:
            break
        try:
            out_path.unlink()
        except OSError:
            pass
        out_path = None
    if out_path is None or not out_path.exists() or out_path.stat().st_size == 0:
        if out_path:
            try:
                out_path.unlink()
            except OSError:
                pass
        raise RuntimeError("ffmpeg sprite failed")
    data = out_path.read_bytes()
    try:
        out_path.unlink()
    except OSError:
        pass
    return data


def _sprite_cached(video: Path, width: int, force: bool = False) -> bytes:
    """Cache-aware single-frame sprite; reads from disk cache when fresh."""
    cached = _sprite_cache_path(video, width)
    if not force and cached.exists() and cached.stat().st_size > 0:
        return cached.read_bytes()
    data = _ffmpeg_sprite(video, width)
    try:
        cached.write_bytes(data)
    except OSError:
        pass
    return data


async def handle_sprite(request: web.Request) -> web.Response:
    rel = request.query.get("v", "")
    rp = safe_resolve(rel)
    if rp is None or not rp.is_file():
        return web.Response(status=404)
    width = int(request.query.get("w", "320"))
    width = max(80, min(width, 1280))
    force = request.query.get("force", "0") == "1"

    try:
        loop = asyncio.get_event_loop()
        data = await loop.run_in_executor(None, _sprite_cached, rp, width, force)
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)
    return web.Response(
        body=data,
        content_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=604800"},
    )


async def handle_ws(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    WS_CLIENTS.add(ws)
    try:
        async for _ in ws:  # noqa
            pass
    finally:
        WS_CLIENTS.discard(ws)
    return ws


# ── CLI batch ───────────────────────────────────────────────────────────────

def cli_batch(folder: Path, recursive: bool, force: bool,
              count: int, cols: int, width: int, lang: str, show_info: bool = True) -> int:
    folder = folder.resolve()
    if not folder.exists() or not folder.is_dir():
        log.error("not a directory: %s", folder)
        return 2
    videos = list(iter_videos(folder, recursive=recursive))
    if not videos:
        log.warning("no videos in %s", folder)
        return 0
    log.info("found %d videos in %s", len(videos), folder)
    counts = {"ok": 0, "skip": 0, "err": 0}
    for i, v in enumerate(videos, 1):
        log.info("[%d/%d] %s", i, len(videos), v.name)
        status, msg = generate_one(v, count, cols, width, force, lang=lang, show_info=show_info)
        counts[status] += 1
        print(f"[{status.upper():4s}] {v.name} :: {msg}", flush=True)
    log.info("done: ok=%d skip=%d err=%d", counts["ok"], counts["skip"], counts["err"])
    return 0 if counts["err"] == 0 else 1


# ── entry ───────────────────────────────────────────────────────────────────

def main():  # noqa: F811
    argv = sys.argv[1:]
    if not argv:
        # HTTP server mode
        app = web.Application()
        app.router.add_get("/", handle_index)
        app.router.add_get("/static/{name}", handle_static)
        app.router.add_get("/api/browse", handle_browse)
        app.router.add_post("/api/generate", handle_generate)
        app.router.add_get("/api/preview", handle_preview)
        app.router.add_get("/api/sprite", handle_sprite)
        app.router.add_get("/api/raw", handle_raw)
        app.router.add_get("/ws/progress", handle_ws)
        port = int(os.environ.get("PORT", "8800"))
        log.info("vthumb webui on http://0.0.0.0:%d", port)
        web.run_app(app, host="0.0.0.0", port=port)
        return

    parser = argparse.ArgumentParser(prog="vthumb")
    parser.add_argument("folder", type=Path)
    parser.add_argument("-r", "--recursive", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--count", type=int, default=DEFAULT_COUNT)
    parser.add_argument("--cols", type=int, default=DEFAULT_COLS)
    parser.add_argument("--width", type=int, default=DEFAULT_WIDTH)
    parser.add_argument("--lang", choices=list(LABELS.keys()), default=DEFAULT_LABEL_LANG,
                        help=f"Label language (default {DEFAULT_LABEL_LANG}).")
    ns = parser.parse_args(argv)
    sys.exit(cli_batch(ns.folder, recursive=ns.recursive, force=ns.force,
                       count=ns.count, cols=ns.cols, width=ns.width, lang=ns.lang))


if __name__ == "__main__":
    main()
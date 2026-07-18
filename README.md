# vthumb

> [中文文档](README_zh.md) | [English](README.md)

PotPlayer-style video thumbnail contact-sheet generator · WebUI + CLI

Generates clean 4×4 (configurable) frame-grid sheets with video metadata,
timestamp labels, and CJK font support — like PotPlayer's built-in thumbnail
feature, but as a self-hosted web service.

## Generated thumbnail

- Header strip: filename / size / resolution / codec / duration
- N-column grid of evenly-spaced frames, each letterboxed to 16:9
  (or 9:16 for vertical/portrait video)
- Timestamp centered at the bottom of each cell
- White canvas with 1px grey borders and clean white gutters
- Output `<source>.jpg`, default width 1920 px

## Quick start

### Native (recommended)

```bash
sudo apt-get install -y --no-install-recommends \
    ffmpeg fonts-noto-cjk fonts-noto-cjk-extra
sudo python3 -m pip install --break-system-packages \
    aiohttp==3.9.5 "Pillow>=10.0.0"

BROWSE_ROOT=/path/to/your/media PORT=8800 python3 server.py
```

Open `http://localhost:8800` to access the WebUI.

### Docker

```bash
# Edit docker-compose.yml to set your media mount path, then:
docker compose up -d --build
```

The container serves the WebUI on `http://localhost:8800`.

### CLI

```bash
python3 server.py /path/to/videos --cols 5 --count 25 --width 2560 --lang zh
```

## WebUI workflow

1. **Browse** your media tree from the left panel.
2. **Click the green dot** to add videos to the selection.
3. **Double-click** any video to generate a single full-size contact sheet
   and preview it in the right panel.
4. Click **Start** to batch-generate sheets for every selected video.
   Real-time progress streams over a WebSocket.

## Configuration

| Env var | Default | Description |
|---------|---------|-------------|
| `PORT` | 8800 | HTTP listen port |
| `BROWSE_ROOT` | /mnt/media | Root path for folder browsing |
| `MEDIA_ROOT` | /mnt/media | Root path for HTTP API access |
| `DEFAULT_COUNT` | 16 | Frames per sheet |
| `DEFAULT_COLS` | 4 | Grid columns |
| `DEFAULT_WIDTH` | 1920 | Sheet canvas width (px) |
| `DEFAULT_LABEL_LANG` | en | Header label language (en / zh) |
| `JPEG_QUALITY` | 95 | Output JPEG quality (1–100) |
| `SPRITE_CACHE_DIR` | `./cache/sprite` | Where single-frame thumbnail sprites are cached |

## HTTP API

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | WebUI (single-page app) |
| `/api/browse?path=...` | GET | List directory entries |
| `/api/generate` | POST | Generate thumbnails (sync or async via WS) |
| `/api/preview?v=...` | GET | Stream a single-video contact sheet (JPEG) |
| `/api/sprite?v=...&w=...` | GET | Single-frame thumbnail cached on disk |
| `/ws/progress` | WS | Real-time batch progress |

Generate request example:

```json
{
  "paths": ["Movies"],
  "recursive": false,
  "force": true,
  "count": 16,
  "cols": 4,
  "width": 1920,
  "lang": "zh"
}
```

## Requirements

- Python 3.11+
- ffmpeg / ffprobe (in PATH)
- Pillow ≥ 10.0
- aiohttp 3.9
- Optional: CJK fonts (`fonts-noto-cjk`) for Chinese/Japanese/Korean filenames

## Troubleshooting

**Chinese filenames show as □** — install CJK fonts:

```bash
sudo apt-get install -y fonts-noto-cjk fonts-noto-cjk-extra
sudo fc-cache -f
```

**Service not reachable** — check the server log:

```bash
# Native: stdout / journalctl
journalctl -u vthumb.service -n 50

# Docker
docker compose logs -f vthumb
```

## License

MIT

## Differences from the Windows PowerShell version

| | Windows vthumb.ps1 | vthumb |
|---|---|---|
| Language | PowerShell + .NET | Python + Pillow + ffmpeg |
| Font | Microsoft YaHei UI | DejaVu Sans / Noto CJK |
| Output | PNG | JPEG (quality 95) |
| Timestamp | Bottom-right corner | Bottom-center |
| Interaction | CLI batch | WebUI + CLI |
| Letterbox | ffmpeg pad filter | Pillow composite |
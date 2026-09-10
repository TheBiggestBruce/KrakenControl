# ⚠️⚠️⚠️ **DISCLAIMER** ⚠️⚠️⚠️
This is completely vibe coded as I am not a software engineer. Use at your own discretion. I will not promise to develop, maintain, update or repair this. But feel free to use, share or fork!

# Kraken Web

Local web control for the **NZXT Kraken Elite LCD** (`1e71:300c`, 640x640 display) on Linux. Provides a browser-based dashboard to upload, configure, stream, and display media on the cooler's LCD — with optional Q565 fast-memory acceleration.

![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue)
![License](https://img.shields.io/badge/license-GPL--3.0-green)

## Features

- **Media upload** — PNG, JPEG, WebP, GIF, and common video formats
- **Fit modes** — contain, cover, or stretch to 640x640
- **Animation/video conversion** — 24 or 30 FPS GIF output with auto-resolution and palette reduction to stay under the 23 MB asset memory limit
- **Live telemetry overlays** — liquid temp, CPU temp, GPU temp, or dual CPU/GPU overlay on any media
- **Circular viewport preview** — see exactly what the physical cooler display will show
- **Cover-crop focal point** — adjust X/Y position to frame the subject
- **URL streaming** — HTTP(S) images, video, HLS, and MJPEG via ffmpeg
- **Webpage streaming** — mirror a JavaScript-rendered 640x640 page through headless Chromium (Playwright)
- **Brightness and orientation** — control LCD brightness (0–100) and rotation (0/90/180/270)
- **Asset library** — up to 50 prepared assets with quick-switch
- **Systemd integration** — automatic startup as a user service
- **Q565 acceleration** (opt-in) — 18–20 FPS live frames via CoolerControl's liquidctl sidecar

## Requirements

- Linux with USB access to the Kraken
- Python 3.12+
- `ffmpeg` (for video/GIF processing and URL streaming)
- [CoolerControl](https://github.com/coolercontrol/coolercontrol) daemon running (`coolercontrold`)
- [liquidctl](https://github.com/liquidctl/liquidctl) source clone at `../liquidctl` (sibling directory)

## Install

```bash
./install.sh
```

The installer:

1. Creates a Python venv at `~/.local/share/kraken-web/venv`
2. Installs all Python dependencies + liquidctl from source
3. Installs Playwright Chromium for webpage streaming
4. Installs a udev rule for USB access (`71-kraken-web.rules`)
5. Generates and starts `kraken-web.service` as a systemd user service
6. Enables user lingering for boot-time startup

Then open **http://127.0.0.1:8787**.

### Manual USB permissions

If the installer skipped the privileged USB step, finish manually:

```bash
sudo install -m 0644 ./71-kraken-web.rules /etc/udev/rules.d/71-kraken-web.rules
sudo udevadm control --reload-rules
sudo udevadm trigger --subsystem-match=usb --action=add
sudo udevadm trigger --subsystem-match=hidraw --action=add
systemctl --user restart kraken-web
```

### Foreground run (development)

```bash
./start.sh
```

## Q565 Acceleration

The 2023 Elite (`300c`) has an undocumented Q565 fast-memory protocol used by NZXT CAM. Kraken Web includes an opt-in patch for CoolerControl's liquidctl sidecar, based on the MIT-licensed implementation from [AIOLCDUnchained](https://github.com/ToniPlays/AIOLCDUnchained).

This keeps CoolerControl as the sole USB owner while bypassing asset-bucket allocation for live frames. Live traffic goes through CoolerControl's serialized liquidctl socket, so fan, pump, status, and LCD commands cannot overlap on USB.

```bash
./enable-q565.sh
```

- Installs only a systemd environment drop-in — does not replace system liquidctl files
- Sustains **18–20 FPS** while the UI is open (18 FPS default live target)
- Without the patch, live frames fall back to CoolerControl's REST API at a lower rate

To disable, remove the drop-in and restart:

```bash
rm /etc/systemd/system/coolercontrold.service.d/kraken-q565.conf
systemctl --user daemon-reload
systemctl --user restart kraken-web
```

## Service Management

```bash
systemctl --user status kraken-web
systemctl --user restart kraken-web
systemctl --user stop kraken-web
journalctl --user -u kraken-web -f       # live logs
```

## Data Directory

All runtime data is stored under `~/.local/share/kraken-web`:

```
~/.local/share/kraken-web/
├── venv/              # Python virtual environment
├── playwright/        # Chromium browser for webpage streaming
├── uploads/           # Original uploaded files
├── assets/            # Processed 640x640 assets
├── assets.json        # Asset library manifest
├── active-asset       # Currently displayed asset ID
└── stream-settings.json
```

Override with `KRAKEN_WEB_DATA` environment variable.

## Architecture

```
┌─────────────┐      HTTP       ┌──────────────┐      USB/socket     ┌──────────────┐
│   Browser    │ ────────────── │  FastAPI +    │ ────────────────── │ CoolerControl│
│  Dashboard   │   :8787       │  Uvicorn      │   liquidctl        │  daemon      │
└─────────────┘                └──────┬───────┘                     └──────────────┘
                                      │
                              ┌───────┴───────┐
                              │  Processing   │
                              │  Pillow/ffmpeg│
                              └───────────────┘
```

- **Dual-control**: CoolerControl REST API when available; direct liquidctl USB fallback when CoolerControl is absent
- **Frame-drop logic**: Streaming decoder keeps only the newest decoded frame (queue `maxsize=1`) to prevent latency buildup
- **Live telemetry overlay**: Drawn into the live frame stream without rebuilding/replacing the entire GIF

## API

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/status` | Device connection, stream, and overlay state |
| `GET` | `/api/telemetry` | Current liquid/CPU/GPU temperatures |
| `GET` | `/api/assets` | List asset library |
| `DELETE` | `/api/assets/{id}` | Remove an asset |
| `GET` | `/api/assets/{id}/source` | Retrieve original uploaded file |
| `POST` | `/api/assets/{id}/preview` | Render preview PNG |
| `PUT` | `/api/assets/{id}/process` | Reprocess with new settings |
| `POST` | `/api/assets/{id}/show` | Display a prepared asset |
| `GET` | `/api/screen` | Live screenshot of current LCD output |
| `POST` | `/api/media` | Upload, process, and display media |
| `POST` | `/api/control` | Set brightness or orientation |
| `POST` | `/api/stream` | Start URL/webpage streaming |
| `DELETE` | `/api/stream` | Stop active stream |
| `GET` | `/` | Dashboard UI |

## Streaming Notes

- The UI reports actual completed LCD uploads and drops stale decoded frames to avoid latency buildup
- Uploaded GIF/video clips without telemetry play locally on the cooler and retain their encoded timing
- Telemetry on URL, webpage, and animated uploaded assets is drawn into the live frame stream
- Webpage rendering uses full Chromium with NVIDIA OpenGL/EGL compositing when available
- Capture is capped at 18 FPS — extra frames above LCD throughput only increase CPU usage and drops

## License

The `q565_patch/` directory is licensed under the [MIT License](q565_patch/LICENSE) (Copyright 2023 Marco Massarotto, from AIOLCDUnchained).

## Acknowledgments

- [CoolerControl](https://github.com/coolercontrol/coolercontrol) — hardware coordination daemon
- [liquidctl](https://github.com/liquidctl/liquidctl) — open-source liquid cooler control
- [AIOLCDUnchained](https://github.com/ToniPlays/AIOLCDUnchained) — Q565 protocol reverse engineering

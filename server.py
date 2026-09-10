from __future__ import annotations

import asyncio
import json
import os
import shutil
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from display import DisplayError, KrakenDisplay
from media import MediaError, prepare_media, render_preview
from overlay import ActiveOverlay
from streaming import StreamError, UrlStreamer


ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(
    os.environ.get("KRAKEN_WEB_DATA", Path.home() / ".local/share/kraken-web")
).expanduser()
UPLOAD_DIR = DATA_DIR / "uploads"
ASSET_DIR = DATA_DIR / "assets"
MANIFEST = DATA_DIR / "assets.json"
ACTIVE_FILE = DATA_DIR / "active-asset"
STREAM_SETTINGS_FILE = DATA_DIR / "stream-settings.json"
MAX_UPLOAD_BYTES = 500 * 1024 * 1024

display = KrakenDisplay()
streamer = UrlStreamer(display)
active_overlay = ActiveOverlay(display)
last_hardware: dict[str, object] = {}


@asynccontextmanager
async def lifespan(_: FastAPI):
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    ASSET_DIR.mkdir(parents=True, exist_ok=True)
    active = _active_entry()
    if active is not None:
        _activate_entry(active)
    yield
    active_overlay.stop()
    streamer.stop()
    display.close()


app = FastAPI(title="Kraken Web", version="0.1.0", lifespan=lifespan)


def _uses_live_overlay(mode: str, overlay: str, source: Path) -> bool:
    return (
        mode == "gif"
        and overlay != "none"
        and source.is_file()
        and display.live_frames_available
    )


def _activate_entry(entry: dict[str, object]) -> None:
    processing = entry.get("processing", {})
    source = Path(str(entry.get("source", "")))
    if (
        _uses_live_overlay(str(entry.get("mode")), str(entry.get("overlay")), source)
        and isinstance(processing, dict)
    ):
        requested_fps = int(processing.get("fps", 0))
        streamer.start_asset(
            source,
            min(requested_fps or 18, 18),
            str(processing.get("fit", "cover")),
            float(processing.get("focus_x", 0.5)),
            float(processing.get("focus_y", 0.5)),
            float(processing.get("start", 0)),
            float(processing.get("duration", 10)),
            str(entry["overlay"]),
            dict(processing.get("overlay_style", {})),
        )
        return
    active_overlay.activate(entry)


class Controls(BaseModel):
    brightness: int | None = Field(default=None, ge=0, le=100)
    orientation: int | None = None


class StreamRequest(BaseModel):
    url: str
    fps: int = Field(default=18, ge=1, le=30)
    fit: str = "contain"
    focus_x: float = Field(default=50, ge=0, le=100)
    focus_y: float = Field(default=50, ge=0, le=100)
    overlay: str = "none"
    source_type: str = "webpage"
    overlay_size: float = Field(default=100, ge=50, le=175)
    overlay_x: float = Field(default=50, ge=0, le=100)
    overlay_y: float = Field(default=76, ge=0, le=100)
    text_color: str = "#f4f7f2"
    accent_color: str = "#a855f7"
    background_color: str = "#050809"
    background_opacity: float = Field(default=78, ge=0, le=100)


class EditorSettings(BaseModel):
    fit: str = "cover"
    fps: int = Field(default=0, ge=0, le=30)
    palette_colors: int = 128
    start: float = Field(default=0, ge=0)
    duration: float = Field(default=10, gt=0, le=30)
    focus_x: float = Field(default=50, ge=0, le=100)
    focus_y: float = Field(default=50, ge=0, le=100)
    overlay: str = "none"
    overlay_size: float = Field(default=100, ge=50, le=175)
    overlay_x: float = Field(default=50, ge=0, le=100)
    overlay_y: float = Field(default=76, ge=0, le=100)
    text_color: str = "#f4f7f2"
    accent_color: str = "#a855f7"
    background_color: str = "#050809"
    background_opacity: float = Field(default=78, ge=0, le=100)
    overlay_refresh: float = Field(default=10, ge=2, le=60)

    def style(self) -> dict[str, object]:
        return {
            "size": self.overlay_size / 100,
            "x": self.overlay_x / 100,
            "y": self.overlay_y / 100,
            "text_color": self.text_color,
            "accent_color": self.accent_color,
            "background_color": self.background_color,
            "background_opacity": self.background_opacity / 100,
        }

    def processing(self) -> dict[str, object]:
        return {
            "fit": self.fit,
            "fps": self.fps,
            "palette_colors": self.palette_colors,
            "start": self.start,
            "duration": self.duration,
            "focus_x": self.focus_x / 100,
            "focus_y": self.focus_y / 100,
            "overlay_refresh": self.overlay_refresh,
            "overlay_style": self.style(),
        }


def _load_assets() -> list[dict[str, object]]:
    if not MANIFEST.exists():
        return []
    try:
        return json.loads(MANIFEST.read_text())
    except (OSError, json.JSONDecodeError):
        return []


def _save_assets(assets: list[dict[str, object]]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    temporary = MANIFEST.with_suffix(".tmp")
    temporary.write_text(json.dumps(assets, indent=2))
    temporary.replace(MANIFEST)


def _load_stream_settings() -> dict[str, object] | None:
    if not STREAM_SETTINGS_FILE.exists():
        return None
    try:
        value = json.loads(STREAM_SETTINGS_FILE.read_text())
        return value if isinstance(value, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def _save_stream_settings(settings: dict[str, object]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    temporary = STREAM_SETTINGS_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(settings, indent=2))
    temporary.replace(STREAM_SETTINGS_FILE)


def _set_active_asset(asset_id: str | None) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if asset_id is None:
        ACTIVE_FILE.unlink(missing_ok=True)
    else:
        ACTIVE_FILE.write_text(asset_id)


def _active_entry() -> dict[str, object] | None:
    if not ACTIVE_FILE.exists():
        return None
    asset_id = ACTIVE_FILE.read_text().strip()
    return next((item for item in _load_assets() if item.get("id") == asset_id), None)


async def _save_upload(upload: UploadFile, destination: Path) -> None:
    size = 0
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("wb") as output:
        while chunk := await upload.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_UPLOAD_BYTES:
                output.close()
                destination.unlink(missing_ok=True)
                raise HTTPException(413, "Upload exceeds the 500 MB limit")
            output.write(chunk)


@app.get("/api/status")
async def status() -> dict[str, object]:
    global last_hardware
    stream_state = streamer.state
    if stream_state["active"]:
        hardware = last_hardware or {
            "connected": True,
            "description": "Kraken stream active (sensor polling paused)",
        }
    else:
        try:
            hardware = await asyncio.to_thread(display.status)
            last_hardware = hardware
        except DisplayError as exc:
            hardware = {"connected": False, "error": str(exc)}
    return {"hardware": hardware, "stream": stream_state, "overlay": active_overlay.state}


@app.get("/api/telemetry")
async def telemetry() -> dict[str, float]:
    try:
        return await asyncio.to_thread(display.telemetry_values)
    except DisplayError as exc:
        raise HTTPException(503, str(exc)) from exc


@app.get("/api/assets")
async def assets() -> list[dict[str, object]]:
    return _load_assets()


@app.get("/api/editor-state")
async def editor_state() -> dict[str, object]:
    return {"asset": _active_entry()}


@app.get("/api/stream-settings")
async def stream_settings() -> dict[str, object]:
    return {"settings": _load_stream_settings()}


@app.get("/api/assets/{asset_id}/source")
async def asset_source(asset_id: str) -> FileResponse:
    entry = next((item for item in _load_assets() if item.get("id") == asset_id), None)
    if entry is None or not entry.get("source"):
        raise HTTPException(404, "Original source is unavailable; re-upload this asset")
    path = Path(str(entry["source"]))
    if not path.is_file():
        raise HTTPException(404, "Original source file is missing")
    return FileResponse(path, headers={"Cache-Control": "no-store, max-age=0"})


@app.post("/api/assets/{asset_id}/preview")
async def preview_asset(asset_id: str, values: EditorSettings) -> Response:
    entry = next((item for item in _load_assets() if item.get("id") == asset_id), None)
    if entry is None or not entry.get("source"):
        raise HTTPException(404, "Original source is unavailable; re-upload this asset")
    try:
        telemetry_values = await asyncio.to_thread(display.telemetry_values)
        content = await asyncio.to_thread(
            render_preview,
            Path(str(entry["source"])),
            values.fit,
            values.focus_x / 100,
            values.focus_y / 100,
            values.overlay,
            telemetry_values,
            values.style(),
            values.start,
        )
    except (MediaError, DisplayError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    return Response(content, media_type="image/png", headers={"Cache-Control": "no-store"})


@app.put("/api/assets/{asset_id}/process")
async def process_asset(asset_id: str, values: EditorSettings) -> dict[str, object]:
    all_assets = _load_assets()
    entry = next((item for item in all_assets if item.get("id") == asset_id), None)
    if entry is None or not entry.get("source"):
        raise HTTPException(404, "Original source is unavailable; re-upload this asset")
    source = Path(str(entry["source"]))
    if not source.is_file():
        raise HTTPException(404, "Original source file is missing")
    active_overlay.stop()
    streamer.stop()
    temporary = ASSET_DIR / asset_id / ".editor-process"
    shutil.rmtree(temporary, ignore_errors=True)
    try:
        telemetry_values = (
            await asyncio.to_thread(display.telemetry_values)
            if values.overlay != "none"
            else {}
        )
        prepared, mode = await asyncio.to_thread(
            prepare_media,
            source,
            temporary,
            values.fit,
            values.fps,
            values.start,
            values.duration,
            values.focus_x / 100,
            values.focus_y / 100,
            values.overlay,
            telemetry_values,
            values.style(),
            values.palette_colors,
        )
        output = ASSET_DIR / asset_id / f"display{prepared.suffix}"
        for old in (ASSET_DIR / asset_id).glob("display.*"):
            if old != output:
                old.unlink(missing_ok=True)
        prepared.replace(output)
        if not _uses_live_overlay(mode, values.overlay, source):
            await asyncio.to_thread(display.show, output, mode)
    except (MediaError, DisplayError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    finally:
        shutil.rmtree(temporary, ignore_errors=True)

    entry.update(
        mode=mode,
        path=str(output),
        size=output.stat().st_size,
        overlay=values.overlay,
        processing=values.processing(),
    )
    _save_assets(all_assets)
    _set_active_asset(asset_id)
    _activate_entry(entry)
    return entry


@app.get("/api/screen")
async def current_screen() -> Response:
    if streamer.state["active"]:
        live_preview = await asyncio.to_thread(streamer.preview)
        if live_preview is not None:
            return Response(
                live_preview,
                media_type="image/jpeg",
                headers={"Cache-Control": "no-store, max-age=0"},
            )
    try:
        content, media_type = await asyncio.to_thread(display.current_screen)
    except DisplayError as exc:
        raise HTTPException(404, str(exc)) from exc
    return Response(
        content,
        media_type=media_type,
        headers={"Cache-Control": "no-store, max-age=0"},
    )


@app.post("/api/media")
async def upload_media(
    file: UploadFile = File(...),
    fit: str = Form("contain"),
    fps: int = Form(24),
    start: float = Form(0),
    duration: float = Form(10),
    focus_x: float = Form(50),
    focus_y: float = Form(50),
    overlay: str = Form("none"),
    overlay_size: float = Form(100),
    overlay_x: float = Form(50),
    overlay_y: float = Form(76),
    text_color: str = Form("#f4f7f2"),
    accent_color: str = Form("#a855f7"),
    background_color: str = Form("#050809"),
    background_opacity: float = Form(78),
    overlay_refresh: float = Form(10),
    palette_colors: int = Form(128),
) -> dict[str, object]:
    active_overlay.stop()
    streamer.stop()
    asset_id = uuid.uuid4().hex
    original_name = Path(file.filename or "upload").name
    source = UPLOAD_DIR / f"{asset_id}-{original_name}"
    output_dir = ASSET_DIR / asset_id
    await _save_upload(file, source)
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        retained_source = output_dir / f"source{source.suffix.lower()}"
        source.replace(retained_source)
        telemetry = (
            await asyncio.to_thread(display.telemetry_values)
            if overlay != "none"
            else {}
        )
        overlay_style = {
            "size": overlay_size / 100,
            "x": overlay_x / 100,
            "y": overlay_y / 100,
            "text_color": text_color,
            "accent_color": accent_color,
            "background_color": background_color,
            "background_opacity": background_opacity / 100,
        }
        prepared, mode = await asyncio.to_thread(
            prepare_media,
            retained_source,
            output_dir,
            fit,
            fps,
            start,
            duration,
            focus_x / 100,
            focus_y / 100,
            overlay,
            telemetry,
            overlay_style,
            palette_colors,
        )
        if not _uses_live_overlay(mode, overlay, retained_source):
            await asyncio.to_thread(display.show, prepared, mode)
    except (MediaError, DisplayError, ValueError) as exc:
        shutil.rmtree(output_dir, ignore_errors=True)
        raise HTTPException(400, str(exc)) from exc
    finally:
        source.unlink(missing_ok=True)

    entry: dict[str, object] = {
        "id": asset_id,
        "name": original_name,
        "mode": mode,
        "path": str(prepared),
        "size": prepared.stat().st_size,
        "overlay": overlay,
        "source": str(retained_source),
        "processing": {
            "fit": fit,
            "fps": fps,
            "start": start,
            "duration": duration,
            "focus_x": focus_x / 100,
            "focus_y": focus_y / 100,
            "overlay_refresh": overlay_refresh,
            "overlay_style": overlay_style,
            "palette_colors": palette_colors,
        },
    }
    all_assets = [entry, *_load_assets()]
    _save_assets(all_assets[:50])
    _set_active_asset(asset_id)
    _activate_entry(entry)
    return entry


@app.post("/api/assets/{asset_id}/show")
async def show_asset(asset_id: str) -> dict[str, bool]:
    entry = next((item for item in _load_assets() if item.get("id") == asset_id), None)
    if entry is None:
        raise HTTPException(404, "Asset not found")
    path = Path(str(entry["path"]))
    if not path.is_file():
        raise HTTPException(404, "Prepared media file is missing")
    streamer.stop()
    active_overlay.stop()
    try:
        if not _uses_live_overlay(
            str(entry["mode"]), str(entry.get("overlay", "none")), Path(str(entry.get("source", "")))
        ):
            await asyncio.to_thread(display.show, path, str(entry["mode"]))
    except DisplayError as exc:
        raise HTTPException(503, str(exc)) from exc
    _activate_entry(entry)
    _set_active_asset(asset_id)
    return {"ok": True}


@app.delete("/api/assets/{asset_id}")
async def delete_asset(asset_id: str) -> dict[str, bool]:
    all_assets = _load_assets()
    entry = next((item for item in all_assets if item.get("id") == asset_id), None)
    if entry is None:
        raise HTTPException(404, "Asset not found")
    if active_overlay.state["asset_id"] == asset_id:
        active_overlay.stop()
    if _active_entry() is not None and _active_entry().get("id") == asset_id:
        if streamer.state.get("source_type") == "asset":
            streamer.stop()
        _set_active_asset(None)
    shutil.rmtree(ASSET_DIR / asset_id, ignore_errors=True)
    _save_assets([item for item in all_assets if item.get("id") != asset_id])
    return {"ok": True}


@app.post("/api/control")
async def control(values: Controls) -> dict[str, bool]:
    try:
        if values.brightness is not None:
            await asyncio.to_thread(display.set_brightness, values.brightness)
        if values.orientation is not None:
            await asyncio.to_thread(display.set_orientation, values.orientation)
    except (DisplayError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True}


@app.post("/api/stream")
async def start_stream(values: StreamRequest) -> dict[str, bool]:
    active_overlay.stop()
    try:
        await asyncio.to_thread(
            streamer.start,
            values.url,
            values.fps,
            values.fit,
            values.focus_x / 100,
            values.focus_y / 100,
            values.overlay,
            values.source_type,
            {
                "size": values.overlay_size / 100,
                "x": values.overlay_x / 100,
                "y": values.overlay_y / 100,
                "text_color": values.text_color,
                "accent_color": values.accent_color,
                "background_color": values.background_color,
                "background_opacity": values.background_opacity / 100,
            },
        )
    except StreamError as exc:
        raise HTTPException(400, str(exc)) from exc
    _save_stream_settings(values.model_dump())
    return {"ok": True}


@app.delete("/api/stream")
async def stop_stream() -> dict[str, bool]:
    await asyncio.to_thread(streamer.stop)
    return {"ok": True}


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(ROOT / "static/index.html")


app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")

from __future__ import annotations

import io
import os
import queue
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

from PIL import Image

from display import KrakenDisplay
from media import draw_telemetry


FRAME_SIZE = 640 * 640 * 3
GPU_CHROMIUM_ARGS = [
    "--ignore-gpu-blocklist",
    "--enable-gpu-rasterization",
    "--enable-zero-copy",
    "--use-gl=angle",
    "--use-angle=gl-egl",
]


class StreamError(RuntimeError):
    pass


class UrlStreamer:
    """Decode URL media continuously and keep only the newest decoded frame."""

    def __init__(self, display: KrakenDisplay) -> None:
        self._display = display
        self._stop = threading.Event()
        self._frames: queue.Queue[bytes] = queue.Queue(maxsize=1)
        self._decoder: threading.Thread | None = None
        self._sender: threading.Thread | None = None
        self._process: subprocess.Popen[bytes] | None = None
        self._lock = threading.Lock()
        self._preview_frame: bytes | None = None
        self._state: dict[str, object] = {
            "active": False,
            "target_fps": 0,
            "actual_fps": 0.0,
            "frames_sent": 0,
            "frames_dropped": 0,
            "error": None,
            "url": None,
            "overlay": "none",
        }

    @property
    def state(self) -> dict[str, object]:
        with self._lock:
            return dict(self._state)

    def preview(self) -> bytes | None:
        with self._lock:
            frame = self._preview_frame
        if frame is None:
            return None
        output = io.BytesIO()
        Image.frombytes("RGB", (640, 640), frame).save(output, "JPEG", quality=85)
        return output.getvalue()

    def start(
        self,
        url: str,
        fps: int,
        fit: str,
        focus_x: float = 0.5,
        focus_y: float = 0.5,
        overlay: str = "none",
        source_type: str = "webpage",
        overlay_style: dict[str, object] | None = None,
    ) -> None:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            raise StreamError("Stream URL must use http or https")
        if not 1 <= fps <= 30:
            raise StreamError("FPS must be between 1 and 30")
        if fit not in {"contain", "cover", "stretch"}:
            raise StreamError("Fit must be contain, cover, or stretch")
        if overlay not in {"none", "liquid", "cpu", "gpu", "cpu_gpu"}:
            raise StreamError("Unsupported telemetry overlay")
        if source_type not in {"webpage", "media"}:
            raise StreamError("Source type must be webpage or media")
        if source_type == "webpage":
            fps = min(fps, 18)

        self.stop()
        self._stop.clear()
        self._frames = queue.Queue(maxsize=1)
        with self._lock:
            self._preview_frame = None
            self._state.update(
                active=True,
                target_fps=fps,
                actual_fps=0.0,
                frames_sent=0,
                frames_dropped=0,
                error=None,
                url=url,
                overlay=overlay,
                source_type=source_type,
            )

        if fit == "cover":
            geometry = (
                "scale=640:640:force_original_aspect_ratio=increase,"
                f"crop=640:640:(in_w-640)*{focus_x}:(in_h-640)*{focus_y}"
            )
        elif fit == "stretch":
            geometry = "scale=640:640"
        else:
            geometry = (
                "scale=640:640:force_original_aspect_ratio=decrease,"
                "pad=640:640:(ow-iw)/2:(oh-ih)/2:color=black"
            )

        if source_type == "media":
            command = [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-fflags",
                "nobuffer",
                "-i",
                url,
                "-vf",
                f"fps={fps},{geometry}",
                "-pix_fmt",
                "rgb24",
                "-f",
                "rawvideo",
                "pipe:1",
            ]
            try:
                self._process = subprocess.Popen(
                    command, stdout=subprocess.PIPE, stderr=subprocess.PIPE
                )
            except OSError as exc:
                self._set_error(f"Could not start ffmpeg: {exc}")
                raise StreamError(str(exc)) from exc
            self._decoder = threading.Thread(target=self._decode_loop, daemon=True)
        else:
            self._decoder = threading.Thread(
                target=self._browser_decode_loop, args=(url, fps), daemon=True
            )
        self._sender = threading.Thread(
            target=self._send_loop,
            args=(overlay, overlay_style or {}),
            daemon=True,
        )
        self._decoder.start()
        self._sender.start()

    def start_asset(
        self,
        path: Path,
        fps: int,
        fit: str,
        focus_x: float,
        focus_y: float,
        start: float,
        duration: float,
        overlay: str,
        overlay_style: dict[str, object],
    ) -> None:
        if not path.is_file():
            raise StreamError("Original asset source is unavailable")
        if fit == "cover":
            geometry = (
                "scale=640:640:force_original_aspect_ratio=increase,"
                f"crop=640:640:(in_w-640)*{focus_x}:(in_h-640)*{focus_y}"
            )
        elif fit == "stretch":
            geometry = "scale=640:640"
        else:
            geometry = (
                "scale=640:640:force_original_aspect_ratio=decrease,"
                "pad=640:640:(ow-iw)/2:(oh-ih)/2:color=black"
            )
        command = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            str(start),
            "-i",
            str(path),
            "-t",
            str(duration),
            "-vf",
            f"fps={fps},{geometry}",
            "-pix_fmt",
            "rgb24",
            "-f",
            "rawvideo",
            "pipe:1",
        ]
        self.stop()
        self._stop.clear()
        self._frames = queue.Queue(maxsize=1)
        with self._lock:
            self._preview_frame = None
            self._state.update(
                active=True,
                target_fps=fps,
                actual_fps=0.0,
                frames_sent=0,
                frames_dropped=0,
                error=None,
                url=path.name,
                overlay=overlay,
                source_type="asset",
            )
        self._decoder = threading.Thread(
            target=self._asset_decode_loop, args=(command, fps), daemon=True
        )
        self._sender = threading.Thread(
            target=self._send_loop, args=(overlay, overlay_style), daemon=True
        )
        self._decoder.start()
        self._sender.start()

    def _asset_decode_loop(self, command: list[str], fps: int) -> None:
        while not self._stop.is_set():
            try:
                self._process = subprocess.Popen(
                    command, stdout=subprocess.PIPE, stderr=subprocess.PIPE
                )
            except OSError as exc:
                self._set_error(f"Could not start ffmpeg: {exc}")
                return
            assert self._process.stdout is not None
            while not self._stop.is_set():
                started = time.monotonic()
                frame = self._process.stdout.read(FRAME_SIZE)
                if len(frame) != FRAME_SIZE:
                    break
                self._put_frame(frame)
                remaining = 1 / fps - (time.monotonic() - started)
                if remaining > 0:
                    self._stop.wait(remaining)
            if self._stop.is_set():
                return
            return_code = self._process.wait()
            if return_code != 0:
                stderr = self._process.stderr.read() if self._process.stderr else b""
                self._set_error(stderr.decode(errors="replace").strip() or "Decoder failed")
                return

    def _decode_loop(self) -> None:
        assert self._process is not None and self._process.stdout is not None
        try:
            while not self._stop.is_set():
                frame = self._process.stdout.read(FRAME_SIZE)
                if len(frame) != FRAME_SIZE:
                    stderr = b""
                    if self._process.stderr is not None:
                        stderr = self._process.stderr.read()
                    message = stderr.decode(errors="replace").strip()
                    self._set_error(message or "The media stream ended")
                    break
                self._put_frame(frame)
        finally:
            self._stop.set()

    def _browser_decode_loop(self, url: str, fps: int) -> None:
        try:
            from playwright.sync_api import sync_playwright

            with sync_playwright() as playwright:
                configured = os.environ.get("KRAKEN_CHROMIUM_PATH")
                candidates = (
                    [Path(configured)]
                    if configured
                    else sorted(
                        Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "")).glob(
                            "chromium-*/chrome-linux64/chrome"
                        ),
                        reverse=True,
                    )
                )
                executable = next((path for path in candidates if path.is_file()), None)
                browser_environment = dict(os.environ)
                browser_environment.update(
                    MANGOHUD="0",
                    OBS_VKCAPTURE="0",
                    __NV_PRIME_RENDER_OFFLOAD="1",
                    __GLX_VENDOR_LIBRARY_NAME="nvidia",
                )
                browser = playwright.chromium.launch(
                    headless=True,
                    executable_path=str(executable) if executable else None,
                    args=GPU_CHROMIUM_ARGS if executable else [],
                    env=browser_environment,
                )
                page = browser.new_page(
                    viewport={"width": 640, "height": 640},
                    device_scale_factor=1,
                )
                page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                page.evaluate("document.documentElement.style.overflow = 'hidden'")
                interval = 1 / fps
                while not self._stop.is_set():
                    started = time.monotonic()
                    screenshot = page.screenshot(type="jpeg", quality=80)
                    with Image.open(io.BytesIO(screenshot)) as image:
                        self._put_frame(image.convert("RGB").tobytes())
                    remaining = interval - (time.monotonic() - started)
                    if remaining > 0:
                        self._stop.wait(remaining)
                browser.close()
        except Exception as exc:
            self._set_error(f"Browser stream failed: {exc}")

    def _put_frame(self, frame: bytes) -> None:
        try:
            self._frames.put_nowait(frame)
        except queue.Full:
            try:
                self._frames.get_nowait()
            except queue.Empty:
                pass
            self._frames.put_nowait(frame)
            with self._lock:
                self._state["frames_dropped"] = int(self._state["frames_dropped"]) + 1

    def _send_loop(self, overlay: str, overlay_style: dict[str, object]) -> None:
        sent_times: list[float] = []
        telemetry: dict[str, float] = {}
        telemetry_updated = 0.0
        with tempfile.TemporaryDirectory(prefix="kraken-stream-") as directory:
            frame_path = Path(directory) / "frame.bmp"
            while not self._stop.is_set():
                try:
                    frame = self._frames.get(timeout=0.25)
                except queue.Empty:
                    continue
                try:
                    now = time.monotonic()
                    if overlay != "none" and now - telemetry_updated >= 1.0:
                        telemetry = self._display.telemetry_values()
                        telemetry_updated = now
                    image = draw_telemetry(
                        Image.frombytes("RGB", (640, 640), frame),
                        overlay,
                        telemetry,
                        overlay_style,
                    )
                    with self._lock:
                        self._preview_frame = image.tobytes()
                    image.save(frame_path, "BMP")
                    self._display.show_live_frame(frame_path)
                except Exception as exc:
                    self._set_error(str(exc))
                    break

                now = time.monotonic()
                sent_times = [stamp for stamp in sent_times if now - stamp <= 2.0]
                sent_times.append(now)
                elapsed = max(now - sent_times[0], 0.001)
                measured = (len(sent_times) - 1) / elapsed if len(sent_times) > 1 else 0.0
                with self._lock:
                    self._state["frames_sent"] = int(self._state["frames_sent"]) + 1
                    self._state["actual_fps"] = round(measured, 1)
        self._stop.set()

    def _set_error(self, message: str) -> None:
        with self._lock:
            self._state["error"] = message
            self._state["active"] = False
        self._stop.set()

    def stop(self) -> None:
        self._stop.set()
        process = self._process
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
        for thread in (self._decoder, self._sender):
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=2)
        self._process = None
        self._decoder = None
        self._sender = None
        with self._lock:
            self._state["active"] = False

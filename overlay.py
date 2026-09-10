from __future__ import annotations

import shutil
import threading
from pathlib import Path
from typing import Any

from display import KrakenDisplay
from media import prepare_media


class ActiveOverlay:
    """Periodically rebuild the active local asset with fresh telemetry."""

    def __init__(self, display: KrakenDisplay) -> None:
        self._display = display
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._asset_id: str | None = None
        self._error: str | None = None
        self._revision = 0
        self._generation = 0

    @property
    def state(self) -> dict[str, object]:
        return {
            "asset_id": self._asset_id,
            "active": self._thread is not None and self._thread.is_alive(),
            "error": self._error,
            "revision": self._revision,
        }

    def activate(self, entry: dict[str, Any]) -> None:
        self.stop()
        config = entry.get("processing")
        source = entry.get("source")
        if entry.get("overlay") == "none" or not config or not source:
            return
        if not Path(source).is_file():
            return
        self._stop.clear()
        self._generation += 1
        generation = self._generation
        self._asset_id = str(entry["id"])
        self._error = None
        self._thread = threading.Thread(
            target=self._loop, args=(entry, generation), daemon=True
        )
        self._thread.start()

    def _loop(self, entry: dict[str, Any], generation: int) -> None:
        config = entry["processing"]
        interval = max(float(config.get("overlay_refresh", 10)), 2.0)
        output = Path(entry["path"])
        source = Path(entry["source"])
        while generation == self._generation and not self._stop.wait(interval):
            temporary = output.parent / ".overlay-refresh"
            shutil.rmtree(temporary, ignore_errors=True)
            try:
                telemetry = self._display.telemetry_values()
                prepared, mode = prepare_media(
                    source,
                    temporary,
                    fit=config["fit"],
                    fps=int(config["fps"]),
                    start=float(config["start"]),
                    duration=float(config["duration"]),
                    focus_x=float(config["focus_x"]),
                    focus_y=float(config["focus_y"]),
                    overlay=str(entry["overlay"]),
                    telemetry=telemetry,
                    overlay_style=config["overlay_style"],
                    palette_colors=int(config.get("palette_colors", 128)),
                )
                if generation != self._generation or self._stop.is_set():
                    break
                prepared.replace(output)
                self._display.show(output, mode)
                self._revision += 1
            except Exception as exc:
                self._error = str(exc)
            finally:
                shutil.rmtree(temporary, ignore_errors=True)

    def stop(self) -> None:
        self._generation += 1
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=2)
        self._thread = None
        self._asset_id = None

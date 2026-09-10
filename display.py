from __future__ import annotations

import os
import stat
import sys
import threading
import mimetypes
from pathlib import Path
from typing import Any

import httpx


ROOT = Path(__file__).resolve().parent
LIQUIDCTL_SOURCE = Path(
    os.environ.get("KRAKEN_LIQUIDCTL_SOURCE", ROOT.parent / "liquidctl")
).resolve()
if LIQUIDCTL_SOURCE.is_dir():
    sys.path.insert(0, str(LIQUIDCTL_SOURCE))

from liquidctl import find_liquidctl_devices  # noqa: E402


NZXT_VENDOR_ID = 0x1E71
SUPPORTED_PRODUCTS = {0x300C, 0x300E, 0x3012, 0x3014}


class DisplayError(RuntimeError):
    pass


class KrakenDisplay:
    """Use CoolerControl when available, with direct liquidctl as fallback."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._device: Any | None = None
        self._cc_url = os.environ.get("COOLERCONTROL_URL", "http://127.0.0.1:11987")
        self._cc_token = os.environ.get("COOLERCONTROL_TOKEN")
        self._cc_device_cache: dict[str, Any] | None = None
        self._liqctld_socket = Path("/run/coolercontrold-liqctld.sock")
        self._liqctld_device_id: int | None = None

    def _cc_headers(self) -> dict[str, str]:
        if not self._cc_token:
            raise DisplayError(
                "CoolerControl is running but KRAKEN Web has no API token configured"
            )
        return {"Authorization": f"Bearer {self._cc_token}"}

    def _cc_request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            response = httpx.request(
                method,
                f"{self._cc_url}{path}",
                headers=self._cc_headers(),
                timeout=120,
                **kwargs,
            )
            response.raise_for_status()
            return response
        except httpx.HTTPError as exc:
            detail = ""
            if getattr(exc, "response", None) is not None:
                try:
                    detail = exc.response.json().get("error", "")
                except Exception:
                    detail = exc.response.text
            raise DisplayError(f"CoolerControl request failed: {detail or exc}") from exc

    def _coolercontrol_available(self) -> bool:
        try:
            return httpx.get(f"{self._cc_url}/handshake", timeout=1).status_code == 200
        except httpx.HTTPError:
            return False

    def _cc_device(self) -> dict[str, Any]:
        if self._cc_device_cache is not None:
            return self._cc_device_cache
        devices = self._cc_request("GET", "/devices").json()["devices"]
        for device in devices:
            channels = device.get("info", {}).get("channels", {})
            if "Kraken" in device.get("name", "") and "lcd" in channels:
                self._cc_device_cache = device
                return device
        raise DisplayError("CoolerControl did not report a Kraken LCD device")

    def _cc_settings(self) -> list[dict[str, Any]]:
        uid = self._cc_device()["uid"]
        return self._cc_request("GET", f"/devices/{uid}/settings").json()["settings"]

    def _connect(self) -> Any:
        if self._device is not None:
            return self._device

        candidates = []
        try:
            for device in find_liquidctl_devices(vendor=NZXT_VENDOR_ID):
                if device.product_id in SUPPORTED_PRODUCTS and "Kraken" in device.description:
                    candidates.append(device)
        except Exception as exc:
            raise DisplayError(
                "Cannot access the Kraken USB device. Install the udev rule, then reconnect the cooler."
            ) from exc

        if not candidates:
            raise DisplayError("No supported NZXT Kraken LCD device was found")

        device = candidates[0]
        try:
            device.connect()
            device.initialize(direct_access=True)
        except Exception as exc:
            try:
                device.disconnect()
            except Exception:
                pass
            raise DisplayError(f"Could not initialize {device.description}: {exc}") from exc

        self._device = device
        return device

    def _run(self, operation: str, *args: Any, **kwargs: Any) -> Any:
        with self._lock:
            device = self._connect()
            try:
                return getattr(device, operation)(*args, **kwargs)
            except Exception as exc:
                self._disconnect_unlocked()
                raise DisplayError(f"Kraken command failed: {exc}") from exc

    def show(self, path: Path, mode: str) -> None:
        if mode not in {"static", "gif"}:
            raise ValueError(f"Unsupported screen mode: {mode}")
        if self._coolercontrol_available():
            with self._lock:
                uid = self._cc_device()["uid"]
                lcd = next(
                    (item["lcd"] for item in self._cc_settings() if item["channel_name"] == "lcd"),
                    {},
                )
                media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                with path.open("rb") as image:
                    self._cc_request(
                        "PUT",
                        f"/devices/{uid}/settings/lcd/lcd/images",
                        data={
                            "mode": "image",
                            "brightness": str(lcd.get("brightness", 100)),
                            "orientation": str(lcd.get("orientation", 0)),
                        },
                        files={"images[]": (path.name, image, media_type)},
                    )
            return
        self._run("set_screen", "lcd", mode, str(path))

    def show_live_frame(self, path: Path) -> None:
        """Send a transient frame without CoolerControl's multipart image copy."""
        try:
            socket_mode = self._liqctld_socket.stat().st_mode
            if not stat.S_ISSOCK(socket_mode) or not os.access(
                self._liqctld_socket, os.R_OK | os.W_OK
            ):
                raise OSError("liqctld socket is unavailable")
            transport = httpx.HTTPTransport(uds=str(self._liqctld_socket))
            with httpx.Client(transport=transport, timeout=10) as client:
                if self._liqctld_device_id is None:
                    devices = client.get("http://liqctld/devices").raise_for_status().json()[
                        "devices"
                    ]
                    device = next(
                        item for item in devices if "Kraken" in item.get("description", "")
                    )
                    self._liqctld_device_id = int(device["id"])
                client.put(
                    f"http://liqctld/devices/{self._liqctld_device_id}/screen",
                    json={"channel": "lcd", "mode": "static", "value": str(path)},
                ).raise_for_status()
            return
        except (OSError, StopIteration, KeyError, ValueError, httpx.HTTPError):
            self.show(path, "static")

    @property
    def live_frames_available(self) -> bool:
        try:
            return stat.S_ISSOCK(self._liqctld_socket.stat().st_mode) and os.access(
                self._liqctld_socket, os.R_OK | os.W_OK
            )
        except OSError:
            return False

    def set_brightness(self, value: int) -> None:
        if not 0 <= value <= 100:
            raise ValueError("Brightness must be between 0 and 100")
        if self._coolercontrol_available():
            self._set_cc_lcd(brightness=value)
        else:
            self._run("set_screen", "lcd", "brightness", value)

    def set_orientation(self, value: int) -> None:
        if value not in {0, 90, 180, 270}:
            raise ValueError("Orientation must be 0, 90, 180, or 270")
        if self._coolercontrol_available():
            self._set_cc_lcd(orientation=value)
        else:
            self._run("set_screen", "lcd", "orientation", value)

    def _set_cc_lcd(self, **changes: int) -> None:
        with self._lock:
            uid = self._cc_device()["uid"]
            lcd = next(
                (item["lcd"] for item in self._cc_settings() if item["channel_name"] == "lcd"),
                None,
            )
            if lcd is None:
                raise DisplayError("CoolerControl has no current LCD settings")
            lcd.update(changes)
            self._cc_request("PUT", f"/devices/{uid}/settings/lcd/lcd", json=lcd)

    def status(self) -> dict[str, Any]:
        if self._coolercontrol_available():
            with self._lock:
                device = self._cc_device()
                uid = device["uid"]
                history = self._cc_request("GET", f"/status/{uid}").json()["status_history"]
                latest = history[-1] if history else {"temps": [], "channels": []}
                sensors = [
                    {"name": f"{item['name'].title()} temperature", "value": item["temp"], "unit": "°C"}
                    for item in latest["temps"]
                ]
                for item in latest["channels"]:
                    sensors.extend(
                        [
                            {"name": f"{item['name'].title()} speed", "value": item["rpm"], "unit": "rpm"},
                            {"name": f"{item['name'].title()} duty", "value": round(item["duty"], 1), "unit": "%"},
                        ]
                    )
                lcd_info = device["info"]["channels"]["lcd"]["lcd_info"]
                return {
                    "connected": True,
                    "description": device["name"],
                    "vendor_id": "1e71",
                    "product_id": "300c",
                    "serial": None,
                    "firmware": device.get("lc_info", {}).get("firmware_version"),
                    "resolution": [lcd_info["screen_width"], lcd_info["screen_height"]],
                    "backend": "CoolerControl",
                    "sensors": sensors,
                }
        with self._lock:
            device = self._connect()
            try:
                values = device.get_status(direct_access=True)
                return {
                    "connected": True,
                    "description": device.description,
                    "vendor_id": f"{device.vendor_id:04x}",
                    "product_id": f"{device.product_id:04x}",
                    "serial": device.serial_number,
                    "resolution": list(getattr(device, "lcd_resolution", (640, 640))),
                    "sensors": [
                        {"name": name, "value": value, "unit": unit}
                        for name, value, unit in values
                    ],
                }
            except Exception as exc:
                self._disconnect_unlocked()
                raise DisplayError(f"Could not read Kraken status: {exc}") from exc

    def telemetry_values(self) -> dict[str, float]:
        if not self._coolercontrol_available():
            status = self.status()
            liquid = next(
                (
                    float(item["value"])
                    for item in status["sensors"]
                    if item["name"].lower().startswith("liquid temperature")
                ),
                0.0,
            )
            return {"liquid": liquid}

        with self._lock:
            values: dict[str, float] = {}
            devices = self._cc_request("GET", "/devices").json()["devices"]
            for device in devices:
                kind = device.get("type")
                if kind not in {"CPU", "GPU", "Liquidctl"}:
                    continue
                history = self._cc_request("GET", f"/status/{device['uid']}").json().get(
                    "status_history", []
                )
                if not history or not history[-1].get("temps"):
                    continue
                temps = history[-1]["temps"]
                if kind == "CPU":
                    package = next((item for item in temps if item["name"] == "temp1"), temps[0])
                    values["cpu"] = float(package["temp"])
                elif kind == "GPU":
                    primary = next(
                        (item for item in temps if item["name"] == "GPU Temp"), temps[0]
                    )
                    values["gpu"] = float(primary["temp"])
                elif "Kraken" in device.get("name", ""):
                    values["liquid"] = float(temps[0]["temp"])
            return values

    def current_screen(self) -> tuple[bytes, str]:
        if not self._coolercontrol_available():
            raise DisplayError("Current-screen preview requires CoolerControl")
        with self._lock:
            uid = self._cc_device()["uid"]
            response = self._cc_request(
                "GET", f"/devices/{uid}/settings/lcd/lcd/images"
            )
            return response.content, response.headers.get("content-type", "image/png")

    def _disconnect_unlocked(self) -> None:
        if self._device is None:
            return
        try:
            self._device.disconnect()
        finally:
            self._device = None

    def close(self) -> None:
        with self._lock:
            self._disconnect_unlocked()

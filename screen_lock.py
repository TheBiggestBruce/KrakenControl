from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
from pathlib import Path

from display import DisplayError, KrakenDisplay


LOGGER = logging.getLogger(__name__)


def _loginctl(*arguments: str) -> str:
    result = subprocess.run(
        ["loginctl", *arguments, "--no-pager"],
        capture_output=True,
        text=True,
        timeout=5,
        check=True,
    )
    return result.stdout


def desktop_locked() -> bool | None:
    """Find this user's graphical sessions, including from a lingering service.

    Prefer the foreground session. With no foreground session, blank only if
    every graphical session is locked. Missing sessions are not unlock events.
    """
    sessions = []
    for line in _loginctl("list-sessions", "--no-legend").splitlines():
        fields = line.split()
        if len(fields) < 2 or fields[1] != str(os.getuid()):
            continue
        output = _loginctl(
            "show-session", fields[0],
            "--property=Type", "--property=Class", "--property=Active",
            "--property=State", "--property=LockedHint",
        )
        properties = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
        if (
            properties.get("Type") in {"wayland", "x11"}
            and properties.get("Class") in {"user", "user-early"}
            and properties.get("State") != "closing"
        ):
            sessions.append(properties)
    foreground = [session for session in sessions if session.get("Active") == "yes"]
    selected = foreground or sessions
    if not selected or any(session.get("LockedHint") not in {"yes", "no"} for session in selected):
        return None
    return all(session["LockedHint"] == "yes" for session in selected)


class ScreenLockMonitor:
    def __init__(self, display: KrakenDisplay, settings_path: Path | None = None) -> None:
        self.display = display
        self.enabled = os.environ.get("KRAKEN_SCREEN_LOCK", "1").lower() not in {"0", "false", "no"}
        self._settings_path = settings_path
        self._settings_lock = threading.RLock()
        if settings_path is not None:
            try:
                settings = json.loads(settings_path.read_text())
                if isinstance(settings, dict) and type(settings.get("enabled")) is bool:
                    self.enabled = settings["enabled"]
            except (OSError, ValueError):
                pass
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._state: dict[str, object] = {"enabled": self.enabled, "locked": None, "error": None}

    @property
    def state(self) -> dict[str, object]:
        return dict(self._state)

    def set_enabled(self, enabled: bool) -> dict[str, object]:
        with self._settings_lock:
            if self._settings_path is not None:
                self._settings_path.parent.mkdir(parents=True, exist_ok=True)
                temporary = self._settings_path.with_suffix(".tmp")
                temporary.write_text(json.dumps({"enabled": enabled}))
                temporary.replace(self._settings_path)
            self.enabled = enabled
            self._state = {**self._state, "enabled": enabled}
            return self.state

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="screen-lock", daemon=True)
        self._thread.start()

    def _check(self) -> None:
        with self._settings_lock:
            locked = desktop_locked() if self.enabled else False
            if locked is not None:
                self.display.set_screen_locked(locked)
            self._state = {"enabled": self.enabled, "locked": locked, "error": None}

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._check()
            except (OSError, ValueError, subprocess.SubprocessError, DisplayError) as exc:
                if self._state["error"] != str(exc):
                    LOGGER.warning("Screen-lock display control: %s", exc)
                self._state = {**self._state, "error": str(exc)}
            if self._stop.wait(1):
                break

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
            self._thread = None
        try:
            self.display.set_screen_locked(False)
        except (DisplayError, OSError, ValueError):
            LOGGER.exception("Could not restore Kraken backlight on shutdown")

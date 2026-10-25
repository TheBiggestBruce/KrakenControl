import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, PropertyMock, call, patch

import httpx

from display import DisplayError, KrakenDisplay
from screen_lock import ScreenLockMonitor, desktop_locked


class SessionDetectionTests(unittest.TestCase):
    def detect(self, sessions):
        listing = "\n".join(f"{sid} {uid} user seat0" for sid, uid, _ in sessions)
        details = {sid: properties for sid, _, properties in sessions}

        def loginctl(command, *args):
            if command == "list-sessions":
                return listing
            return details[args[0]]

        with patch("screen_lock.os.getuid", return_value=1000), patch(
            "screen_lock._loginctl", side_effect=loginctl
        ):
            return desktop_locked()

    def test_graphical_lock_and_unlock_without_session_environment(self):
        for kind in ("wayland", "x11"):
            for hint, expected in (("yes", True), ("no", False)):
                with self.subTest(kind=kind, hint=hint):
                    self.assertIs(self.detect([
                        ("1", 1000, "Type=unspecified\nClass=manager\nLockedHint=no"),
                        ("2", 1000, f"Type={kind}\nClass=user\nActive=yes\nLockedHint={hint}"),
                        ("3", 1001, "Type=wayland\nClass=user\nActive=yes\nLockedHint=no"),
                    ]), expected)

    def test_foreground_session_wins_over_old_locked_session(self):
        self.assertIs(self.detect([
            ("2", 1000, "Type=wayland\nClass=user\nActive=no\nLockedHint=yes"),
            ("3", 1000, "Type=wayland\nClass=user\nActive=yes\nLockedHint=no"),
        ]), False)

    def test_no_foreground_requires_all_sessions_locked(self):
        for hint, expected in (("yes", True), ("no", False)):
            self.assertIs(self.detect([
                ("2", 1000, "Type=wayland\nClass=user\nActive=no\nLockedHint=yes"),
                ("3", 1000, f"Type=x11\nClass=user\nActive=no\nLockedHint={hint}"),
            ]), expected)

    def test_missing_or_closing_desktop_is_not_an_unlock(self):
        for sessions in ([], [
            ("2", 1000, "Type=wayland\nClass=user\nState=closing\nLockedHint=no"),
        ], [
            ("2", 1000, "Type=wayland\nClass=user\nActive=yes"),
        ], [
            ("2", 1000, "Type=tty\nClass=user\nLockedHint=no"),
        ]):
            self.assertIsNone(self.detect(sessions))

    def test_unknown_or_failed_detection_does_not_touch_display(self):
        display = Mock()
        monitor = ScreenLockMonitor(display)
        monitor.enabled = True
        with patch("screen_lock.desktop_locked", return_value=None):
            monitor._check()
        with patch("screen_lock.desktop_locked", side_effect=subprocess.TimeoutExpired("loginctl", 5)):
            with self.assertRaises(subprocess.TimeoutExpired):
                monitor._check()
        display.set_screen_locked.assert_not_called()

    def test_monitor_retries_failed_transition_and_restores_on_stop(self):
        display = Mock()
        display.set_screen_locked.side_effect = [DisplayError("offline"), None, None]
        monitor = ScreenLockMonitor(display)
        monitor.enabled = True
        with patch("screen_lock.desktop_locked", return_value=True), patch.object(
            monitor._stop, "wait", side_effect=[False, True]
        ), self.assertLogs("screen_lock", level="WARNING"):
            monitor._run()
        self.assertEqual(monitor.state, {"enabled": True, "locked": True, "error": None})
        monitor.stop()
        self.assertEqual(display.set_screen_locked.call_args_list, [call(True), call(True), call(False)])


class BacklightTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.state_file = Path(directory.name) / "brightness.json"
        self.display = KrakenDisplay(self.state_file)
        self.display._coolercontrol_available = Mock(return_value=False)
        self.display._connect = Mock(return_value=Mock(brightness=62))
        self.display._run = Mock()

    def test_lock_is_idempotent_and_restores_previous_brightness(self):
        self.display.set_screen_locked(True)
        self.display.set_screen_locked(True)
        self.assertEqual(self.state_file.read_text(), "62")
        self.display.set_screen_locked(False)
        self.display.set_screen_locked(False)
        self.assertEqual(self.display._run.call_args_list, [
            call("set_screen", "lcd", "brightness", 0),
            call("set_screen", "lcd", "brightness", 62),
        ])
        self.assertFalse(self.state_file.exists())

    def test_brightness_changes_while_locked_are_deferred_including_zero(self):
        self.display.set_brightness(45)
        self.display.set_screen_locked(True)
        self.assertEqual(self.state_file.read_text(), "45")
        self.display._run.reset_mock()
        self.display.set_brightness(0)
        self.display._run.assert_not_called()
        self.display.set_screen_locked(False)
        self.display._run.assert_called_once_with("set_screen", "lcd", "brightness", 0)

    def test_failed_off_and_restore_are_retried_without_losing_brightness(self):
        for locked in (True, False):
            self.display._run.side_effect = DisplayError("offline")
            with self.assertRaises(DisplayError):
                self.display.set_screen_locked(locked)
            self.assertEqual(self.state_file.read_text(), "62")
            self.display._run.side_effect = None
            self.display.set_screen_locked(locked)
        self.assertFalse(self.state_file.exists())

    def test_saved_brightness_recovers_after_service_restart(self):
        self.display.set_screen_locked(True)
        recovered = KrakenDisplay(self.state_file)
        recovered._coolercontrol_available = Mock(return_value=False)
        recovered._connect = Mock(return_value=Mock(brightness=0))
        recovered._run = Mock()
        recovered.set_screen_locked(True)
        self.assertEqual(self.state_file.read_text(), "62")
        recovered.set_screen_locked(False)
        recovered._run.assert_called_with("set_screen", "lcd", "brightness", 62)

    def test_sidecar_only_changes_backlight_and_retries_without_usb_takeover(self):
        self.display._coolercontrol_available.return_value = True
        self.display._cc_settings = Mock(return_value=[{"channel_name": "lcd", "lcd": {"brightness": 62}}])
        self.display._sidecar_screen = Mock(side_effect=httpx.ConnectError("offline"))
        self.display._set_cc_lcd = Mock()
        with patch.object(KrakenDisplay, "live_frames_available", new_callable=PropertyMock, return_value=True):
            with self.assertRaises(DisplayError):
                self.display.set_screen_locked(True)
            self.display._sidecar_screen.side_effect = None
            self.display.set_screen_locked(True)
            self.display.set_screen_locked(False)
        self.assertEqual(self.display._sidecar_screen.call_args_list, [
            call("brightness", "0"), call("brightness", "0"), call("brightness", "62"),
        ])
        self.display._set_cc_lcd.assert_not_called()
        self.display._connect.assert_not_called()
        self.display._run.assert_not_called()

    def test_rest_fallback_keeps_orientation_changes_dark_and_restores_brightness(self):
        self.display._coolercontrol_available.return_value = True
        settings = {"brightness": 62, "orientation": 0}
        self.display._cc_settings = Mock(return_value=[{"channel_name": "lcd", "lcd": settings}])
        self.display._set_cc_lcd = Mock(side_effect=lambda **changes: settings.update(changes))
        with patch.object(KrakenDisplay, "live_frames_available", new_callable=PropertyMock, return_value=False):
            self.display.set_screen_locked(True)
            self.assertEqual(settings["brightness"], 0)
            self.display.set_orientation(90)
            self.assertEqual(settings["brightness"], 0)
            self.display.set_screen_locked(False)
        self.assertEqual(settings, {"brightness": 62, "orientation": 90})

    def test_new_media_cannot_light_up_locked_display(self):
        self.display.set_screen_locked(True)
        self.display._coolercontrol_available.return_value = True
        self.display._cc_device = Mock(return_value={"uid": "kraken"})
        self.display._cc_settings = Mock(return_value=[
            {"channel_name": "lcd", "lcd": {"brightness": 62, "orientation": 90}},
        ])
        self.display._cc_request = Mock()
        image = self.state_file.parent / "image.png"
        image.write_bytes(b"image")
        self.display.show(image, "static")
        self.assertEqual(self.display._cc_request.call_args.kwargs["data"], {
            "mode": "image", "brightness": "0", "orientation": "90",
        })
        self.assertEqual(self.state_file.read_text(), "62")

    def test_sidecar_sends_brightness_without_media_upload(self):
        self.display._liqctld_device_id = 3
        with patch("display.httpx.Client") as client_class:
            client = client_class.return_value.__enter__.return_value
            self.display._sidecar_screen("brightness", "0")
        client.put.assert_called_once_with(
            "http://liqctld/devices/3/screen",
            json={"channel": "lcd", "mode": "brightness", "value": "0"},
        )
        self.display._run.assert_not_called()


if __name__ == "__main__":
    unittest.main()

import os
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import httpx

from display import DisplayError, KrakenDisplay


class UsbOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.display = KrakenDisplay()
        self.display._liqctld_socket = Mock()
        self.display._liqctld_socket.exists.return_value = False
        self.device = Mock(product_id=0x300C, description="NZXT Kraken Elite")
        self.discovery = patch("display.find_liquidctl_devices", return_value=[self.device])
        self.find_devices = self.discovery.start()
        self.addCleanup(self.discovery.stop)
        self.health = patch("display.httpx.get")
        self.get = self.health.start()
        self.addCleanup(self.health.stop)
        self.get.side_effect = httpx.ConnectError("Connection refused")

    def test_daemon_restart_does_not_take_over_usb(self):
        self.display._cc_token = "test-token"
        self.get.side_effect = None
        self.get.return_value.status_code = 200
        self.display._cc_device_cache = {"uid": "old-device"}
        self.display._liqctld_device_id = 1
        with patch("display.httpx.request") as request:
            request.return_value.content = b"preview"
            request.return_value.headers = {"content-type": "image/png"}
            self.assertEqual(self.display.current_screen(), (b"preview", "image/png"))

        self.get.side_effect = httpx.ConnectError("Daemon restarting")
        self.display._cc_token = None
        with self.assertRaisesRegex(DisplayError, "waiting for it to recover"):
            self.display.status()
        self.find_devices.assert_not_called()
        self.assertIsNone(self.display._cc_device_cache)
        self.assertIsNone(self.display._liqctld_device_id)

    def test_configured_daemon_unavailable_at_startup_does_not_open_usb(self):
        for config in ({"COOLERCONTROL_TOKEN": "test-token"},
                       {"COOLERCONTROL_URL": "http://localhost:11987"}):
            with self.subTest(config=config), patch.dict(os.environ, config):
                display = KrakenDisplay()
                display._liqctld_socket = self.display._liqctld_socket
                with self.assertRaises(DisplayError):
                    display.set_brightness(50)
        self.find_devices.assert_not_called()

    def test_sidecar_without_rest_api_does_not_open_usb(self):
        self.display._liqctld_socket.exists.return_value = True
        with self.assertRaises(DisplayError):
            self.display.set_orientation(90)
        self.find_devices.assert_not_called()

    def test_unhealthy_api_does_not_open_usb(self):
        self.get.side_effect = None
        self.get.return_value.status_code = 503
        with self.assertRaises(DisplayError):
            self.display.status()
        self.get.side_effect = httpx.ConnectError("Daemon restarting")
        with self.assertRaises(DisplayError):
            self.display.status()
        self.find_devices.assert_not_called()

    def test_handoff_releases_bulk_before_coolercontrol_upload(self):
        self.display.show(Path("direct.gif"), "gif")
        self.device.set_screen.assert_called_once_with("lcd", "gif", "direct.gif")
        self.get.side_effect = None
        self.get.return_value.status_code = 200
        self.display._cc_device_cache = {"uid": "kraken"}
        self.display._cc_token = "test-token"

        def request(method, url, **kwargs):
            self.device.disconnect.assert_called_once()
            self.device.bulk_device.close.assert_called_once()
            return httpx.Response(200, json={}, request=httpx.Request(method, url))

        with patch("display.httpx.request", side_effect=request):
            self.display.current_screen()
        self.assertIsNone(self.display._device)

    def test_failed_direct_initialization_releases_bulk(self):
        self.device.initialize.side_effect = RuntimeError("USB failure")
        with self.assertRaisesRegex(DisplayError, "Could not initialize"):
            self.display.status()
        self.device.disconnect.assert_called_once()
        self.device.bulk_device.close.assert_called_once()
        self.assertIsNone(self.display._device)

    def test_close_releases_bulk_even_if_hid_disconnect_fails(self):
        self.display.set_brightness(50)
        self.device.disconnect.side_effect = OSError("HID disconnected")
        with self.assertRaises(OSError):
            self.display.close()
        self.device.bulk_device.close.assert_called_once()
        self.assertIsNone(self.display._device)


if __name__ == "__main__":
    unittest.main()

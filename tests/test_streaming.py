import io
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

from PIL import Image

from streaming import UrlStreamer


class BrowserStreamTests(unittest.TestCase):
    def test_playwright_uses_matching_browser_without_forced_gpu(self):
        screenshot = io.BytesIO()
        Image.new("RGB", (640, 640), "red").save(screenshot, "JPEG")
        for executable in (None, "/opt/chromium/chrome"):
            with self.subTest(executable=executable):
                stream = UrlStreamer(MagicMock())
                playwright = MagicMock()
                page = playwright.chromium.launch.return_value.new_page.return_value
                page.screenshot.return_value = screenshot.getvalue()
                sync_playwright = MagicMock()
                sync_playwright.return_value.__enter__.return_value = playwright
                module = MagicMock(sync_playwright=sync_playwright)
                environment = {"KRAKEN_CHROMIUM_PATH": executable} if executable else {}
                with patch.dict(os.environ, environment, clear=True), patch.dict(
                    sys.modules, {"playwright.sync_api": module}
                ), patch.object(stream, "_put_frame", side_effect=lambda _: stream._stop.set()) as put:
                    stream._browser_decode_loop("http://localhost/", 18)

                expected = {"executable_path": executable} if executable else {}
                playwright.chromium.launch.assert_called_once_with(headless=True, **expected)
                put.assert_called_once()
                self.assertEqual(len(put.call_args.args[0]), 640 * 640 * 3)
                self.assertIsNone(stream.state["error"])


if __name__ == "__main__":
    unittest.main()

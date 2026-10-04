import os
import subprocess
import unittest
from unittest.mock import patch

import httpx

import server


class ServiceRestartTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=server.app), base_url="http://test"
        )
        self.addAsyncCleanup(self.client.aclose)

    async def test_restart_is_scheduled_outside_the_web_service(self):
        with patch.dict(os.environ, {"INVOCATION_ID": "test"}), patch(
            "server.subprocess.run", return_value=subprocess.CompletedProcess([], 0)
        ) as run:
            previous = (await self.client.get("/api/service")).json()
            response = await self.client.post("/api/service/restart")
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json(), previous)
        command = run.call_args.args[0]
        self.assertEqual(command[0], "systemd-run")
        self.assertIn("--on-active=2s", command)
        self.assertEqual(command[-4:], ["systemctl", "--user", "restart", "kraken-web.service"])

    async def test_foreground_server_cannot_restart_another_instance(self):
        with patch.dict(os.environ, {}, clear=True), patch("server.subprocess.run") as run:
            response = await self.client.post("/api/service/restart")
        self.assertEqual(response.status_code, 409)
        run.assert_not_called()

    async def test_systemd_failure_is_reported(self):
        with patch.dict(os.environ, {"INVOCATION_ID": "test"}), patch(
            "server.subprocess.run",
            return_value=subprocess.CompletedProcess([], 1, stderr="Failed to connect to user bus"),
        ):
            response = await self.client.post("/api/service/restart")
        self.assertEqual(response.status_code, 503)
        self.assertIn("Failed to connect to user bus", response.json()["detail"])

    async def test_missing_systemd_or_timeout_is_reported(self):
        for error in (FileNotFoundError("systemd-run"), subprocess.TimeoutExpired("systemd-run", 5)):
            with self.subTest(error=error), patch.dict(os.environ, {"INVOCATION_ID": "test"}), patch(
                "server.subprocess.run", side_effect=error
            ):
                response = await self.client.post("/api/service/restart")
                self.assertEqual(response.status_code, 503)


if __name__ == "__main__":
    unittest.main()

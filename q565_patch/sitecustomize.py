"""Opt-in Q565 acceleration for CoolerControl's liquidctl sidecar."""

from __future__ import annotations

import logging
import grp
import os
import threading
import time

from PIL import Image, ImageDraw
from q565_rust import py_encode

from liquidctl.driver.kraken3 import KrakenZ3


_LOGGER = logging.getLogger("kraken_q565")
_ORIGINAL_SET_SCREEN = KrakenZ3.set_screen
_Q565_PRODUCTS = {0x300C, 0x3012}
_COMMON_HEADER = [
    0x12,
    0xFA,
    0x01,
    0xE8,
    0xAB,
    0xCD,
    0xEF,
    0x98,
    0x76,
    0x54,
    0x32,
    0x10,
]
_LIQCTLD_SOCKET = "/run/coolercontrold-liqctld.sock"


def _grant_socket_access() -> None:
    try:
        initial_inode = os.stat(_LIQCTLD_SOCKET).st_ino
    except FileNotFoundError:
        initial_inode = None
    group_name = os.environ.get("KRAKEN_Q565_GROUP", "bruce")
    try:
        group_id = grp.getgrnam(group_name).gr_gid
    except KeyError:
        _LOGGER.warning("Q565 socket group %s does not exist", group_name)
        return
    for _ in range(200):
        try:
            stat = os.stat(_LIQCTLD_SOCKET)
            if stat.st_ino != initial_inode:
                os.chown(_LIQCTLD_SOCKET, -1, group_id)
                os.chmod(_LIQCTLD_SOCKET, 0o660)
                return
        except FileNotFoundError:
            pass
        time.sleep(0.05)
    _LOGGER.warning("Timed out waiting for the liqctld socket")


def _result(message: list[int]) -> bool:
    return message[14] == 1


def _prepare_frame(device: KrakenZ3, path: str) -> bytes:
    image = Image.open(path).convert("RGB")
    image = image.resize(device.lcd_resolution).rotate(device.orientation * -90)

    # Pixels outside the physical panel are invisible and compress best as black.
    mask = Image.new("1", device.lcd_resolution)
    ImageDraw.Draw(mask).ellipse((0, 0, *device.lcd_resolution), fill=1)
    image = Image.composite(image, Image.new("RGB", device.lcd_resolution), mask)
    return py_encode(image.width, image.height, image.tobytes())


def _send_frame(device: KrakenZ3, data: bytes) -> None:
    device.device.clear_enqueued_reports()
    device._write([0x36, 0x01, 0x00, 0x01, 0x08])
    started = device._read_until_first_match({b"\x37\x01": _result})
    if not started:
        raise RuntimeError("Kraken rejected Q565 frame start")

    header = _COMMON_HEADER + [0x08, 0x00, 0x00, 0x00] + list(
        len(data).to_bytes(4, "little")
    )
    device._bulk_write(header)
    device._bulk_write(data)
    device._write([0x36, 0x02])
    finished = device._read_until_first_match({b"\x37\x02": _result})
    if not finished:
        raise RuntimeError("Kraken rejected Q565 frame completion")


def _set_screen_q565(
    self: KrakenZ3, channel: str, mode: str, value: str | int | None, **kwargs
):
    if (
        channel.lower() == "lcd"
        and mode == "static"
        and self.device.product_id in _Q565_PRODUCTS
        and isinstance(value, str)
    ):
        _send_frame(self, _prepare_frame(self, value))
        return None
    return _ORIGINAL_SET_SCREEN(self, channel, mode, value, **kwargs)


KrakenZ3.set_screen = _set_screen_q565
threading.Thread(target=_grant_socket_access, daemon=True).start()
_LOGGER.info("Enabled Q565 accelerated LCD frames for Kraken Elite")

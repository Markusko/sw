"""Cameras: a live view in the browser, from an RTSP stream or a snapshot URL.

No browser can play RTSP, so something has to convert it.  Three ways in,
tried in this order, so a camera works with whatever the machine can offer:

  1. ``rtsp_url`` with ffmpeg on PATH -- transcoded to MJPEG and streamed
     straight into an ``<img>``.  Works in every browser, no player needed.
  2. ``snapshot_url`` -- the still-image endpoint most cameras also expose,
     fetched on a timer.  No ffmpeg, no transcoding, a frame a second.
  3. neither -- the dashboard says so instead of showing a broken picture.

Credentials usually live inside the RTSP URL, so URLs are masked before they
are handed back out of the API.
"""

from __future__ import annotations

import re
import shutil
import struct
import subprocess
import time
import urllib.request
import zlib
from typing import Any, Iterator
from urllib.parse import urlsplit, urlunsplit

from . import net

FFMPEG_TIMEOUT = 15.0
FRAME_RATE = 5  # plenty for a wall dashboard, and gentle on the CPU
JPEG_START = b"\xff\xd8"
JPEG_END = b"\xff\xd9"


class CameraError(Exception):
    pass


def have_ffmpeg() -> bool:
    return bool(shutil.which("ffmpeg"))


def mask_url(url: str) -> str:
    """rtsp://user:secret@host/stream -> rtsp://user:***@host/stream"""
    if not url:
        return ""
    try:
        parts = urlsplit(url)
    except ValueError:
        return "(unreadable)"
    if not parts.hostname:
        return url
    host = parts.hostname + (f":{parts.port}" if parts.port else "")
    if parts.username:
        host = f"{parts.username}:***@{host}"
    return urlunsplit((parts.scheme, host, parts.path, "", ""))


def describe(camera: dict[str, Any]) -> dict[str, Any]:
    """What the API may say about a camera -- never the credentials."""
    return {
        "id": camera["id"],
        "name": camera.get("name", "Camera"),
        "room": camera.get("room"),
        "url": mask_url(camera.get("rtsp_url", "")),
        "snapshot_url": mask_url(camera.get("snapshot_url", "")),
        "mode": mode(camera),
        "demo": bool(camera.get("demo")),
    }


def mode(camera: dict[str, Any]) -> str:
    if camera.get("demo"):
        return "demo"
    if camera.get("rtsp_url") and have_ffmpeg():
        return "stream"
    if camera.get("snapshot_url"):
        return "snapshot"
    if camera.get("rtsp_url"):
        return "needs_ffmpeg"
    return "unconfigured"


# --- single frames -----------------------------------------------------------


def frame(camera: dict[str, Any]) -> tuple[str, bytes]:
    """One still image, for the thumbnail on a room card."""
    how = mode(camera)
    if how == "demo":
        return "image/png", demo_frame()
    if how == "snapshot":
        return _fetch(camera["snapshot_url"])
    if how == "stream":
        return "image/jpeg", _ffmpeg_still(camera["rtsp_url"])
    if how == "needs_ffmpeg":
        raise CameraError("ffmpeg is not installed, so an RTSP stream cannot be shown")
    raise CameraError("this camera has neither an RTSP nor a snapshot address")


def _fetch(url: str) -> tuple[str, bytes]:
    try:
        request = urllib.request.Request(url, headers={"Accept": "image/*"})
        with urllib.request.urlopen(request, timeout=net.TIMEOUT) as response:
            return response.headers.get("Content-Type", "image/jpeg"), response.read()
    except Exception as error:
        raise CameraError(f"cannot fetch the snapshot: {error}") from error


def _ffmpeg_still(url: str) -> bytes:
    command = [
        "ffmpeg", "-nostdin", "-loglevel", "error",
        "-rtsp_transport", "tcp", "-i", url,
        "-frames:v", "1", "-f", "image2", "-c:v", "mjpeg", "-",
    ]
    try:
        done = subprocess.run(command, capture_output=True, timeout=FFMPEG_TIMEOUT, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise CameraError(f"ffmpeg could not read the stream: {error}") from error
    if not done.stdout:
        raise CameraError(_ffmpeg_reason(done.stderr))
    return done.stdout


def _ffmpeg_reason(stderr: bytes) -> str:
    text = stderr.decode("utf-8", "replace").strip().splitlines()
    return text[-1][:200] if text else "ffmpeg produced no image"


# --- live view ---------------------------------------------------------------


def frames(camera: dict[str, Any], should_stop) -> Iterator[tuple[str, bytes]]:
    """A live sequence of frames, however this camera can provide one."""
    how = mode(camera)
    if how == "demo":
        yield from _demo_frames(should_stop)
    elif how == "stream":
        yield from _ffmpeg_frames(camera["rtsp_url"], should_stop)
    elif how == "snapshot":
        yield from _polled_frames(camera["snapshot_url"], should_stop)
    else:
        raise CameraError(
            "ffmpeg is not installed, so an RTSP stream cannot be shown"
            if how == "needs_ffmpeg"
            else "this camera has neither an RTSP nor a snapshot address"
        )


def _ffmpeg_frames(url: str, should_stop) -> Iterator[tuple[str, bytes]]:
    command = [
        "ffmpeg", "-nostdin", "-loglevel", "error",
        "-rtsp_transport", "tcp", "-i", url,
        "-f", "mjpeg", "-q:v", "6", "-r", str(FRAME_RATE), "-an", "-",
    ]
    try:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError as error:
        raise CameraError(f"cannot start ffmpeg: {error}") from error

    buffer = b""
    try:
        while not should_stop():
            chunk = process.stdout.read(32768)
            if not chunk:
                break
            buffer += chunk
            # ffmpeg writes one JPEG after another; cut them apart on the markers.
            while True:
                start = buffer.find(JPEG_START)
                end = buffer.find(JPEG_END, start + 2) if start >= 0 else -1
                if start < 0 or end < 0:
                    break
                yield "image/jpeg", buffer[start : end + 2]
                buffer = buffer[end + 2 :]
    finally:
        process.kill()
        try:
            process.communicate(timeout=2)
        except Exception:
            pass


def _polled_frames(url: str, should_stop) -> Iterator[tuple[str, bytes]]:
    while not should_stop():
        try:
            yield _fetch(url)
        except CameraError:
            return
        time.sleep(1.0)


# --- the demo camera ---------------------------------------------------------
# A generated picture, so the camera plumbing and the room view can be seen
# working on a machine with no camera and no ffmpeg.


def _png(width: int, height: int, pixels: bytes) -> bytes:
    rows = b"".join(b"\x00" + pixels[y * width * 3 : (y + 1) * width * 3] for y in range(height))

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(rows, 6))
        + chunk(b"IEND", b"")
    )


def demo_frame(width: int = 320, height: int = 180) -> bytes:
    """A drifting gradient with a sweeping bar, so movement is obvious."""
    phase = time.time() % 4 / 4
    sweep = int(phase * width)
    pixels = bytearray()
    for y in range(height):
        for x in range(width):
            near = abs(x - sweep) < 3
            pixels += bytes(
                (255, 255, 255) if near else (30 + x * 90 // width, 40 + y * 120 // height, 90 + x * 60 // width)
            )
    return _png(width, height, bytes(pixels))


def _demo_frames(should_stop) -> Iterator[tuple[str, bytes]]:
    while not should_stop():
        yield "image/png", demo_frame()
        time.sleep(0.5)


# --- configuration -----------------------------------------------------------

SCHEMES = ("rtsp://", "rtsps://", "http://", "https://")


def normalise(raw: dict[str, Any], existing: dict[str, Any] | None = None) -> dict[str, Any]:
    # On create the id arrives with the payload; on update the stored one wins.
    base = existing or {"id": str(raw.get("id", "")), "rtsp_url": "", "snapshot_url": "", "room": None}
    if not base["id"]:
        raise ValueError("a camera needs an id")
    rtsp = str(raw.get("rtsp_url", base["rtsp_url"])).strip()
    still = str(raw.get("snapshot_url", base["snapshot_url"])).strip()
    for url in (rtsp, still):
        if url and not url.lower().startswith(SCHEMES):
            raise ValueError(f"{url[:40]}… is not an rtsp:// or http:// address")
    room = raw.get("room", base["room"])
    return {
        **base,
        "name": str(raw.get("name", base.get("name", "Camera"))).strip()[:60] or "Camera",
        "rtsp_url": rtsp,
        "snapshot_url": still,
        "room": str(room) if room else None,
    }


def redact(text: str) -> str:
    """Keep credentials out of error messages that reach the browser."""
    return re.sub(r"//[^/@\s]+:[^/@\s]+@", "//***:***@", text)

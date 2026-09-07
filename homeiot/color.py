"""Colour maths for lamps: CIE xy <-> sRGB, mired <-> kelvin <-> sRGB.

Pure functions only, no state.  The matrices are the "Wide RGB D65" ones
Philips publishes for the Hue gamuts; they are what the lamps expect.
"""

from __future__ import annotations

import math
from typing import Iterable, Sequence

Point = tuple[float, float]
Gamut = tuple[Point, Point, Point]  # red, green, blue primaries

GAMUT_A: Gamut = ((0.704, 0.296), (0.2151, 0.7106), (0.138, 0.08))
GAMUT_B: Gamut = ((0.675, 0.322), (0.409, 0.518), (0.167, 0.04))
GAMUT_C: Gamut = ((0.6915, 0.3038), (0.17, 0.7), (0.1532, 0.0475))
GAMUTS = {"A": GAMUT_A, "B": GAMUT_B, "C": GAMUT_C}


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return low if value < low else high if value > high else value


# --- sRGB companding ---------------------------------------------------------


def _gamma(channel: float) -> float:
    return 12.92 * channel if channel <= 0.0031308 else 1.055 * channel ** (1 / 2.4) - 0.055


def _ungamma(channel: float) -> float:
    return channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4


# --- hex helpers -------------------------------------------------------------


def hex_to_rgb(value: str) -> tuple[float, float, float]:
    text = value.strip().lstrip("#")
    if len(text) == 3:
        text = "".join(char * 2 for char in text)
    if len(text) != 6:
        raise ValueError(f"not a colour: {value!r}")
    return tuple(int(text[i : i + 2], 16) / 255 for i in (0, 2, 4))  # type: ignore[return-value]


def rgb_to_hex(rgb: Sequence[float]) -> str:
    return "#" + "".join(f"{round(clamp(channel) * 255):02x}" for channel in rgb)


# --- gamut geometry ----------------------------------------------------------


def _cross(a: Point, b: Point) -> float:
    return a[0] * b[1] - a[1] * b[0]


def _closest_on_segment(start: Point, end: Point, point: Point) -> Point:
    line = (end[0] - start[0], end[1] - start[1])
    offset = (point[0] - start[0], point[1] - start[1])
    length = line[0] ** 2 + line[1] ** 2
    if length == 0:
        return start
    t = clamp((offset[0] * line[0] + offset[1] * line[1]) / length)
    return (start[0] + line[0] * t, start[1] + line[1] * t)


def in_gamut(point: Point, gamut: Gamut) -> bool:
    red, green, blue = gamut
    to_point = (point[0] - red[0], point[1] - red[1])
    to_green = (green[0] - red[0], green[1] - red[1])
    to_blue = (blue[0] - red[0], blue[1] - red[1])
    s = _cross(to_point, to_blue) / _cross(to_green, to_blue)
    t = _cross(to_green, to_point) / _cross(to_green, to_blue)
    return s >= 0 and t >= 0 and s + t <= 1


def clamp_to_gamut(point: Point, gamut: Gamut | None) -> Point:
    """Move an xy point onto the nearest edge of the lamp's triangle."""
    if gamut is None or in_gamut(point, gamut):
        return point
    red, green, blue = gamut
    candidates = (
        _closest_on_segment(red, green, point),
        _closest_on_segment(green, blue, point),
        _closest_on_segment(blue, red, point),
    )
    return min(candidates, key=lambda c: (c[0] - point[0]) ** 2 + (c[1] - point[1]) ** 2)


def gamut_from_light(light: dict) -> Gamut | None:
    """Read a gamut out of a CLIP v2 `light` resource, falling back on its type."""
    color = light.get("color") or {}
    raw = color.get("gamut")
    if isinstance(raw, dict) and {"red", "green", "blue"} <= raw.keys():
        try:
            return tuple(  # type: ignore[return-value]
                (float(raw[key]["x"]), float(raw[key]["y"])) for key in ("red", "green", "blue")
            )
        except (KeyError, TypeError, ValueError):
            pass
    return GAMUTS.get(str(color.get("gamut_type") or "C").upper())


# --- conversions -------------------------------------------------------------


def xy_to_hex(x: float, y: float, brightness: float = 100.0) -> str:
    """xy + brightness (0..100) -> display colour.

    Brightness is folded in gently: a dimmed lamp should still show its hue in
    the UI, so the luminance floor keeps swatches readable.
    """
    luminance = clamp(0.35 + 0.65 * clamp(brightness / 100))
    if y <= 0:
        return "#000000"
    big_x = (luminance / y) * x
    big_z = (luminance / y) * (1 - x - y)
    r = big_x * 1.656492 - luminance * 0.354851 - big_z * 0.255038
    g = -big_x * 0.707196 + luminance * 1.655397 + big_z * 0.036152
    b = big_x * 0.051713 - luminance * 0.121364 + big_z * 1.011530
    channels = [max(0.0, channel) for channel in (r, g, b)]
    peak = max(channels)
    if peak > 1:
        channels = [channel / peak for channel in channels]
    return rgb_to_hex([_gamma(channel) for channel in channels])


def hex_to_xy(value: str, gamut: Gamut | None = GAMUT_C) -> Point:
    r, g, b = (_ungamma(channel) for channel in hex_to_rgb(value))
    big_x = r * 0.664511 + g * 0.154324 + b * 0.162028
    big_y = r * 0.283881 + g * 0.668433 + b * 0.047685
    big_z = r * 0.000088 + g * 0.072310 + b * 0.986039
    total = big_x + big_y + big_z
    if total == 0:
        return clamp_to_gamut((0.3227, 0.3290), gamut)
    return clamp_to_gamut((big_x / total, big_y / total), gamut)


def mirek_to_kelvin(mirek: float) -> float:
    return 1_000_000 / mirek if mirek else 0.0


def kelvin_to_mirek(kelvin: float) -> float:
    return 1_000_000 / kelvin if kelvin else 0.0


def mirek_to_hex(mirek: float) -> str:
    """Approximate a colour temperature as a display colour (Helland's fit)."""
    if not mirek:
        return "#ffffff"
    temperature = clamp(mirek_to_kelvin(mirek), 1000, 40000) / 100
    if temperature <= 66:
        red = 255.0
        green = 99.4708025861 * math.log(temperature) - 161.1195681661
    else:
        red = 329.698727446 * (temperature - 60) ** -0.1332047592
        green = 288.1221695283 * (temperature - 60) ** -0.0755148492
    if temperature >= 66:
        blue = 255.0
    elif temperature <= 19:
        blue = 0.0
    else:
        blue = 138.5177312231 * math.log(temperature - 10) - 305.0447927307
    return rgb_to_hex([clamp(channel / 255) for channel in (red, green, blue)])


def describe_kelvin(mirek: float) -> str:
    return f"{round(mirek_to_kelvin(mirek) / 50) * 50:.0f}K" if mirek else "—"


def average_hex(colors: Iterable[str]) -> str | None:
    """Blend swatches so a room can show one representative colour."""
    values = [hex_to_rgb(color) for color in colors if color]
    if not values:
        return None
    count = len(values)
    return rgb_to_hex([sum(channel[i] for channel in values) / count for i in range(3)])

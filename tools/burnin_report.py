#!/usr/bin/env python3
"""Estimate how much OLED wear the clock's anti burn-in drift actually prevents.

Every element on screen is a fixed image translated by a drift offset d(t).
Cumulative wear is the time-integral of luminance, and "translate, then
integrate" is exactly a convolution:

    wear(x) = Σ_elements  E[element](x)  ⊛  p_drift_of_that_element(x)

where E[element] is the time-averaged image of that element with no drift
(each glyph, icon and temperature reading weighted by how often it is shown)
and p_drift is the occupancy histogram of its drift path.  Wear is linear in
luminance, so one convolution per element gives an exact map for an
arbitrarily long run, in about a second.

Everything is read from the running code — `clock.Assets` supplies the real
scaled artwork, the real positions and the real drift objects — so this report
cannot quietly diverge from what is on the panel.

Two numbers come out of it:

  peak wear   - the worst pixel's total luminance·time.  This is what
                physically ages the panel, and drift barely moves it: the
                strokes of these glyphs are far wider than any offset that
                fits on screen, so the middle of a stroke is lit either way.
                Brightness and duty cycle are the levers here, not motion.
  ghost edge  - the largest gradient of the wear map.  This is what makes
                burn-in *visible*: a sharp step between worn and unworn pixels
                reads as a legible ghost of the digits, a smooth ramp does
                not.  Drift attacks this directly, and it is the real win.

This is a model, not a panel simulation.  Wear is taken as linear in
luminance × time, which understates real OLED aging (roughly superlinear in
drive current, with blue degrading fastest), so read the ratios as relative
rather than as a forecast.

Needs numpy and Pillow (development only; the clock itself needs neither):
    pip install numpy pillow
Run:
    python3 tools/burnin_report.py [--days 30] [--config config.json] [--heatmap out.png]
"""

from __future__ import annotations

import argparse
import math
import os
import pathlib
import sys

import numpy as np

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SCREEN_W, SCREEN_H = 1920, 1080
DAY_FRACTION = 0.5

# How often a 12-hour clock shows each glyph in each of the six slots.
SLOT_PROBS = [
    {"0": 9 / 12, "1": 3 / 12},                                        # hour tens
    {**{d: 1 / 12 for d in "34567890"}, "1": 2 / 12, "2": 2 / 12},     # hour ones
    {"colon": 1.0},
    {d: 1 / 6 for d in "012345"},                                      # minute tens
    {d: 1 / 10 for d in "0123456789"},                                 # minute ones
    {"am": 0.5, "pm": 0.5},
]

# A plausible spread of indoor readings, so the temperature block is averaged
# over the digits it actually shows rather than one frozen value.
TEMPERATURES = range(58, 86)


def luma(rgb: np.ndarray) -> np.ndarray:
    return 0.2126 * rgb[..., 0] + 0.7152 * rgb[..., 1] + 0.0722 * rgb[..., 2]


def surface_luma(surf) -> np.ndarray:
    """(h, w) luminance of a pygame surface."""
    import pygame
    return luma(pygame.surfarray.array3d(surf).transpose(1, 0, 2).astype(np.float32))


def sprite_luma(spr) -> np.ndarray:
    """Same, for a Sprite, with its 1px border removed."""
    return surface_luma(spr.img)[1:-1, 1:-1]


def place(canvas: np.ndarray, patch: np.ndarray, x: int, y: int, weight: float) -> None:
    h, w = patch.shape
    y0, x0 = max(0, y), max(0, x)
    y1, x1 = min(canvas.shape[0], y + h), min(canvas.shape[1], x + w)
    if y1 <= y0 or x1 <= x0:
        return
    canvas[y0:y1, x0:x1] += patch[y0 - y:y1 - y, x0 - x:x1 - x] * weight


def build_layers(cfg, assets) -> list[tuple[np.ndarray, object, str]]:
    """One time-averaged luminance layer per independently drifting element."""
    A = assets
    pad = cfg.padding
    layers: list[tuple[np.ndarray, object, str]] = []

    # ── digits ───────────────────────────────────────────────────────────
    strip = np.zeros((SCREEN_H, SCREEN_W), np.float32)
    widths = (A.w_full, A.w_full, A.w_half, A.w_full, A.w_full, A.w_half)
    x = int(A.strip_x)
    for probs, w in zip(SLOT_PROBS, widths):
        for name, p in probs.items():
            for theme, tf in (("day", DAY_FRACTION), ("night", 1 - DAY_FRACTION)):
                place(strip, surface_luma(A.digits[theme][name]), x, int(A.strip_y), p * tf)
        x += w
    layers.append((strip, A.drift_digits, "digits"))

    # ── temperature ──────────────────────────────────────────────────────
    temp = np.zeros((SCREEN_H, SCREEN_W), np.float32)
    for theme, tf, raw, b in (
        ("day", DAY_FRACTION, cfg.temp_color_day, cfg.temp_brightness_day),
        ("night", 1 - DAY_FRACTION, cfg.temp_color_night, cfg.temp_brightness_night),
    ):
        colour = tuple(min(255, int(c * b)) for c in raw)
        for value in TEMPERATURES:
            spr = A.temp_sprite(f"{value}\N{DEGREE SIGN}{cfg.temp_unit}", colour)
            place(temp, sprite_luma(spr), int((SCREEN_W - spr.w) / 2), int(A.temp_ink_top),
                  tf / len(TEMPERATURES))
    layers.append((temp, A.drift_temp, "temperature"))

    # ── weather corner ───────────────────────────────────────────────────
    weather = np.zeros((SCREEN_H, SCREEN_W), np.float32)
    for theme, tf in (("day", DAY_FRACTION), ("night", 1 - DAY_FRACTION)):
        icons = A.weather[theme]
        for spr in icons.values():
            place(weather, sprite_luma(spr), pad, pad, tf / max(1, len(icons)))
    if weather.any():
        layers.append((weather, A.drift_weather, "weather icon"))

    # ── sun / moon corner ────────────────────────────────────────────────
    corner = np.zeros((SCREEN_H, SCREEN_W), np.float32)
    for spr in A.sun.values():
        place(corner, sprite_luma(spr), SCREEN_W - spr.w - pad, pad,
              DAY_FRACTION / len(A.sun))
    for spr in A.moon.values():
        place(corner, sprite_luma(spr), SCREEN_W - spr.w - pad, pad,
              (1 - DAY_FRACTION) / len(A.moon))
    layers.append((corner, A.drift_corner, "sun/moon icon"))

    return layers


def drift_occupancy(drift, days: int, step_s: int = 30) -> np.ndarray:
    """Histogram of the path this exact Drift object walks, over `days` days."""
    R = int(math.ceil(max(drift.amp_x, drift.amp_y))) + 2
    k = np.zeros((2 * R + 1, 2 * R + 1), np.float64)
    for t in range(0, days * 86400, step_s):
        dx, dy = drift.at(t)
        k[round(dy) + R, round(dx) + R] += 1
    return k / k.sum()


def jump_occupancy(radius: int, days: int, seed: int = 7) -> np.ndarray:
    """The old design: a uniform random jump of +/-radius every five minutes."""
    rng = np.random.default_rng(seed)
    R = radius + 2
    k = np.zeros((2 * R + 1, 2 * R + 1), np.float64)
    for _ in range(days * 24 * 12):
        k[int(rng.integers(-radius, radius + 1)) + R,
          int(rng.integers(-radius, radius + 1)) + R] += 1
    return k / k.sum()


def still_occupancy() -> np.ndarray:
    k = np.zeros((3, 3))
    k[1, 1] = 1.0
    return k


def convolve(img: np.ndarray, ker: np.ndarray) -> np.ndarray:
    ih, iw = img.shape
    kh, kw = ker.shape
    fh = 1 << (ih + kh - 2).bit_length()
    fw = 1 << (iw + kw - 2).bit_length()
    out = np.fft.irfft2(np.fft.rfft2(img, (fh, fw)) * np.fft.rfft2(ker, (fh, fw)), (fh, fw))
    return out[kh // 2:kh // 2 + ih, kw // 2:kw // 2 + iw]


def wear_map(layers, kernel_for) -> np.ndarray:
    total = np.zeros((SCREEN_H, SCREEN_W), np.float64)
    for layer, drift, _ in layers:
        total += convolve(layer.astype(np.float64), kernel_for(drift))
    return total


def report(name: str, wear: np.ndarray, ref: tuple | None) -> tuple:
    gy, gx = np.gradient(wear)
    edge = float(np.hypot(gx, gy).max())
    peak = float(wear.max())
    lit = wear[wear > peak * 0.02]
    rel = ""
    if ref:
        rel = f"   {ref[0] / peak:5.2f}x peak   {ref[1] / edge:7.1f}x ghost edge"
    print(f"  {name:<26s} peak {peak:8.1f}   peak/mean {peak / lit.mean():4.2f}"
          f"   ghost edge {edge:7.3f}{rel}")
    return peak, edge


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", type=pathlib.Path, default=ROOT / "config.json")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--heatmap", type=pathlib.Path, help="write the wear map as a PNG")
    ap.add_argument("--per-element", action="store_true",
                    help="also report each element's own worst pixel")
    args = ap.parse_args()

    os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
    import pygame
    import clock as C                                  # noqa: E402 - after sys.path setup

    pygame.display.init()
    pygame.font.init()
    pygame.display.set_mode((320, 240))

    cfg = C.load_config(args.config)
    assets = C.Assets(cfg, SCREEN_W, SCREEN_H)
    d = assets.drift_digits
    print(f"theme {cfg.theme}: strip {assets.strip_w}x{assets.digit_h}, "
          f"top_reserved {assets.top_reserved}, digits drift +/-{d.amp_x:.0f}/+/-{d.amp_y:.0f} px, "
          f"peak speed {d.peak_speed():.4f} px/s "
          f"(1 px every {1 / max(d.peak_speed(), 1e-9):.0f} s)")

    layers = build_layers(cfg, assets)

    # night_dim multiplies the whole canvas on top of brightness_night, so fold
    # it into a scalar on the night half of every layer.  Layers are already a
    # day/night mix, so apply it as an overall factor on total emitted light.
    nd = cfg.night_dim
    dim_factor = 1.0
    if nd.enabled:
        span = ((nd.end - nd.start) % (24 * 60)) or 24 * 60
        night_h = 12.0                                  # rough annual mean
        inside = min(span / 60.0, night_h)
        dim_factor = 1.0 - (1.0 - nd.brightness) * (inside / 24.0)
        print(f"night_dim: {inside:.1f}h/day at x{nd.brightness} -> "
              f"x{dim_factor:.3f} on total emitted light")
    print(f"elements: {', '.join(n for _, _, n in layers)}")
    print(f"simulating {args.days} days\n")

    ref = report("no drift at all", wear_map(layers, lambda _: still_occupancy()) * dim_factor,
                 None)
    report("old: +/-6 px every 5 min",
           wear_map(layers, lambda _: jump_occupancy(6, args.days)) * dim_factor, ref)
    wear = wear_map(layers, lambda dr: drift_occupancy(dr, args.days)) * dim_factor
    report("this build", wear, ref)

    if args.per_element:
        print()
        for layer, drift, name in layers:
            still = convolve(layer.astype(np.float64), still_occupancy())
            moved = convolve(layer.astype(np.float64), drift_occupancy(drift, args.days))
            gy, gx = np.gradient(moved)
            gy0, gx0 = np.gradient(still)
            print(f"  {name:<16s} +/-{drift.amp_x:>4.0f}/+/-{drift.amp_y:<4.0f} px  "
                  f"peak {still.max():7.1f} -> {moved.max():7.1f}   "
                  f"ghost edge {np.hypot(gx0, gy0).max():7.2f} -> {np.hypot(gx, gy).max():6.2f}")

    print("\n  peak wear is set by total emitted light, so drift can only nibble at it;"
          "\n  lower brightness_night or enable night_dim to move that number."
          "\n  ghost edge is what you would actually see, and that is where drift wins.")

    if args.heatmap:
        from PIL import Image
        Image.fromarray((wear / wear.max() * 255).astype(np.uint8)).save(args.heatmap)
        print(f"\nwrote {args.heatmap}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

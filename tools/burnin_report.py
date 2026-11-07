#!/usr/bin/env python3
"""Forecast how long the panel lasts before a ghost shows, for a given config.

Burn-in is measured against the one panel we know the answer for: the first
one, which ran from May 2025 with a ±6 px jump every five minutes and was
visibly burned in by September 2026.  Everything is reported as "this config
takes N times as long to reach that panel's ghost".

How a year of wear is built up:

  * The year is replayed minute by minute: real sunrise and sunset at the
    configured location, `time_adjust`, `night_dim`, which glyph is in which
    slot, the weekday sun and the moon phase.  The temperature uses the
    habitat's measured spread (it read 70-78°F all year, so the "7" and "°F"
    never change), the weather a climatological guess.
  * Wear is per subpixel and in linear light: brightness settings multiply
    the sRGB code values, so `0.7` emits about 45% of the light, not 70%.
  * Aging is superlinear in drive, stress = L^n with n ≈ 1.7 for OLED
    emitters, and the luminance loss grows like (stress·time)^β with β ≈ 0.6.
    Blue ages about three times faster than red and green.
  * Drift is a convolution: the time-averaged image of each element, smeared
    by the occupancy of that element's exact drift path over the year.
  * What makes a ghost *visible* is judged through the eye's contrast
    sensitivity at real viewing distances on this panel's 0.0634 mm pixels.
    A sharp edge you cannot resolve from across the room does not count; a
    soft blob the size of an icon does.

The constants are not published for this module, so read the output as
relative — this config against the old panel — not as an absolute lifetime.
The ranking of what helps holds across n = 1.3-2.0 and β = 0.5-0.7.

Needs numpy and Pillow (development only; the clock itself needs neither):
    pip install numpy pillow
Run:
    python3 tools/burnin_report.py [--config config.json] [--per-element] [--heatmap out.png]
"""

from __future__ import annotations

import argparse
import dataclasses
import math
import os
import pathlib
import sys
from datetime import datetime, timedelta

import numpy as np

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SCREEN_W, SCREEN_H = 1920, 1080
PITCH_MM = 5.5 * 25.4 / math.hypot(1080, 1920)        # Waveshare 5.5" AMOLED, 0.0634 mm
LUMA = np.array([0.2126, 0.7152, 0.0722])
BLUE_AGING = 3.0
DISTANCES = (1.0, 2.0)                                # metres

# Measured: the habitat's displayed reading over a year (portal habitat/api/data?range=year).
TEMPERATURES = {70: .022, 71: .113, 72: .185, 73: .138, 74: .110, 75: .152,
                76: .176, 77: .085, 78: .019}
# A guess for southern New Hampshire; the weather endpoint keeps no history.
WEATHER_DAY = {"clear_day": .22, "scattered": .14, "clouds": .20, "overcast": .20, "rain": .08,
               "drizzle": .03, "shower": .02, "mist": .03, "snow_day": .06, "thunderstorm": .02}
WEATHER_NIGHT = {**{k: v for k, v in WEATHER_DAY.items() if k not in ("clear_day", "snow_day")},
                 "clear_night": .22, "snow_night": .06}

# The panel that burned in, and the settings it ran at.
OLD_PANEL = dict(overall_brightness=1.0, brightness_day=1.0, brightness_night=0.8,
                 brightness_sun=1.0, brightness_moon=0.7, brightness_weather_day=1.0,
                 brightness_weather_night=0.5, temp_brightness_day=1.0, temp_brightness_night=0.5)
OLD_JUMP_PX = 6
ELEMENTS = ("digits", "temp", "corner", "weather")


def srgb_to_linear(code: np.ndarray) -> np.ndarray:
    c = np.asarray(code, np.float64) / 255.0
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)


def rgb(surf) -> np.ndarray:
    import pygame
    return pygame.surfarray.array3d(surf).transpose(1, 0, 2).astype(np.float64)


def year_of_minutes(C, cfg, days: int) -> dict:
    """Hours per day spent in each displayed state, keyed per element."""
    tz = C.resolve_tz(cfg.timezone)
    sky = C.SkyCache(cfg, tz)
    nd = cfg.night_dim
    start = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    hours: dict[str, dict] = {e: {} for e in ELEMENTS}

    def add(element, key):
        hours[element][key] = hours[element].get(key, 0) + 1

    for minute in range(days * 1440):
        now = start + timedelta(minutes=minute)
        theme = "day" if sky.is_day(now) else "night"
        shown = now + timedelta(hours=cfg.time_adjust)
        dim = 1.0
        if nd.enabled:                                # mirrors Clock.dim_factor
            m = shown.hour * 60 + shown.minute
            inside = (nd.start <= m < nd.end) if nd.start <= nd.end else (m >= nd.start or m < nd.end)
            dim = nd.brightness if inside else 1.0
        for slot, glyph in enumerate(C.glyph_sequence(shown)):
            add("digits", (slot, glyph, theme, dim))
        icon = shown.strftime("%A").lower() if theme == "day" else sky.moon_icon(shown.date())
        add("corner", (icon, theme, dim))
        add("temp", (theme, dim))
        add("weather", (theme, dim))
    per_day = 1.0 / (days * 60.0)
    return {e: {k: v * per_day for k, v in h.items()} for e, h in hours.items()}


def place(canvas: np.ndarray, patch: np.ndarray, x: float, y: float, weight: float) -> None:
    h, w = patch.shape[:2]
    x, y = int(round(x)), int(round(y))
    y0, x0 = max(0, y), max(0, x)
    y1, x1 = min(canvas.shape[0], y + h), min(canvas.shape[1], x + w)
    if y1 > y0 and x1 > x0:
        canvas[y0:y1, x0:x1] += patch[y0 - y:y1 - y, x0 - x:x1 - x] * weight


def stress_layers(cfg, A, hours: dict, n: float) -> dict[str, np.ndarray]:
    """Per element, the stress each subpixel takes per day with no motion, in
    hours of full drive.  Brightness is already baked into the artwork by
    `Assets`, exactly as on screen; night_dim multiplies on top."""
    def stress(code, dim):
        return srgb_to_linear(code * dim) ** n

    layers = {e: np.zeros((SCREEN_H, SCREEN_W, 3)) for e in ELEMENTS}
    offsets = [0, A.w_full, 2 * A.w_full, 2 * A.w_full + A.w_half,
               3 * A.w_full + A.w_half, 4 * A.w_full + A.w_half]
    cache: dict = {}
    for (slot, glyph, theme, dim), h in hours["digits"].items():
        key = (glyph, theme, dim)
        if key not in cache:
            cache[key] = stress(rgb(A.digits[theme][glyph]), dim)
        place(layers["digits"], cache[key], A.strip_x + offsets[slot], A.strip_y, h)
    for (theme, dim), h in hours["temp"].items():
        raw = cfg.temp_color_day if theme == "day" else cfg.temp_color_night
        b = cfg.temp_brightness_day if theme == "day" else cfg.temp_brightness_night
        colour = tuple(min(255, int(c * b * cfg.overall_brightness)) for c in raw)
        for value, p in TEMPERATURES.items():
            spr = A.temp_sprite(f"{value}\N{DEGREE SIGN}{cfg.temp_unit}", colour)
            place(layers["temp"], stress(rgb(spr.img)[1:-1, 1:-1], dim),
                  (SCREEN_W - spr.w) / 2.0, A.temp_ink_top, h * p)
    for (icon, theme, dim), h in hours["corner"].items():
        spr = (A.sun if theme == "day" else A.moon).get(icon)
        if spr is not None:
            place(layers["corner"], stress(rgb(spr.img)[1:-1, 1:-1], dim),
                  SCREEN_W - spr.w - cfg.padding, cfg.padding, h)
    for (theme, dim), h in hours["weather"].items():
        for name, p in (WEATHER_DAY if theme == "day" else WEATHER_NIGHT).items():
            spr = A.weather[theme].get(name)
            if spr is not None:
                place(layers["weather"], stress(rgb(spr.img)[1:-1, 1:-1], dim),
                      cfg.padding, cfg.padding, h * p)
    return layers


def still() -> np.ndarray:
    k = np.zeros((3, 3))
    k[1, 1] = 1.0
    return k


def jump(radius: int) -> np.ndarray:
    """The old design: a uniformly random jump within ±radius every five minutes."""
    return np.full((2 * radius + 1, 2 * radius + 1), 1.0 / (2 * radius + 1) ** 2)


def path(drift, days: int, step_s: float = 60.0) -> np.ndarray:
    """Occupancy of the path this exact Drift object walks, bilinearly splatted
    the way blit_subpixel draws it."""
    if drift.amp_x == 0 and drift.amp_y == 0:
        return still()
    t = datetime.now().timestamp() + np.arange(0, days * 86400, step_s)
    dx, dy = drift.at(t)
    R = int(math.ceil(max(drift.amp_x, drift.amp_y))) + 2
    k = np.zeros((2 * R + 1, 2 * R + 1))
    fx, fy = np.floor(dx), np.floor(dy)
    ux, uy = dx - fx, dy - fy
    ix, iy = fx.astype(int) + R, fy.astype(int) + R
    for ox, oy, w in ((0, 0, (1 - ux) * (1 - uy)), (1, 0, ux * (1 - uy)),
                      (0, 1, (1 - ux) * uy), (1, 1, ux * uy)):
        np.add.at(k, (iy + oy, ix + ox), w)
    return k / k.sum()


def convolve(img: np.ndarray, ker: np.ndarray) -> np.ndarray:
    ih, iw = img.shape
    kh, kw = ker.shape
    fh = 1 << (ih + kh).bit_length()
    fw = 1 << (iw + kw).bit_length()
    out = np.fft.irfft2(np.fft.rfft2(img, (fh, fw)) * np.fft.rfft2(ker, (fh, fw)), (fh, fw))
    return out[kh // 2:kh // 2 + ih, kw // 2:kw // 2 + iw]


def worn(layers: dict, kernels: dict) -> np.ndarray:
    total = np.zeros((SCREEN_H, SCREEN_W, 3))
    for name, layer in layers.items():
        for c in range(3):
            total[..., c] += convolve(layer[..., c], kernels[name])
    return np.clip(total, 0, None)


def damage(stress: np.ndarray, beta: float) -> np.ndarray:
    """Luminance lost on a plain white test screen, up to a common factor."""
    return ((stress * np.array([1.0, 1.0, BLUE_AGING])) ** beta) @ LUMA


def as_seen(D: np.ndarray, metres: float) -> np.ndarray:
    """D filtered by the eye's contrast sensitivity (peak 4 cycles/degree)."""
    ppd = metres * 1000 * math.tan(math.radians(1)) / PITCH_MM
    h, w = D.shape
    fh, fw = 1 << (h + h // 2).bit_length(), 1 << (w + w // 2).bit_length()   # no wrap-around
    f = np.hypot(np.fft.rfftfreq(fw)[None, :], np.fft.fftfreq(fh)[:, None]) * ppd
    csf = f ** 0.8 * np.exp(-0.2 * f)
    return np.abs(np.fft.irfft2(np.fft.rfft2(D, (fh, fw)) * csf / csf.max(), (fh, fw))[:h, :w])


REGIONS = {"digits": (0, SCREEN_W, 440, SCREEN_H), "temp": (440, 1480, 100, 440),
           "corner": (1480, SCREEN_W, 0, 440), "weather": (0, 440, 0, 440)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", type=pathlib.Path, default=ROOT / "config.json")
    ap.add_argument("--theme", help="override the config's theme")
    ap.add_argument("--days", type=int, default=365, help="length of the replay (a full year "
                    "by default, so every season's day length is in it)")
    ap.add_argument("--n", type=float, default=1.7, help="aging exponent on drive level")
    ap.add_argument("--beta", type=float, default=0.6, help="stretched-exponential exponent")
    ap.add_argument("--per-element", action="store_true",
                    help="also say which element's ghost would show first")
    ap.add_argument("--heatmap", type=pathlib.Path,
                    help="write this config's wear, as seen from 1 m, as a PNG")
    args = ap.parse_args()

    os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
    import pygame
    import clock as C                                  # noqa: E402 - after sys.path setup

    pygame.display.init()
    pygame.font.init()
    pygame.display.set_mode((320, 240))

    cfg = C.load_config(args.config)
    if args.theme:
        cfg = dataclasses.replace(cfg, theme=args.theme)
    old = dataclasses.replace(cfg, night_dim=C.NightDim(), **OLD_PANEL)
    A = C.Assets(cfg, SCREEN_W, SCREEN_H)
    A_old = C.Assets(old, SCREEN_W, SCREEN_H)
    d = A.drift_digits
    print(f"theme {cfg.theme}, overall_brightness {cfg.overall_brightness:g}: digits drift "
          f"±{d.amp_x:.0f}/±{d.amp_y:.0f} px at {d.peak_speed():.4f} px/s "
          f"(1 px every {1 / max(d.peak_speed(), 1e-9):.0f} s)")
    print(f"replaying {args.days} days minute by minute ...", flush=True)

    hours = year_of_minutes(C, cfg, args.days)
    hours_old = hours if not cfg.night_dim.enabled else year_of_minutes(C, old, args.days)
    layers = stress_layers(cfg, A, hours, args.n)
    drifts = {"digits": A.drift_digits, "temp": A.drift_temp,
              "corner": A.drift_corner, "weather": A.drift_weather}
    runs = {
        "old panel (±6 px jumps, its settings)":
            worn(stress_layers(old, A_old, hours_old, args.n), dict.fromkeys(ELEMENTS, jump(OLD_JUMP_PX))),
        "this config with no drift": worn(layers, dict.fromkeys(ELEMENTS, still())),
        "this config": worn(layers, {e: path(drifts[e], args.days) for e in ELEMENTS}),
    }
    seen = {name: {m: as_seen(damage(S, args.beta), m) for m in DISTANCES}
            for name, S in runs.items()}
    ref = seen["old panel (±6 px jumps, its settings)"]

    print(f"\ntime to reach the old panel's ghost, as a multiple of the time it took "
          f"(n={args.n:g}, beta={args.beta:g})")
    print(f"  {'':40s}" + "".join(f"{f'from {m:g} m':>12s}" for m in DISTANCES))
    for name, by_m in seen.items():
        cells = "".join(f"{(ref[m].max() / by_m[m].max()) ** (1 / args.beta):11.1f}x"
                        for m in DISTANCES)
        print(f"  {name:40s}{cells}")
    S = runs["this config"]
    print(f"\n  worst subpixel, hours of full drive per day (R/G/B): "
          + " / ".join(f"{v:.2f}" for v in S.reshape(-1, 3).max(0)))

    if args.per_element:
        mine, theirs = seen["this config"][1.0], ref[1.0].max()
        print("\n  how visible each element's ghost is from 1 m, vs the old panel's worst (=1.00):")
        for name, (x0, x1, y0, y1) in REGIONS.items():
            print(f"    {name:8s} {mine[y0:y1, x0:x1].max() / theirs:5.2f}")

    if args.heatmap:
        from PIL import Image
        img = seen["this config"][1.0]
        Image.fromarray((img / img.max() * 255).astype(np.uint8)).save(args.heatmap)
        print(f"\nwrote {args.heatmap}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

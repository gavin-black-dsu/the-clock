#!/usr/bin/env python3
"""
Themed fullscreen image clock.

  • Theme chosen in config.json → images/<theme>/…
  • Daytime corner icon:    sun/<weekday>.png
  • Night-time corner icon: moon/<phase>.png   (phase from astral.moon.phase)
  • HH:MM am/pm            (colon + am/pm = half-width)
  • Per-theme brightness, separate sun/moon/weather brightness
  • Temperature readout centred at the top
  • A row of small alert icons above the temperature, each shown only when
    its endpoint says something needs attention
  • Continuous sub-pixel anti-burn-in drift that is too slow to see

Run `clock.py --help` for the development switches (windowed mode,
single-frame screenshots, endpoint self-test).
"""

from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import math
import os
import pathlib
import random
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Sequence

import pygame
from astral import LocationInfo, moon
from astral.sun import sun

try:                                    # astral 3 ships zoneinfo support; pytz is optional
    from zoneinfo import ZoneInfo as _TZ
except ImportError:                     # pragma: no cover - Python < 3.9
    from pytz import timezone as _TZ    # type: ignore[assignment]

BASE_DIR = pathlib.Path(__file__).resolve().parent
IMAGES_ROOT = BASE_DIR / "images"
CONFIG_PATH = BASE_DIR / "config.json"
LOG_PATH = BASE_DIR / "error.log"

log = logging.getLogger("clock")


# ═══════════════════════════════════════════════════════════════════════════
#  CONFIG
# ═══════════════════════════════════════════════════════════════════════════

DEFAULT_STATUS_MAP = {
    "okay": "okay", "ok": "okay", "good": "okay", "green": "okay",
    "warn": "warn", "warning": "warn", "due": "warn", "amber": "warn",
    "yellow": "warn", "orange": "warn",
    "alarm": "alarm", "alert": "alarm", "critical": "alarm", "error": "alarm",
    "overdue": "alarm", "red": "alarm", "fail": "alarm", "failed": "alarm",
}
SEVERITY_RANK = {"okay": 0, "warn": 1, "alarm": 2, "never": 99}

DEFAULT_ALERT_ITEMS = [
    {"id": "weight", "icon": "scale",
     "endpoint": "http://snek:8000/scale/api/schedule_status/mitts",
     "period": 300, "show_at": "alarm"},
    {"id": "plants", "icon": "plant", "endpoint": "", "period": 600, "show_at": "warn"},
    {"id": "logs", "icon": "logs", "endpoint": "", "period": 300, "show_at": "warn"},
    {"id": "camera", "icon": "camera", "endpoint": "", "period": 300, "show_at": "warn"},
]

_LEGACY_LINE_KEYS = ("line_okay", "line_warn", "line_alarm", "line_status_endpoint",
                     "line_network_period", "line_height", "line_margin")


class ConfigError(Exception):
    """Raised with every problem found in config.json, not just the first."""


@dataclass(frozen=True)
class AlertSpec:
    """One alert icon and the endpoint that decides whether it is shown."""
    id: str
    icon: str
    endpoint: str
    period: float = 300.0
    timeout: float = 4.0
    path: str = "status"            # dotted path into the JSON body
    values: dict = field(default_factory=lambda: dict(DEFAULT_STATUS_MAP))
    show_at: str = "warn"           # minimum severity at which the icon appears
    stale_after: float = 1800.0     # hide the icon if the reading is older than this

    @property
    def enabled(self) -> bool:
        return bool(self.endpoint)


@dataclass(frozen=True)
class AlertsConfig:
    icon_size: int = 88
    icon_gap: int = 40
    gap_below: int = 28
    brightness_day: float = 1.0
    brightness_night: float = 0.75
    tint: dict = field(default_factory=dict)   # {"warn": [r,g,b], "alarm": [r,g,b]}
    items: tuple[AlertSpec, ...] = ()


@dataclass(frozen=True)
class DriftConfig:
    """Anti burn-in drift.

    ``min_x`` and ``max_y`` are the half-amplitudes the digit strip is
    *guaranteed*: the layout reserves them and shrinks the digits if that is
    what it takes.  ``max_x`` is the cap on how much further it drifts if the
    layout happens to leave more room.  The floor on x matters — without it a
    strip that grows to fill the screen width leaves no horizontal slack at
    all, and horizontal drift, which does most of the work, quietly vanishes.
    The
    periods are deliberately co-prime-ish so the path never repeats exactly,
    and long enough that the peak speed stays far below the threshold at
    which a human can see a high-contrast edge move (see README).
    """
    max_x: float = 160.0
    min_x: float = 48.0
    max_y: float = 40.0
    icon_max: float = 40.0
    top_y: float = 16.0
    periods_min: tuple[float, ...] = (71.0, 113.0, 179.0)
    quantum: float = 0.0625         # redraw once the position moves this far (px)
    enabled: bool = True


@dataclass(frozen=True)
class NightDim:
    """Optional extra dimming during hours nobody is looking at the panel."""
    start: int = 0                  # minutes past midnight
    end: int = 0
    brightness: float = 1.0
    enabled: bool = False


@dataclass(frozen=True)
class Config:
    theme: str = "default"
    brightness_day: float = 1.0
    brightness_night: float = 1.0
    brightness_sun: float = 1.0
    brightness_moon: float = 1.0
    brightness_weather_day: float = 1.0
    brightness_weather_night: float = 1.0
    temp_brightness_day: float = 1.0
    temp_brightness_night: float = 1.0
    temp_color_day: tuple = (255, 255, 255)
    temp_color_night: tuple = (255, 255, 255)
    temp_font: str = "comicsansms"
    temp_font_size: int = 64
    temp_padding_top: int = 40
    temp_endpoint: str = ""
    temp_field: str = "Temperature"
    temp_unit: str = "F"
    temp_period: float = 120.0
    temp_stale_after: float = 7200.0
    weather_endpoint: str = "http://snek:8000/weather/api/condition"
    weather_field: str = "value"
    weather_period: float = 900.0
    weather_stale_after: float = 21600.0
    time_adjust: int = 0
    timezone: str = "America/New_York"
    latitude: float = 42.95
    longitude: float = -72.04
    screen_w: int = 0               # 0 → use the panel's native resolution
    screen_h: int = 0
    padding: int = 40
    digit_scale: float = 1.0
    fps: int = 30
    net_timeout: float = 4.0
    alerts: AlertsConfig = field(default_factory=AlertsConfig)
    drift: DriftConfig = field(default_factory=DriftConfig)
    night_dim: NightDim = field(default_factory=NightDim)


def _clamp01(v: Any, name: str, problems: list) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        problems.append(f"{name}: expected a number, got {v!r}")
        return 1.0
    if not 0.0 <= f <= 1.0:
        problems.append(f"{name}: {f} is outside 0.0-1.0, clamped")
    return max(0.0, min(1.0, f))


def _rgb(v: Any, name: str, problems: list, default=(255, 255, 255)) -> tuple:
    if v is None:
        return default
    try:
        r, g, b = (max(0, min(255, int(c))) for c in v)
        return (r, g, b)
    except (TypeError, ValueError):
        problems.append(f"{name}: expected [r, g, b], got {v!r}")
        return default


def _num(v: Any, default: float, name: str, problems: list,
         lo: float | None = None, hi: float | None = None) -> float:
    """A number that reports a problem instead of raising.  A typo anywhere in
    config.json used to escape load_config() as a bare ValueError and take the
    clock down with a traceback."""
    if v is None:
        return default
    try:
        f = float(v)
    except (TypeError, ValueError):
        problems.append(f"{name}: expected a number, got {v!r}")
        return default
    if f != f or f in (float("inf"), float("-inf")):
        problems.append(f"{name}: {v!r} is not a finite number")
        return default
    if lo is not None and f < lo:
        problems.append(f"{name}: {f:g} is below {lo:g}, clamped")
        return lo
    if hi is not None and f > hi:
        problems.append(f"{name}: {f:g} is above {hi:g}, clamped")
        return hi
    return f


def _positive(v: Any, default: float, name: str, problems: list) -> float:
    """A duration that must be > 0.  Zero would mean "never goes stale", which
    is the opposite of what anyone typing a zero intends."""
    if v is None:
        return default
    try:
        f = float(v)
    except (TypeError, ValueError):
        problems.append(f"{name}: expected a number, got {v!r}")
        return default
    if f <= 0:
        problems.append(f"{name}: must be greater than 0, using {default:g}")
        return default
    return f


def _hhmm(v: Any, name: str, problems: list) -> int:
    try:
        h, m = str(v).split(":")
        return int(h) % 24 * 60 + int(m) % 60
    except (ValueError, AttributeError):
        problems.append(f"{name}: expected \"HH:MM\", got {v!r}")
        return 0


def resolve_tz(name: str):
    """A tzinfo for `name`, degrading all the way to a database-free UTC.

    Falling back to the *string* "UTC" is not enough: on an image with no
    /usr/share/zoneinfo and no tzdata package, zoneinfo cannot resolve "UTC"
    either, so the fallback raises exactly like the original name did.
    `datetime.timezone.utc` needs no database at all.
    """
    for candidate in (name, "UTC"):
        try:
            return _TZ(candidate)
        except Exception:                                  # noqa: BLE001 - backend varies
            continue
    return timezone.utc


def _timezone(name: Any, problems: list) -> str:
    """Check the zone resolves now, so a typo or a missing tzdata package is a
    config problem at startup rather than a traceback out of Clock.__init__."""
    text = str(name)
    try:
        _TZ(text)
        return text
    except Exception as exc:                               # noqa: BLE001 - backend varies
        problems.append(f"timezone: {text!r} is unusable ({exc}); falling back to UTC. "
                        f"On a minimal image this usually means the tzdata package "
                        f"is missing.")
        return "UTC"


def _parse_alerts(data: dict, problems: list) -> AlertsConfig:
    blk = data.get("alerts")
    legacy_present = [k for k in _LEGACY_LINE_KEYS if k in data]

    if blk is None:
        # No "alerts" block: carry the old status-line endpoint over as the
        # weight alert so an un-migrated config keeps working.
        raw_items = [dict(it) for it in DEFAULT_ALERT_ITEMS]
        legacy_ep = str(data.get("line_status_endpoint") or "").strip()
        if legacy_ep:
            raw_items[0]["endpoint"] = legacy_ep
            raw_items[0]["period"] = _num(data.get("line_network_period"), 300.0,
                                          "line_network_period", problems, lo=5.0)
            log.info("config: no \"alerts\" block; carried line_status_endpoint over as "
                     "the 'weight' alert. See README for the new \"alerts\" block.")
        blk = {}
    else:
        if isinstance(blk, list):
            blk = {"items": blk}
        if not isinstance(blk, dict):
            problems.append(f"alerts: expected an object or list, got {type(blk).__name__}")
            blk = {}
        raw_items = blk.get("items")
        if raw_items is None:
            log.info("config: \"alerts\" has no \"items\"; no alert icons will be shown")
            raw_items = []
        if legacy_present:
            log.info("config: ignoring obsolete key(s) %s (replaced by \"alerts\")",
                     ", ".join(legacy_present))

    specs: list[AlertSpec] = []
    seen: set[str] = set()
    if not isinstance(raw_items, list):
        problems.append("alerts.items: expected a list")
        raw_items = []
    for i, raw in enumerate(raw_items):
        if not isinstance(raw, dict):
            problems.append(f"alerts.items[{i}]: expected an object")
            continue
        aid = str(raw.get("id") or f"alert{i}")
        if aid in seen:
            problems.append(f"alerts.items[{i}]: duplicate id {aid!r}")
            continue
        seen.add(aid)
        show_at = str(raw.get("show_at", "warn")).lower()
        if show_at not in SEVERITY_RANK:
            problems.append(f"alerts.items[{i}].show_at: {show_at!r} is not one of "
                            f"okay/warn/alarm/never")
            show_at = "warn"
        values = dict(DEFAULT_STATUS_MAP)
        extra = raw.get("values") or {}
        if isinstance(extra, dict):
            for k, v in extra.items():
                target = str(v).strip().lower()
                if target not in ("okay", "warn", "alarm"):
                    # "red"/"critical"/"due" are input synonyms, not severities.
                    # Writing one as the target used to produce a severity no
                    # rank knew about, which silently disabled the alert.
                    coerced = DEFAULT_STATUS_MAP.get(target)
                    problems.append(
                        f"alerts.items[{i}].values[{k!r}]: {v!r} is not okay/warn/alarm"
                        + (f", reading it as {coerced!r}" if coerced else ", ignored"))
                    if coerced is None:
                        continue
                    target = coerced
                values[str(k).strip().lower()] = target
        else:
            problems.append(f"alerts.items[{i}].values: expected an object")
        item_period = _num(raw.get("period"), 300.0, f"alerts.items[{i}].period",
                           problems, lo=5.0)
        item_stale = _positive(raw.get("stale_after"), 1800.0,
                               f"alerts.items[{i}].stale_after", problems)
        if raw.get("endpoint") and item_stale < 2 * item_period:
            problems.append(
                f"alerts.items[{i}].stale_after ({item_stale:g}s) is less than twice "
                f"period ({item_period:g}s); the icon will flicker between polls")
        specs.append(AlertSpec(
            id=aid,
            icon=str(raw.get("icon") or aid),
            endpoint=str(raw.get("endpoint") or "").strip(),
            period=item_period,
            timeout=_num(raw.get("timeout"), 4.0, f"alerts.items[{i}].timeout", problems, lo=0.5),
            path=str(raw.get("field", "status")),
            values=values,
            show_at=show_at,
            stale_after=item_stale,
        ))

    tint_raw = blk.get("tint") or {}
    tint = {}
    if isinstance(tint_raw, dict):
        for sev in ("warn", "alarm"):
            if sev in tint_raw:
                tint[sev] = _rgb(tint_raw[sev], f"alerts.tint.{sev}", problems)
    else:
        problems.append("alerts.tint: expected an object")

    return AlertsConfig(
        icon_size=int(_num(blk.get("icon_size"), 88, "alerts.icon_size", problems, lo=8)),
        icon_gap=int(_num(blk.get("icon_gap"), 40, "alerts.icon_gap", problems, lo=0)),
        gap_below=int(_num(blk.get("gap_below"), 28, "alerts.gap_below", problems, lo=0)),
        brightness_day=_clamp01(blk.get("brightness_day", 1.0), "alerts.brightness_day", problems),
        brightness_night=_clamp01(blk.get("brightness_night", 0.75), "alerts.brightness_night", problems),
        tint=tint,
        items=tuple(specs),
    )


def _parse_drift(data: dict, problems: list) -> DriftConfig:
    blk = data.get("drift") or {}
    if not isinstance(blk, dict):
        problems.append("drift: expected an object")
        blk = {}
    periods = blk.get("periods_minutes", (71.0, 113.0, 179.0))
    try:
        periods = tuple(sorted(float(p) for p in periods))
        if not periods or min(periods) <= 0:
            raise ValueError
    except (TypeError, ValueError):
        problems.append(f"drift.periods_minutes: expected positive numbers, got {periods!r}")
        periods = (71.0, 113.0, 179.0)
    return DriftConfig(
        max_x=_num(blk.get("max_x"), 160.0, "drift.max_x", problems, lo=0.0),
        min_x=_num(blk.get("min_x"), 48.0, "drift.min_x", problems, lo=0.0),
        max_y=_num(blk.get("max_y"), 40.0, "drift.max_y", problems, lo=0.0),
        icon_max=_num(blk.get("icon_max"), 40.0, "drift.icon_max", problems, lo=0.0),
        top_y=_num(blk.get("top_y"), 16.0, "drift.top_y", problems, lo=0.0),
        periods_min=periods,
        quantum=_num(blk.get("quantum"), 0.0625, "drift.quantum", problems, lo=1 / 64, hi=1.0),
        enabled=bool(blk.get("enabled", True)),
    )


def _parse_night_dim(data: dict, problems: list) -> NightDim:
    blk = data.get("night_dim")
    if not blk:
        return NightDim()
    if not isinstance(blk, dict):
        problems.append("night_dim: expected an object")
        return NightDim()
    return NightDim(
        start=_hhmm(blk.get("start", "01:00"), "night_dim.start", problems),
        end=_hhmm(blk.get("end", "06:00"), "night_dim.end", problems),
        brightness=_clamp01(blk.get("brightness", 0.35), "night_dim.brightness", problems),
        enabled=bool(blk.get("enabled", True)),
    )


def load_config(path: pathlib.Path) -> Config:
    """Parse config.json, reporting every problem at once rather than dying on the first."""
    problems: list[str] = []
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        log.warning("config: %s not found, using defaults", path)
        data = {}
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: top level must be an object")

    def num(key, default, cast=float):
        if key not in data:
            return default
        try:
            return cast(data[key])
        except (TypeError, ValueError):
            problems.append(f"{key}: expected a number, got {data[key]!r}")
            return default

    weather_b = data.get("brightness_weather", 1.0)
    cfg = Config(
        theme=str(data.get("theme", "default")),
        brightness_day=_clamp01(data.get("brightness_day", 1.0), "brightness_day", problems),
        brightness_night=_clamp01(data.get("brightness_night", 1.0), "brightness_night", problems),
        brightness_sun=_clamp01(data.get("brightness_sun", 1.0), "brightness_sun", problems),
        brightness_moon=_clamp01(data.get("brightness_moon", 1.0), "brightness_moon", problems),
        brightness_weather_day=_clamp01(data.get("brightness_weather_day", weather_b),
                                        "brightness_weather_day", problems),
        brightness_weather_night=_clamp01(data.get("brightness_weather_night", weather_b),
                                          "brightness_weather_night", problems),
        temp_brightness_day=_clamp01(data.get("temp_brightness_day", 1.0),
                                     "temp_brightness_day", problems),
        temp_brightness_night=_clamp01(data.get("temp_brightness_night", 1.0),
                                       "temp_brightness_night", problems),
        temp_color_day=_rgb(data.get("temp_color_day"), "temp_color_day", problems),
        temp_color_night=_rgb(data.get("temp_color_night"), "temp_color_night", problems),
        temp_font=str(data.get("temp_font", "comicsansms")),
        temp_font_size=max(8, int(num("temp_font_size", 64, int))),
        temp_padding_top=int(num("temp_padding_top", 40, int)),
        temp_endpoint=str(data.get("temp_endpoint") or "").strip(),
        temp_field=str(data.get("temp_field", "Temperature")),
        temp_unit=str(data.get("temp_unit", "F")),
        temp_period=max(5.0, num("temp_network_period", 120.0)),
        temp_stale_after=_positive(data.get("temp_stale_after"), 7200.0,
                                   "temp_stale_after", problems),
        weather_endpoint=str(data.get("weather_endpoint",
                                      "http://snek:8000/weather/api/condition") or "").strip(),
        weather_field=str(data.get("weather_field", "value")),
        weather_period=max(5.0, num("weather_network_period", 900.0)),
        weather_stale_after=_positive(data.get("weather_stale_after"), 21600.0,
                                      "weather_stale_after", problems),
        time_adjust=int(num("time_adjust", 0, int)),
        timezone=_timezone(data.get("timezone", "America/New_York"), problems),
        latitude=num("latitude", 42.95),
        longitude=num("longitude", -72.04),
        screen_w=int(num("screen_width", 0, int)),
        screen_h=int(num("screen_height", 0, int)),
        padding=max(0, int(num("padding", 40, int))),
        digit_scale=max(0.25, min(1.0, num("digit_scale", 1.0))),
        fps=max(1, int(num("fps", 30, int))),
        net_timeout=max(0.5, num("net_timeout", 4.0)),
        alerts=_parse_alerts(data, problems),
        drift=_parse_drift(data, problems),
        night_dim=_parse_night_dim(data, problems),
    )
    for p in problems:
        log.warning("config: %s", p)
    return cfg


# ═══════════════════════════════════════════════════════════════════════════
#  LOGGING
# ═══════════════════════════════════════════════════════════════════════════

class _Collapse(logging.Filter):
    """Drop a warning that repeats within `window` seconds, then say how many were dropped.

    Attached to the `clock` logger rather than to the handlers, deliberately.
    A filter is stateful, so one instance shared by two handlers would count
    the first handler's pass as "already seen" and starve the second — and an
    instance per handler would annotate the same record twice.  A logger-level
    filter runs exactly once per record, before it reaches any handler.
    """

    def __init__(self, window: float = 600.0):
        super().__init__()
        self.window = window
        self._seen: dict[tuple, tuple[float, int]] = {}

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno < logging.WARNING:
            return True
        # The *formatted* text, not the format string: every poller failure
        # shares the literal "%s: %s (%s)", so keying on record.msg would
        # collapse a dead temperature endpoint and a dead weather endpoint
        # into one line and report only whichever failed first.
        key = (record.name, record.levelno, record.getMessage())
        now = time.monotonic()
        first, count = self._seen.get(key, (0.0, 0))
        if first and now - first < self.window:
            self._seen[key] = (first, count + 1)
            return False
        if count:
            record.msg = f"{record.msg}  [+{count} identical suppressed]"
        if len(self._seen) > 256:                      # bounded for a months-long run
            cutoff = now - self.window
            self._seen = {k: v for k, v in self._seen.items() if v[0] > cutoff}
        self._seen[key] = (now, 0)
        return True


def setup_logging(verbose: bool = False) -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if verbose:                       # our own logger only; third-party DEBUG is noise
        log.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                            "%Y-%m-%d %H:%M:%S")
    stream = logging.StreamHandler()
    stream.setFormatter(fmt)
    stream.setLevel(logging.DEBUG if verbose else logging.INFO)
    root.addHandler(stream)
    try:
        with open(LOG_PATH, "a", encoding="utf-8"):        # probe now, not on the
            pass                                           # first warning at 3am
        rot = logging.handlers.RotatingFileHandler(
            LOG_PATH, maxBytes=512_000, backupCount=2, encoding="utf-8", delay=True)
        rot.setFormatter(fmt)
        rot.setLevel(logging.WARNING)
        root.addHandler(rot)
    except OSError as exc:                             # read-only filesystem, full disk
        print(f"cannot write {LOG_PATH}: {exc} — logging to the console only",
              file=sys.stderr)
    log.addFilter(_Collapse())


# ═══════════════════════════════════════════════════════════════════════════
#  NETWORK  —  every fetch happens on a daemon thread, never on the render loop
# ═══════════════════════════════════════════════════════════════════════════

USER_AGENT = "the-clock/2 (+https://github.com/gavin-black-dsu/the-clock)"
MAX_BODY = 512 * 1024


@dataclass(frozen=True, slots=True)
class Reading:
    """One published value.  Frozen, so the render thread can never see a torn read."""
    value: Any = None
    fetched_at: float = float("-inf")   # time.monotonic() of the last SUCCESS
    ok: bool = False
    disabled: bool = False
    error: str = ""

    def fresh(self, stale_after: float, now: float | None = None) -> bool:
        """Is this value recent enough to show?  `stale_after` is always > 0;
        load_config refuses a zero, which would pin a dead endpoint's last
        reading on screen for the life of the process."""
        if self.value is None:
            return False
        return (time.monotonic() if now is None else now) - self.fetched_at <= stale_after


class Store:
    """Copy-on-write dict of Readings.  Writers hold a lock; readers never do."""

    __slots__ = ("_lock", "_data")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: dict[str, Reading] = {}

    def put(self, key: str, reading: Reading) -> None:
        with self._lock:
            d = dict(self._data)
            d[key] = reading
            self._data = d

    def snapshot(self) -> dict[str, Reading]:
        return self._data          # rebound atomically by put(); never mutated in place

    def get(self, key: str) -> Reading:
        return self._data.get(key, Reading())


_OPENER = urllib.request.build_opener()
_OPENER.addheaders = [("User-Agent", USER_AGENT), ("Accept", "application/json, text/plain")]

PERMANENT_CODES = frozenset({400, 401, 403, 404, 405, 410, 414, 451, 501})


def http_get_text(url: str, timeout: float) -> str:
    """GET a small body as text.  Raises on anything that is not a 2xx."""
    with _OPENER.open(url, timeout=timeout) as resp:
        body = resp.read(MAX_BODY + 1)
    if len(body) > MAX_BODY:
        raise ValueError(f"body larger than {MAX_BODY} bytes")
    return body.decode("utf-8", errors="replace").lstrip("﻿").strip()


def json_path(obj: Any, path: str) -> Any:
    """Walk a dotted path; a numeric segment indexes a list."""
    cur = obj
    for seg in str(path).split("."):
        if isinstance(cur, list):
            try:
                cur = cur[int(seg)]
            except (ValueError, IndexError):
                return None
        elif isinstance(cur, dict):
            if seg not in cur:
                return None
            cur = cur[seg]
        else:
            return None
    return cur


class Poller(threading.Thread):
    """Fetch one endpoint forever, on its own daemon thread, with backoff."""

    def __init__(self, key: str, url: str, period: float, timeout: float,
                 parse: Callable[[str], Any], store: Store, stop: threading.Event,
                 start_delay: float = 0.0):
        super().__init__(name=f"poll-{key}", daemon=True)
        self.key, self.url, self.period, self.timeout = key, url, period, timeout
        self.parse, self.store, self.stop = parse, store, stop
        self.start_delay = start_delay
        self._fails = 0
        self._down_since = 0.0

    # -- one attempt; returns the delay until the next one -------------------
    def _cycle(self) -> float:
        try:
            value = self.parse(http_get_text(self.url, self.timeout))
            if value is None:
                raise ValueError("endpoint returned nothing usable")
        except Exception as exc:                    # noqa: BLE001 - a poller must never die
            permanent = (isinstance(exc, urllib.error.HTTPError)
                         and exc.code in PERMANENT_CODES)
            self._fails += 1
            if self._fails == 1:
                self._down_since = time.monotonic()
            prev = self.store.get(self.key)
            self.store.put(self.key, Reading(value=prev.value, fetched_at=prev.fetched_at,
                                             ok=False, error=f"{type(exc).__name__}: {exc}"))
            log.warning("%s: %s (%s)", self.key, exc, self.url)
            if permanent:
                return 3600.0                       # a 404 will not fix itself in 30 s
            return max(self.period, min(900.0, 5.0 * 2 ** min(self._fails, 8)))

        if self._fails:
            log.info("%s: recovered after %d failure(s), %.0fs down",
                     self.key, self._fails, time.monotonic() - self._down_since)
            self._fails = 0
        self.store.put(self.key, Reading(value=value, fetched_at=time.monotonic(), ok=True))
        return self.period

    def run(self) -> None:
        if not self.url:                       # publish before the stagger wait, so a
            self.store.put(self.key, Reading(disabled=True))   # blank endpoint is known at once
            return
        if self.stop.wait(self.start_delay):
            return
        while not self.stop.is_set():
            delay = self._cycle()
            # ±10% jitter so two pollers that happen to align drift apart again
            self.stop.wait(delay * random.uniform(0.9, 1.1))


def build_pollers(cfg: Config, store: Store, stop: threading.Event) -> list[Poller]:
    """One poller per configured endpoint, started staggered so snek never sees a burst."""
    specs: list[tuple[str, str, float, float, Callable[[str], Any]]] = []

    def extract(text: str, field: str) -> Any:
        """Pull a value out of whatever shape the endpoint answers with: a JSON
        object, a bare JSON scalar, or plain text."""
        try:
            doc = json.loads(text)
        except ValueError:
            return text
        if isinstance(doc, (dict, list)):
            return json_path(doc, field)
        return text if doc is None else doc

    def parse_temp(text: str) -> float | None:
        value = extract(text, cfg.temp_field)
        return None if value is None else float(value)

    def parse_weather(text: str) -> str | None:
        value = extract(text, cfg.weather_field)
        return None if value is None else str(value)

    specs.append(("temp", cfg.temp_endpoint, cfg.temp_period, cfg.net_timeout, parse_temp))
    specs.append(("weather", cfg.weather_endpoint, cfg.weather_period, cfg.net_timeout, parse_weather))

    for spec in cfg.alerts.items:
        def parse_alert(text: str, spec: AlertSpec = spec) -> str:
            # Accept the shapes an endpoint might plausibly answer with:
            # {"status": "alarm"}, a bare JSON string "alarm", or plain alarm.
            raw = extract(text, spec.path)
            if raw is None:
                raise ValueError(f"no {spec.path!r} in the response")
            token = str(raw).strip().lower()
            severity = spec.values.get(token)
            if severity is None:
                # Never fall back to "okay".  An unrecognised status means the
                # monitoring is broken, and an alert that silently reports
                # health it cannot actually see is worse than no alert: this
                # goes down the failure path, gets logged with the offending
                # value, and the icon ages out instead.
                raise ValueError(f"unrecognised status {token!r} — add it to "
                                 f"alerts.items[{spec.id}].values")
            return severity
        specs.append((f"alert:{spec.id}", spec.endpoint, spec.period, spec.timeout, parse_alert))

    pollers = []
    for i, (key, url, period, timeout, parse) in enumerate(specs):
        pollers.append(Poller(key, url, period, timeout, parse, store, stop, start_delay=1.5 * i))
    return pollers


# ═══════════════════════════════════════════════════════════════════════════
#  SPRITES  —  everything is pre-flattened onto black so it can be sub-pixel blitted
# ═══════════════════════════════════════════════════════════════════════════

def opaque_surface(size: tuple[int, int]) -> pygame.Surface:
    """A black surface that is opaque *and* carries an alpha channel.

    Both halves matter.  The alpha channel picks SDL's correct blitter (see
    `Sprite`), and the alpha has to be explicitly 255 because a plain
    `pygame.Surface(size)` inherits the display's alpha mask on some drivers
    (wayland, notably) while leaving those bits at zero — anything blitted
    into it then comes out invisible.
    """
    surf = pygame.Surface(size, pygame.SRCALPHA)
    surf.fill((0, 0, 0, 255))
    return surf


def make_image(src: pygame.Surface, size: tuple[int, int] | None = None,
               brightness: float = 1.0, tint: Sequence[int] | None = None) -> pygame.Surface:
    """Flatten `src` onto black, scale it, then dim or tint it.

    Flattening *before* scaling is deliberate: `smoothscale` does not
    premultiply alpha, so scaling straight-alpha artwork pulls the (black,
    fully transparent) pixels from outside the drawing into every edge and
    darkens it.  Compositing onto the black background first makes the
    resampling correct — and black is what is actually behind it on screen.
    """
    flat = opaque_surface(src.get_size())
    flat.blit(src, (0, 0))
    if size and size != flat.get_size():
        flat = pygame.transform.smoothscale(flat, size)
    if tint is not None:
        flat.fill((*tint, 255), None, pygame.BLEND_RGB_MULT)
    if brightness < 0.999:
        m = max(0, min(255, int(round(brightness * 255))))
        flat.fill((m, m, m, 255), None, pygame.BLEND_RGB_MULT)
    return flat.convert_alpha()


class Sprite:
    """An image wrapped in a 1px black border, ready for `blit_subpixel`.

    Two details are load-bearing:

    * The border means all four bilinear taps fully cover the destination
      rectangle, so the weighted chain in `blit_subpixel` is exact at the
      edges instead of blending against whatever was underneath.
    * The surface carries an alpha channel even though every pixel is
      opaque.  SDL has a separate, SIMD-optimised blitter for per-surface
      alpha on sources *without* an alpha channel, and at least one shipped
      build (SDL 2.32.70) leaves the last `width % 4` columns of every row
      unblended.  Keeping the alpha channel routes the blit through the
      path that is correct at every width, for about 0.4 us more per blit.
    """

    __slots__ = ("img", "w", "h")

    def __init__(self, image: pygame.Surface):
        self.w, self.h = image.get_size()
        bordered = opaque_surface((self.w + 2, self.h + 2))
        bordered.blit(image, (1, 1))
        self.img = bordered.convert_alpha()

    @property
    def size(self) -> tuple[int, int]:
        return self.w, self.h


_TAPS = ((0, 0), (1, 0), (0, 1), (1, 1))


def blit_subpixel(dst: pygame.Surface, spr: Sprite, x: float, y: float) -> pygame.Rect:
    """Draw `spr` with its top-left corner at the *fractional* position (x, y).

    pygame blits on integer coordinates only, so a drifting image would have
    to jump a whole pixel at a time — and a one-pixel jump of a high-contrast
    edge is visible however rarely it happens.  This is a true bilinear
    sub-pixel shift built from four ordinary blits and no temporary surfaces:
    blit the first tap opaque, then each later tap with per-surface alpha

        w_k / (w_0 + … + w_k)

    which telescopes to exactly Σ w_i·I_i because the weights sum to 1.
    Measured against a float reference the worst error is 3/255, and the
    whole thing costs about as much as one and a half plain blits.

    `dst` must be an SRCALPHA surface — see `Sprite` for why the blitter
    choice matters.  `check_subpixel()` verifies both ends of that at boot.
    """
    ix, iy = math.floor(x), math.floor(y)
    fx, fy = x - ix, y - iy
    weights = ((1 - fx) * (1 - fy), fx * (1 - fy), (1 - fx) * fy, fx * fy)
    img = spr.img
    acc = 0.0
    for (dx, dy), w in zip(_TAPS, weights):
        if w <= 0.0:
            continue
        acc += w
        img.set_alpha(255 if acc <= w else max(1, min(255, int(round(255.0 * w / acc)))))
        dst.blit(img, (ix + dx - 1, iy + dy - 1))
    img.set_alpha(255)
    return pygame.Rect(ix - 1, iy - 1, spr.w + 2, spr.h + 2)


def check_subpixel() -> float:
    """Blit a gradient at known offsets and compare against exact arithmetic.

    Sub-pixel drift is the whole anti burn-in story, and it rests on the
    platform's alpha blitter being right.  One build was not (see `Sprite`),
    so verify it at boot rather than shipping a silent artefact.  Returns the
    worst error in 0-255 units; anything above ~4 means something is wrong.
    """
    w, h = 23, 5                                   # deliberately not a multiple of 4
    art = pygame.Surface((w, h), pygame.SRCALPHA)
    for x in range(w):
        art.fill((x * 11 % 256, 255 - x * 7 % 256, x * 3 % 256, 255), pygame.Rect(x, 0, 1, h))
    spr = Sprite(make_image(art))          # exactly how real artwork is prepared
    if spr.img.get_at((1, 1))[3] != 255:
        return 255.0                       # the image came out transparent
    ref = [[spr.img.get_at((x, y))[:3] for x in range(w + 2)] for y in range(h + 2)]
    worst = 0.0
    canvas = opaque_surface((w + 8, h + 8)).convert_alpha()
    for fx, fy in ((0.5, 0.5), (0.25, 0.75), (1 / 16, 15 / 16)):
        canvas.fill((0, 0, 0, 255))
        blit_subpixel(canvas, spr, 1 + fx, 1 + fy)
        weights = ((1 - fx) * (1 - fy), fx * (1 - fy), (1 - fx) * fy, fx * fy)
        for y in range(h + 2):
            for x in range(w + 2):
                for c in range(3):
                    want = sum(wt * (ref[y - dy][x - dx][c] if y >= dy and x >= dx else 0)
                               for (dx, dy), wt in zip(_TAPS, weights))
                    worst = max(worst, abs(canvas.get_at((x, y))[c] - want))
    return worst


# ═══════════════════════════════════════════════════════════════════════════
#  DRIFT
# ═══════════════════════════════════════════════════════════════════════════

class Drift:
    """A slow, aperiodic, area-filling path for one element.

    Each axis is the mean of N sinusoids whose periods are mutually
    incommensurate, so the path never repeats and fills a rounded region
    rather than tracing a fixed loop.  Peak speed is
    ``amp · (2π/60) · mean(1/period_minutes)``, which for the shipped layout
    works out at 0.057 px/s for the digit strip — one pixel every 17 seconds —
    and 0.16 px/s for an element running at the full 160 px amplitude.  Both
    are well over an order of magnitude below the velocity at which a human
    can see a high-contrast edge move.  `peak_speed()` reports the real figure
    and it is logged at startup, so neither number has to be trusted.
    """

    __slots__ = ("amp_x", "amp_y", "wx", "wy", "px", "py")

    def __init__(self, amp_x: float, amp_y: float, periods_min: Sequence[float], phase: int = 0):
        self.amp_x, self.amp_y = amp_x, amp_y
        n = len(periods_min)
        self.wx = tuple(2 * math.pi / (p * 60.0) for p in periods_min)
        # A different, irrational-ish stretch on the vertical axis keeps x and
        # y from locking into a diagonal line.
        self.wy = tuple(2 * math.pi / (p * 60.0 * 1.6180339887) for p in periods_min)
        self.px = tuple(phase * 1.2345 + i * 2.399963 for i in range(n))
        self.py = tuple(phase * 0.7531 + i * 1.899774 + 0.5 for i in range(n))

    def at(self, t: float) -> tuple[float, float]:
        n = len(self.wx)
        sx = sum(math.sin(w * t + p) for w, p in zip(self.wx, self.px)) / n
        sy = sum(math.sin(w * t + p) for w, p in zip(self.wy, self.py)) / n
        return self.amp_x * sx, self.amp_y * sy

    def peak_speed(self) -> float:
        n = len(self.wx)
        return math.hypot(self.amp_x * sum(self.wx) / n, self.amp_y * sum(self.wy) / n)


class NoDrift(Drift):
    __slots__ = ()

    def __init__(self):
        super().__init__(0.0, 0.0, (1.0,))

    def at(self, t: float) -> tuple[float, float]:
        return 0.0, 0.0


# ═══════════════════════════════════════════════════════════════════════════
#  ASSETS + LAYOUT
# ═══════════════════════════════════════════════════════════════════════════

WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
MOON_NAMES = ("new", "waxing_crescent", "first_quarter", "waxing_gibbous",
              "full", "waning_gibbous", "third_quarter", "waning_crescent")
GLYPHS = ("0", "1", "2", "3", "4", "5", "6", "7", "8", "9", "colon", "am", "pm")
NARROW = frozenset({"colon", "am", "pm"})
DIGIT_UNITS = 5.0                 # 4 full-width glyphs + 2 half-width ones


def moon_phase_name(day: date) -> str:
    """astral.moon.phase returns 0 ≤ p < 28 (not the synodic 29.53), so the
    eight buckets are 3.5 wide and centred by shifting 1.75."""
    p = moon.phase(day)
    return MOON_NAMES[int((p + 1.75) // 3.5) % 8]


def glyph_sequence(dt: datetime) -> list[str]:
    """The six glyph names for a time.  Built from the fields rather than
    strftime so it cannot be broken by a locale where %p is empty."""
    h12 = dt.hour % 12 or 12
    return [f"{h12:02d}"[0], f"{h12:02d}"[1], "colon",
            f"{dt.minute:02d}"[0], f"{dt.minute:02d}"[1],
            "am" if dt.hour < 12 else "pm"]


class Assets:
    """Everything loaded from disk, scaled for the current screen, in both themes."""

    def __init__(self, cfg: Config, screen_w: int, screen_h: int):
        self.cfg = cfg
        self.sw, self.sh = screen_w, screen_h
        self.theme_dir = IMAGES_ROOT / cfg.theme
        if not self.theme_dir.is_dir():
            raise ConfigError(f"theme {cfg.theme!r} not found at {self.theme_dir}")
        pad = cfg.padding

        # ── corner icons ────────────────────────────────────────────────────
        # Everything is loaded, scaled and dropped one file at a time.  The
        # source art is 1024x1536 and 1024x1024 — holding all 53 decoded
        # surfaces at once costs about 270 MB, which is most of a small
        # appliance's RAM for data that is thrown away seconds later.
        icon_box = (screen_w / 4, screen_h / 3)
        self.max_icon_h = self.max_icon_w = 0

        def corner(path: pathlib.Path, brightness: float) -> Sprite | None:
            raw = self._load_optional(path)
            if raw is None:
                return None
            size = _fit(raw.get_size(), icon_box)
            self.max_icon_w = max(self.max_icon_w, size[0])
            self.max_icon_h = max(self.max_icon_h, size[1])
            return Sprite(make_image(raw, size, brightness))

        self.sun = {d: spr for d in WEEKDAYS
                    if (spr := corner(self.theme_dir / "sun" / f"{d}.png",
                                      cfg.brightness_sun)) is not None}
        self.moon = {n: spr for n in MOON_NAMES
                     if (spr := corner(self.theme_dir / "moon" / f"{n}.png",
                                       cfg.brightness_moon)) is not None}
        weather_dir = self.theme_dir / "weather"
        weather_files = sorted(weather_dir.glob("*.png"))
        if not weather_files:
            log.warning("no weather icons in %s; the weather corner will stay empty", weather_dir)
        wb = {"day": cfg.brightness_weather_day, "night": cfg.brightness_weather_night}
        self.weather: dict[str, dict[str, Sprite]] = {"day": {}, "night": {}}
        for wp in weather_files:
            raw = self._load_optional(wp)
            if raw is None:
                continue
            size = _fit(raw.get_size(), icon_box)
            self.max_icon_w = max(self.max_icon_w, size[0])
            self.max_icon_h = max(self.max_icon_h, size[1])
            for key in ("day", "night"):
                self.weather[key][wp.stem] = Sprite(make_image(raw, size, wb[key]))
            del raw

        # ── alert icons ─────────────────────────────────────────────────────
        A = cfg.alerts
        ab = {"day": A.brightness_day, "night": A.brightness_night}
        self.alert: dict[tuple[str, str, str], pygame.Surface] = {}
        for spec in A.items:
            raw = self._load_alert(spec.icon, A.icon_size)
            for key in ("day", "night"):
                for sev in ("okay", "warn", "alarm"):
                    self.alert[(spec.id, sev, key)] = make_image(
                        raw, (A.icon_size, A.icon_size), ab[key], A.tint.get(sev))

        # ── temperature font ────────────────────────────────────────────────
        font_path = pygame.font.match_font(cfg.temp_font) if cfg.temp_font else None
        if cfg.temp_font and not font_path:
            log.info("font %r not installed; using the pygame default. The layout is "
                     "anchored on the rendered ink, so this only changes how the "
                     "temperature looks. Set \"temp_font\": \"\" to silence this.",
                     cfg.temp_font)
        self.font = pygame.font.Font(font_path, cfg.temp_font_size)
        # Reserve for the widest reading, not a guessed one.  "-88" is not it:
        # in a proportional font a hyphen is narrower than a digit, so "888"
        # is wider — and the habitat sensor reaching three digits is ordinary.
        widest = max("-0123456789", key=lambda ch: self.font.size(ch)[0])
        probe = self.font.render(f"{widest * 3}\N{DEGREE SIGN}{cfg.temp_unit}",
                                 True, (255, 255, 255))
        box = probe.get_bounding_rect()
        self.temp_ink_h = box.height
        self.temp_ink_w = box.width

        # ── the vertical stack ──────────────────────────────────────────────
        # drift.top_y is the vertical drift the top block is *guaranteed*, and
        # it is paid for out of the layout.  The corner icons usually push
        # top_reserved further down than the temperature needs, so whatever
        # slack that leaves is handed back as extra drift for free.
        D = cfg.drift
        self.alert_band_h = A.icon_size if A.items else 0
        top_y_min = D.top_y if D.enabled else 0.0
        stack_h = (cfg.temp_padding_top + self.alert_band_h
                   + (A.gap_below if A.items else 0) + self.temp_ink_h + pad)
        self.top_reserved = int(max(pad + self.max_icon_h + pad, stack_h + 2 * top_y_min))
        top_y = (min(D.max_y, (self.top_reserved - stack_h) / 2)
                 if D.enabled and top_y_min > 0 else 0.0)
        self.alert_band_top = cfg.temp_padding_top + top_y
        self.temp_ink_top = (self.alert_band_top + self.alert_band_h
                             + (A.gap_below if A.items else 0))

        # ── digit strip ─────────────────────────────────────────────────────
        # Take the reference aspect from a full-width digit: the narrow
        # glyphs ship on a 500px-wide canvas, so picking whichever file the
        # filesystem happened to list first would squash the whole strip.
        probe_glyph = self._load(self.theme_dir / "day" / "8.png")
        ow, oh = probe_glyph.get_size()
        del probe_glyph
        avail_w = self.sw - 2 * pad - 2 * (D.min_x if D.enabled else 0)
        avail_h = self.sh - self.top_reserved - pad - 2 * (D.max_y if D.enabled else 0)
        if avail_w <= 0 or avail_h <= 0:
            raise ConfigError(
                f"nothing fits in {self.sw}x{self.sh}: reduce drift.max_y, "
                f"temp_font_size ({cfg.temp_font_size}) or padding ({pad})")
        # digit_scale trades clock size for drift room: the digits are
        # height-limited, so shrinking them widens the horizontal envelope.
        scale = min(avail_w / (DIGIT_UNITS * ow), avail_h / oh) * cfg.digit_scale
        self.w_full = max(1, round(ow * scale))
        self.w_half = max(1, round(self.w_full * 0.5))
        self.digit_h = max(1, round(oh * scale))
        self.strip_w = 4 * self.w_full + 2 * self.w_half

        bright = {"day": cfg.brightness_day, "night": cfg.brightness_night}
        # Glyphs are kept as plain images and composited into one strip
        # surface per displayed minute.  Sub-pixel blitting each glyph
        # separately would be both slower and wrong: the 1px borders of
        # adjacent glyphs overlap, so each would clip its neighbour.
        self.digits: dict[str, dict[str, pygame.Surface]] = {"day": {}, "night": {}}
        for key in ("day", "night"):
            for g in GLYPHS:
                raw = self._load(self.theme_dir / key / f"{g}.png")
                self.digits[key][g] = make_image(
                    raw, (self.w_half if g in NARROW else self.w_full, self.digit_h), bright[key])
                del raw

        # ── anchors and per-element drift envelopes ─────────────────────────
        self.strip_x = (self.sw - self.strip_w) / 2.0
        self.strip_y = self.top_reserved + (self.sh - pad - self.top_reserved - self.digit_h) / 2.0
        slack_x = self.strip_x - pad
        slack_y = min(self.strip_y - self.top_reserved,
                      self.sh - pad - (self.strip_y + self.digit_h))
        # The clear span between the two corner icons at *their* maximum
        # drift, not at their resting position — otherwise a wide alert row
        # and an outward-drifting icon can meet in the middle.
        icon_reach = pad + self.max_icon_w + (D.icon_max if D.enabled else 0)
        gutter = self.sw - 2 * icon_reach

        if D.enabled:
            self.drift_digits = Drift(min(D.max_x, max(0.0, slack_x)),
                                      min(D.max_y, max(0.0, slack_y)), D.periods_min, 0)
            # The alert row and the temperature share one drift object, so the
            # gap between them is preserved exactly.  Drifting them
            # independently let a 28px gap close to under 2px within a week.
            # The envelope is sized from the widest thing either can draw, so
            # neither can wander under a corner icon.
            n_alerts = max(1, len(A.items))
            widest_row = n_alerts * A.icon_size + (n_alerts - 1) * A.icon_gap
            top_w = max(self.temp_ink_w, widest_row if A.items else 0)
            self.drift_temp = Drift(min(D.max_x, max(0.0, (gutter - top_w) / 2)),
                                    top_y, D.periods_min, 1)
            self.drift_alerts = self.drift_temp
            # Capped at `padding`: the icons sit that far from the panel edge,
            # so anything larger would clip them against it.
            icon_amp = min(D.icon_max, float(pad))
            if D.icon_max > pad:
                log.info("drift.icon_max %.0f exceeds padding %d; using %d so the corner "
                         "icons stay on screen", D.icon_max, pad, pad)
            self.drift_weather = Drift(icon_amp, icon_amp, D.periods_min, 3)
            self.drift_corner = Drift(icon_amp, icon_amp, D.periods_min, 4)
        else:
            self.drift_digits = self.drift_temp = self.drift_alerts = NoDrift()
            self.drift_weather = self.drift_corner = NoDrift()

        self._temp_cache: dict[tuple, Sprite] = {}
        self._strip_cache: dict[tuple, Sprite] = {}
        self._row_cache: dict[tuple, Sprite] = {}

    # -- helpers ------------------------------------------------------------
    @staticmethod
    def _load(path: pathlib.Path) -> pygame.Surface:
        try:
            return pygame.image.load(str(path)).convert_alpha()
        except (OSError, pygame.error) as exc:
            raise ConfigError(f"cannot load {path}: {exc}") from exc

    @staticmethod
    def _load_optional(path: pathlib.Path) -> pygame.Surface | None:
        """Corner artwork: a truncated file after a power cut should cost one
        icon, not the whole clock.  The digits have no such fallback — there is
        nothing to show without them — so they still use _load()."""
        try:
            return pygame.image.load(str(path)).convert_alpha()
        except (OSError, pygame.error) as exc:
            log.warning("cannot load %s (%s); that icon will not be drawn", path, exc)
            return None

    def _load_alert(self, icon: str, size: int) -> pygame.Surface:
        for candidate in (self.theme_dir / "alerts" / f"{icon}.png",
                          IMAGES_ROOT / "alerts" / f"{icon}.png"):
            if candidate.is_file():
                return self._load(candidate)
        log.info("no artwork for alert icon %r; drawing it instead", icon)
        return draw_alert_glyph(icon, size * 4)

    def temp_sprite(self, text: str, colour: tuple) -> Sprite:
        """The reading, trimmed to its ink so the layout does not depend on
        the font's line box (which varies wildly between fonts)."""
        key = (text, colour)
        cached = self._temp_cache.get(key)
        if cached is None:
            surf = self.font.render(text, True, colour)
            box = surf.get_bounding_rect()
            ink = surf.subsurface(box).copy() if box.width and box.height else surf
            flat = opaque_surface(ink.get_size())
            flat.blit(ink, (0, 0))
            cached = Sprite(flat)
            if len(self._temp_cache) > 32:
                self._temp_cache.clear()
            self._temp_cache[key] = cached
        return cached

    def strip_sprite(self, theme: str, glyphs: Sequence[str]) -> Sprite:
        """The whole HH:MM am/pm strip as one image, rebuilt once a minute."""
        key = (theme, tuple(glyphs))
        cached = self._strip_cache.get(key)
        if cached is None:
            surf = opaque_surface((self.strip_w, self.digit_h))
            images = self.digits[theme]
            x = 0
            for name in glyphs:
                surf.blit(images[name], (x, 0))
                x += self.w_half if name in NARROW else self.w_full
            cached = Sprite(surf)
            if len(self._strip_cache) > 4:
                self._strip_cache.clear()
            self._strip_cache[key] = cached
        return cached

    def alert_row_sprite(self, theme: str,
                         active: Sequence[tuple[str, str]]) -> Sprite | None:
        """The visible alert icons as one centred row, or None if there are none."""
        if not active:
            return None
        key = (theme, tuple(active))
        cached = self._row_cache.get(key)
        if cached is None:
            A = self.cfg.alerts
            n = len(active)
            surf = opaque_surface((n * A.icon_size + (n - 1) * A.icon_gap, A.icon_size))
            x = 0
            for aid, sev in active:
                img = self.alert.get((aid, sev, theme))
                if img is not None:
                    surf.blit(img, (x, 0))
                x += A.icon_size + A.icon_gap
            cached = Sprite(surf)
            if len(self._row_cache) > 16:
                self._row_cache.clear()
            self._row_cache[key] = cached
        return cached


def _fit(size: tuple[int, int], box: tuple[float, float]) -> tuple[int, int]:
    w, h = size
    f = min(box[0] / w, box[1] / h, 1.0)
    return max(1, int(w * f)), max(1, int(h * f))


# ═══════════════════════════════════════════════════════════════════════════
#  FALLBACK ALERT GLYPHS  (used only when the PNG is missing)
# ═══════════════════════════════════════════════════════════════════════════

def draw_alert_glyph(name: str, size: int) -> pygame.Surface:
    """A plain single-colour pictogram, so a missing PNG degrades instead of crashing.

    Coordinates are given on a 100x100 grid and scaled, so the same shapes
    stay legible at any icon size.
    """
    s = pygame.Surface((size, size), pygame.SRCALPHA)
    col = (245, 245, 245, 255)
    u = size / 100.0
    stroke = max(1, round(6 * u))

    def rect(x, y, w, h, r=0, width=0):
        pygame.draw.rect(s, col, pygame.Rect(round(x * u), round(y * u),
                                             round(w * u), round(h * u)),
                         width, round(r * u))

    def line(x1, y1, x2, y2, w=6):
        pygame.draw.line(s, col, (round(x1 * u), round(y1 * u)),
                         (round(x2 * u), round(y2 * u)), max(1, round(w * u)))

    def circle(x, y, r, width=0):
        pygame.draw.circle(s, col, (round(x * u), round(y * u)), round(r * u), round(width * u))

    if name in ("scale", "weight", "scales"):
        line(50, 20, 50, 82, 7)                         # column
        line(14, 28, 86, 28, 7)                         # beam
        rect(32, 82, 36, 9, 4)                          # foot
        circle(50, 17, 6)                               # finial
        for cx in (14, 86):                             # two hanging pans
            line(cx, 28, cx, 46, 4)
            pygame.draw.arc(s, col, pygame.Rect(round((cx - 16) * u), round(34 * u),
                                                round(32 * u), round(28 * u)),
                            math.pi, 2 * math.pi, stroke)
    elif name in ("plant", "plants", "sprout"):
        rect(32, 60, 36, 32, 6)                         # pot
        rect(27, 53, 46, 10, 4)                         # rim
        line(50, 62, 50, 26, 6)                         # stem
        pygame.draw.ellipse(s, col, pygame.Rect(round(20 * u), round(24 * u),
                                                round(30 * u), round(17 * u)))
        pygame.draw.ellipse(s, col, pygame.Rect(round(50 * u), round(28 * u),
                                                round(30 * u), round(17 * u)))
    elif name in ("logs", "log", "computer"):
        rect(12, 16, 76, 52, 6, stroke)                 # monitor bezel
        rect(45, 68, 10, 10)                            # neck
        rect(30, 78, 40, 8, 4)                          # base
        for i, w in enumerate((36, 26, 32)):
            rect(24, 27 + i * 12, w, 6, 3)              # lines of text
    elif name in ("camera", "cam"):
        pygame.draw.arc(s, col, pygame.Rect(round(20 * u), round(22 * u),
                                            round(60 * u), round(60 * u)),
                        0.0, math.pi, stroke)           # dome
        rect(16, 52, 68, 16, 8)                         # base
        circle(50, 46, 13, 6)                           # lens
    else:                                               # generic "!" badge
        circle(50, 50, 34, 7)
        rect(46, 28, 8, 30, 4)
        circle(50, 68, 5)
    return s


# ═══════════════════════════════════════════════════════════════════════════
#  SOLAR / LUNAR CACHE
# ═══════════════════════════════════════════════════════════════════════════

class SkyCache:
    """sun() and moon.phase() are expensive and change once a day."""

    def __init__(self, cfg: Config, tz):
        self.loc = LocationInfo(latitude=cfg.latitude, longitude=cfg.longitude)
        self.tz = tz
        self._day: date | None = None
        self._sunrise = self._sunset = None
        self._phase_day: date | None = None
        self._phase = MOON_NAMES[0]

    def is_day(self, now: datetime) -> bool:
        d = now.date()
        if d != self._day:
            try:
                times = sun(self.loc.observer, date=d, tzinfo=self.tz)
                self._sunrise, self._sunset = times["sunrise"], times["sunset"]
            except Exception as exc:            # polar day/night, bad coords
                log.warning("sunrise/sunset unavailable for %s: %s", d, exc)
                self._sunrise = self._sunset = None
            self._day = d
        if self._sunrise is None:
            return 6 <= now.hour < 18
        return self._sunrise <= now < self._sunset

    def moon_icon(self, day: date) -> str:
        if day != self._phase_day:
            try:
                self._phase = moon_phase_name(day)
            except Exception as exc:
                log.warning("moon phase unavailable for %s: %s", day, exc)
            self._phase_day = day
        return self._phase


# ═══════════════════════════════════════════════════════════════════════════
#  THE CLOCK
# ═══════════════════════════════════════════════════════════════════════════

TOUCH_DURATION = 0.3
TOUCH_RADIUS = 40
TOUCH_COLOR = (255, 255, 255)


class Clock:
    def __init__(self, cfg: Config, screen: pygame.Surface, store: Store,
                 logical: tuple[int, int] | None = None):
        self.cfg = cfg
        self.screen = screen
        self.store = store
        self.tz = resolve_tz(cfg.timezone)
        sw, sh = logical or screen.get_size()
        self.assets = Assets(cfg, sw, sh)
        # SRCALPHA on purpose even though every pixel is opaque: see Sprite.
        # SDL picks its blitter from the surface format, and the alpha-less
        # path mishandles the tail of each row on some builds.
        self.canvas = opaque_surface((sw, sh)).convert_alpha()
        self.sky = SkyCache(cfg, self.tz)
        self.touches: list[tuple[tuple[int, int], float]] = []
        self._drawn: dict[str, tuple] = {}      # element -> (state, last rect)
        self._dim = 1.0
        self._force_full = True
        self._unknown_weather: str | None = None
        # Anchor the drift phase to wall-clock time, then advance it
        # monotonically.  Starting from zero on every boot would replay the
        # same first stretch of the path each time, so a device that reboots
        # daily would concentrate its wear exactly where the drift is meant to
        # spread it.  Reading the wall clock once keeps the path continuous
        # across restarts without exposing it to a later NTP step.
        self._t0 = time.monotonic() - time.time()
        err = check_subpixel()
        if err > 4.0:
            log.warning("sub-pixel blending is inexact on this SDL build (worst error "
                        "%.1f/255); anti burn-in drift will be coarser than intended", err)
        else:
            log.debug("sub-pixel blend check: worst error %.2f/255", err)
        log.info("layout: strip %dx%d at (%.0f, %.0f), top_reserved %d, drift ±%.0f/±%.0f px "
                 "peak %.3f px/s", self.assets.strip_w, self.assets.digit_h,
                 self.assets.strip_x, self.assets.strip_y, self.assets.top_reserved,
                 self.assets.drift_digits.amp_x, self.assets.drift_digits.amp_y,
                 self.assets.drift_digits.peak_speed())

    # -- what to show -------------------------------------------------------
    def display_time(self, now: datetime) -> datetime:
        return now + timedelta(hours=self.cfg.time_adjust)

    def dim_factor(self, shown: datetime) -> float:
        nd = self.cfg.night_dim
        if not nd.enabled:
            return 1.0
        minute = shown.hour * 60 + shown.minute
        inside = (nd.start <= minute < nd.end) if nd.start <= nd.end \
            else (minute >= nd.start or minute < nd.end)
        return nd.brightness if inside else 1.0

    def visible_alerts(self, snap: dict, mono: float) -> list[tuple[AlertSpec, str]]:
        out = []
        for spec in self.cfg.alerts.items:
            reading = snap.get(f"alert:{spec.id}")
            if reading is None or not reading.fresh(spec.stale_after, mono):
                continue
            sev = str(reading.value)
            if sev not in SEVERITY_RANK:
                continue                                   # unknown; treat as no reading
            if SEVERITY_RANK[sev] >= SEVERITY_RANK[spec.show_at]:
                out.append((spec, sev))
        return out

    # -- the frame ----------------------------------------------------------
    def _elements(self, now: datetime) -> list[tuple]:
        """Everything drawable this frame as (name, sprite, x, y, content_key).

        One entry per independently drifting thing.  `content_key` is whatever
        would change that element's pixels other than its position.
        """
        cfg, A = self.cfg, self.assets
        mono = time.monotonic() - self._t0
        snap = self.store.snapshot()
        shown = self.display_time(now)
        is_day = self.sky.is_day(now)
        theme = "day" if is_day else "night"
        pad = cfg.padding
        out: list[tuple] = []

        # weather, top-left
        weather = snap.get("weather")
        spr = None
        name = None
        if weather is not None and weather.fresh(cfg.weather_stale_after):
            name = str(weather.value)
            spr = A.weather[theme].get(name)
            if spr is None and name != self._unknown_weather:
                self._unknown_weather = name
                log.warning("no artwork for weather condition %r; expected one of %s",
                            name, ", ".join(sorted(A.weather[theme])) or "(none)")
        dx, dy = A.drift_weather.at(mono)
        out.append(("weather", spr, pad + dx, pad + dy, (theme, name)))

        # weekday sun / moon phase, top-right
        icon = shown.strftime("%A").lower() if is_day else self.sky.moon_icon(shown.date())
        spr = A.sun.get(icon) if is_day else A.moon.get(icon)
        dx, dy = A.drift_corner.at(mono)
        x = (A.sw - spr.w - pad + dx) if spr else 0.0
        out.append(("corner", spr, x, pad + dy, (theme, icon)))

        # alert row, centred above the temperature
        active = tuple((spec.id, sev) for spec, sev in self.visible_alerts(snap, time.monotonic()))
        row = A.alert_row_sprite(theme, active)
        dx, dy = A.drift_alerts.at(mono)
        x = ((A.sw - row.w) / 2.0 + dx) if row else 0.0
        out.append(("alerts", row, x, A.alert_band_top + dy, (theme, active)))

        # temperature
        temp = snap.get("temp")
        spr = None
        text = None
        if temp is not None and temp.fresh(cfg.temp_stale_after):
            raw = cfg.temp_color_day if is_day else cfg.temp_color_night
            b = cfg.temp_brightness_day if is_day else cfg.temp_brightness_night
            colour = tuple(min(255, int(c * b)) for c in raw)
            text = f"{float(temp.value):.0f}\N{DEGREE SIGN}{cfg.temp_unit}"
            spr = A.temp_sprite(text, colour)
        dx, dy = A.drift_temp.at(mono)
        x = ((A.sw - spr.w) / 2.0 + dx) if spr else 0.0
        out.append(("temp", spr, x, A.temp_ink_top + dy, (theme, text)))

        # the time
        glyphs = tuple(glyph_sequence(shown))
        strip = A.strip_sprite(theme, glyphs)
        dx, dy = A.drift_digits.at(mono)
        out.append(("digits", strip, A.strip_x + dx, A.strip_y + dy, (theme, glyphs)))
        return out

    def render(self, now: datetime, full: bool = False) -> list[pygame.Rect]:
        """Redraw only the elements that actually changed.

        The digit strip is by far the most expensive thing on screen — its
        four-tap sub-pixel blit is two thirds of a full frame — and it drifts
        three times slower than the temperature.  Rebuilding the whole canvas
        whenever anything moved therefore redrew the strip about three times
        more often than it had moved.  Elements do not overlap, so each one is
        cleared and redrawn on its own schedule instead.

        Returns the rectangles that changed, for a partial screen update.
        """
        q = self.cfg.drift.quantum
        dim = self.dim_factor(self.display_time(now))
        if dim < 0.999:
            # A whole-canvas multiply cannot be applied incrementally without
            # dimming already-dimmed pixels, so night_dim redraws in full.
            full = True
        if dim != self._dim:
            self._dim, full = dim, True

        dirty: list[pygame.Rect] = []
        if full:
            self.canvas.fill((0, 0, 0, 255))
            self._drawn.clear()
            dirty.append(self.canvas.get_rect())

        elements = self._elements(now)
        planned = []
        for name, spr, x, y, content in elements:
            rect = (pygame.Rect(math.floor(x) - 1, math.floor(y) - 1, spr.w + 2, spr.h + 2)
                    if spr is not None else None)
            planned.append((name, spr, x, y, (content, int(x / q), int(y / q)), rect))

        changed = {name for name, _, _, _, state, _ in planned
                   if full or (self._drawn.get(name) or (None, None))[0] != state}

        # Clearing one element's rectangle would erase any neighbour that
        # overlaps it, so a redraw pulls in everything it touches.  Nothing
        # overlaps in the shipped layout, but a config with big icons or many
        # alerts can make them meet, and a silently corrupted frame that only
        # appears at the extremes of a 3-hour drift is not a good failure mode.
        def rects_of(entry):
            prev = self._drawn.get(entry[0])
            return [r for r in (entry[5], prev[1] if prev else None) if r is not None]

        growing = True
        while growing:
            growing = False
            hot = [r for e in planned if e[0] in changed for r in rects_of(e)]
            for entry in planned:
                if entry[0] in changed:
                    continue
                if any(r.colliderect(h) for r in rects_of(entry) for h in hot):
                    changed.add(entry[0])
                    growing = True

        if not full:
            for entry in planned:
                if entry[0] not in changed:
                    continue
                prev = self._drawn.get(entry[0])
                if prev is not None and prev[1] is not None:
                    self.canvas.fill((0, 0, 0, 255), prev[1])
                    dirty.append(prev[1])

        for name, spr, x, y, state, rect in planned:
            if name not in changed:
                continue
            if spr is not None:
                blit_subpixel(self.canvas, spr, x, y)
                if not full:
                    dirty.append(rect)
            self._drawn[name] = (state, rect)

        if dim < 0.999:
            m = max(0, min(255, int(round(dim * 255))))
            self.canvas.fill((m, m, m, 255), None, pygame.BLEND_RGB_MULT)
        return dirty

    def present(self, dirty: Sequence[pygame.Rect] | None = None) -> None:
        """Canvas → screen.  `dirty` limits the copy to the rectangles that
        actually changed; None means the whole frame."""
        scaled = self.screen.get_size() != self.canvas.get_size()
        if scaled or dirty is None or self.touches:
            # Touch ripples are drawn straight onto the screen and are not in
            # the canvas, so a partial update would not erase them.
            if scaled:
                try:
                    pygame.transform.smoothscale(self.canvas, self.screen.get_size(), self.screen)
                except (ValueError, pygame.error):
                    self.screen.blit(pygame.transform.smoothscale(
                        self.canvas, self.screen.get_size()), (0, 0))
            else:
                self.screen.blit(self.canvas, (0, 0))
            for pos, _ in self.touches:
                pygame.draw.circle(self.screen, TOUCH_COLOR, pos, TOUCH_RADIUS, 3)
            pygame.display.flip()
            return
        if not dirty:
            return
        clip = self.screen.get_rect()
        rects = [r.clip(clip) for r in dirty]
        rects = [r for r in rects if r.width and r.height]
        for r in rects:
            self.screen.blit(self.canvas, r, r)
        pygame.display.update(rects)

    # -- main loop ----------------------------------------------------------
    GIVE_UP_AFTER = 60          # consecutive failing ticks

    def run(self, stop: threading.Event) -> bool:
        """Drive the display until `stop`.  Returns True on a clean exit, False
        if it gave up because something kept failing — `main` turns that into a
        non-zero status so `Restart=on-failure` actually restarts it."""
        pyclock = pygame.time.Clock()
        broken = 0
        while not stop.is_set():
            try:
                self._tick(stop)
            except pygame.error as exc:
                # A compositor restart, a VT switch or an HDMI renegotiation
                # can make one frame fail.  Riding that out beats ending a
                # months-long run.
                broken += 1
                log.warning("display error (%d in a row): %s", broken, exc)
            except Exception:
                # Anything else gets the same treatment and the same counter:
                # a repeatable error must not leave the panel frozen forever.
                broken += 1
                log.exception("error in the main loop (%d in a row)", broken)
            else:
                broken = 0
                pyclock.tick(self.cfg.fps)
                continue
            if broken >= self.GIVE_UP_AFTER:
                log.error("%d consecutive failures; giving up so a supervisor can "
                          "restart cleanly", broken)
                stop.set()
                return False
            stop.wait(1.0)
        return True

    def _tick(self, stop: threading.Event) -> None:
            now = datetime.now(self.tz)
            mono = time.monotonic()

            for e in pygame.event.get():
                if e.type == pygame.QUIT:
                    log.info("window closed; exiting")
                    stop.set()
                elif e.type == pygame.KEYDOWN and e.key in (pygame.K_ESCAPE, pygame.K_q):
                    log.info("%s pressed; exiting", pygame.key.name(e.key))
                    stop.set()
                elif e.type == pygame.MOUSEBUTTONDOWN:
                    self.touches.append((e.pos, mono + TOUCH_DURATION))
                elif e.type == pygame.FINGERDOWN:
                    self.touches.append(((int(e.x * self.screen.get_width()),
                                          int(e.y * self.screen.get_height())),
                                         mono + TOUCH_DURATION))

            had_touches = bool(self.touches)
            self.touches[:] = [t for t in self.touches if t[1] > mono]

            try:
                dirty = self.render(now, full=self._force_full)
            except Exception:
                log.exception("render failed; keeping the previous frame")
                self._force_full = True         # rebuild from scratch next time
                return
            self._force_full = False
            if dirty:
                self.present(None if self.touches or had_touches else dirty)
            elif self.touches or had_touches:
                self.present(None)


# ═══════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════

def init_display(cfg: Config, windowed: bool,
                 headless: bool) -> tuple[pygame.Surface, tuple[int, int]]:
    """Open the display and return it with the *logical* frame size.

    The two are not always the same: windowed development renders the full
    1920x1080 layout and scales it into a half-size window, and a panel whose
    native mode is not the logical size gets the same treatment.
    """
    if headless:
        os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
    os.environ.setdefault("SDL_VIDEO_CENTERED", "1")
    # pygame.init() also spins up the audio mixer, which on a headless
    # appliance means an ALSA probe and a stack of harmless-looking errors.
    pygame.display.init()
    pygame.font.init()

    if windowed or headless:
        logical = (cfg.screen_w or 1920, cfg.screen_h or 1080)
        window = (logical[0] // 2, logical[1] // 2) if windowed and not headless else logical
        screen = pygame.display.set_mode(window)
    else:
        info = pygame.display.Info()
        logical = (cfg.screen_w or info.current_w, cfg.screen_h or info.current_h)
        screen = pygame.display.set_mode(logical, pygame.FULLSCREEN)
        if screen.get_size() != logical:
            log.warning("asked for a %sx%s mode, got %sx%s; the frame will be scaled",
                        *logical, *screen.get_size())
    pygame.display.set_caption(f"Image Clock - theme: {cfg.theme}")
    pygame.mouse.set_visible(False)
    return screen, logical


def selftest(cfg: Config) -> int:
    """Hit every configured endpoint once and report.  Never touches the display."""
    store, stop = Store(), threading.Event()
    rc = 0
    for poller in build_pollers(cfg, store, stop):
        if not poller.url:
            print(f"  {poller.key:22s} (not configured)")
            continue
        t0 = time.monotonic()
        try:
            value = poller.parse(http_get_text(poller.url, poller.timeout))
            print(f"  {poller.key:22s} OK    {value!r}  "
                  f"({(time.monotonic()-t0)*1000:.0f} ms)  {poller.url}")
        except Exception as exc:                       # noqa: BLE001
            rc = 1
            print(f"  {poller.key:22s} FAIL  {type(exc).__name__}: {exc}  {poller.url}")
    return rc


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", type=pathlib.Path, default=CONFIG_PATH)
    ap.add_argument("--windowed", action="store_true", help="run in a half-size window")
    ap.add_argument("--screenshot", type=pathlib.Path, metavar="PNG",
                    help="render one frame to PNG and exit")
    ap.add_argument("--at", metavar="HH:MM", help="with --screenshot, pretend it is this time")
    ap.add_argument("--alerts", metavar="IDS",
                    help="with --screenshot, force these alert ids on (comma separated, "
                         "optionally id=warn|alarm)")
    ap.add_argument("--selftest", action="store_true", help="probe every endpoint once and exit")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args(argv)

    setup_logging(args.verbose)
    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        log.error("%s", exc)
        return 2

    if args.selftest:
        print(f"theme {cfg.theme}, {len(cfg.alerts.items)} alert(s)")
        return selftest(cfg)

    headless = bool(args.screenshot)
    try:
        screen, logical = init_display(cfg, args.windowed, headless)
    except pygame.error as exc:
        log.error("cannot open a display: %s", exc)
        return 2

    store = Store()
    stop = threading.Event()
    try:
        clock = Clock(cfg, screen, store, logical)
    except ConfigError as exc:
        log.error("%s", exc)
        return 2

    if args.screenshot:
        _screenshot(clock, store, args)
        return 0

    for poller in build_pollers(cfg, store, stop):
        poller.start()
    clean = True
    try:
        clean = clock.run(stop)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        pygame.quit()
    return 0 if clean else 3


def _screenshot(clock: Clock, store: Store, args) -> None:
    """Render a single deterministic frame for eyeballing or regression tests."""
    now = datetime.now(clock.tz)
    if args.at:
        try:
            at = datetime.strptime(args.at, "%H:%M").time()
        except ValueError:
            raise SystemExit(f"--at expects HH:MM, got {args.at!r}") from None
        now = now.replace(hour=at.hour, minute=at.minute, second=0, microsecond=0)
        now -= timedelta(hours=clock.cfg.time_adjust)
    for poller in build_pollers(clock.cfg, store, threading.Event()):
        if not poller.url:
            continue
        try:
            store.put(poller.key, Reading(value=poller.parse(http_get_text(poller.url, 3.0)),
                                          fetched_at=time.monotonic(), ok=True))
        except Exception as exc:                       # noqa: BLE001
            log.warning("%s unavailable for the screenshot: %s", poller.key, exc)
    for item in (args.alerts or "").split(","):
        if not item.strip():
            continue
        aid, _, sev = item.partition("=")
        store.put(f"alert:{aid.strip()}",
                  Reading(value=(sev or "alarm").strip(), fetched_at=time.monotonic(), ok=True))
    clock.render(now, full=True)
    try:
        pygame.image.save(clock.canvas, str(args.screenshot))
    except pygame.error:                               # pygame built without SDL_image
        from PIL import Image
        Image.frombytes("RGB", clock.canvas.get_size(),
                        pygame.image.tobytes(clock.canvas, "RGB")).save(args.screenshot)
    print(f"wrote {args.screenshot}")


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""
Themed fullscreen image clock 1920×1080
• Theme chosen in config.json → images/<theme>/…
• Daytime corner icon:   sun/<weekday>.png
• Night-time corner icon: moon/<phase>.png  (phase from astral.moon.phase)
• HH:MM am/pm  (colon + am/pm = half-width)
• Per-theme brightness  • anti burn-in drift
"""

import json, os, pathlib, random, sys
from datetime import datetime, timedelta

import pygame
from astral import LocationInfo, moon
from astral.sun import sun
from pytz import timezone as tzlib

# ─── CONSTANTS ───────────────────────────────────────────────────────────────
SCREEN_W, SCREEN_H = 1920, 1080
TZ_NAME            = "America/New_York"
LAT, LON           = 42.95, -72.04                 # adjust if desired

BASE_DIR           = pathlib.Path(__file__).parent
IMAGES_ROOT        = BASE_DIR / "images"
CONFIG_PATH        = BASE_DIR / "config.json"

PADDING, DRIFT_PIXELS = 40, 6
DRIFT_PERIOD          = timedelta(minutes=5)
FPS                   = 30
DEFAULT_THEME         = "default"
# ─────────────────────────────────────────────────────────────────────────────

# ─── LOAD CONFIG ─────────────────────────────────────────────────────────────
def load_config(path):
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        data = {}
    theme = data.get("theme", DEFAULT_THEME)
    clamp = lambda v: max(0.0, min(1.0, float(v)))
    b_day   = clamp(data.get("brightness_day",   1.0))
    b_night = clamp(data.get("brightness_night", 1.0))
    return theme, b_day, b_night

THEME_NAME, B_DAY, B_NIGHT = load_config(CONFIG_PATH)
THEME_DIR = IMAGES_ROOT / THEME_NAME
if not THEME_DIR.exists():
    sys.exit(f"Theme '{THEME_NAME}' not found at {THEME_DIR}")

# ─── INIT PYGAME ─────────────────────────────────────────────────────────────
os.environ["SDL_VIDEO_CENTERED"] = "1"
pygame.init()
tz      = tzlib(TZ_NAME)
screen  = pygame.display.set_mode((SCREEN_W, SCREEN_H), pygame.FULLSCREEN)
pygame.display.set_caption(f"Image Clock – theme: {THEME_NAME}")
clock   = pygame.time.Clock()

# ─── CORNER ICONS (SUN & MOON) ───────────────────────────────────────────────
def scale_icon(raw):
    max_w, max_h = SCREEN_W/4, SCREEN_H/3
    f = min(max_w/raw.get_width(), max_h/raw.get_height(), 1.0)
    return pygame.transform.smoothscale(raw, (int(raw.get_width()*f),
                                              int(raw.get_height()*f)))

SUN_ICONS = {
    d: scale_icon(pygame.image.load(THEME_DIR/"sun"/f"{d}.png").convert_alpha())
    for d in ("monday","tuesday","wednesday","thursday","friday","saturday","sunday")
}

MOON_NAMES = (
    "new","waxing_crescent","first_quarter","waxing_gibbous",
    "full","waning_gibbous","third_quarter","waning_crescent"
)
MOON_ICONS = {
    n: scale_icon(pygame.image.load(THEME_DIR/"moon"/f"{n}.png").convert_alpha())
    for n in MOON_NAMES
}

MAX_ICON_H   = max(i.get_height() for i in (*SUN_ICONS.values(), *MOON_ICONS.values()))
TOP_RESERVED = MAX_ICON_H + 2*PADDING


# ─── MOON-PHASE → FILENAME (8-way) ───────────────────────────────────────────
MOON_NAMES = (
    "new", "waxing_crescent", "first_quarter", "waxing_gibbous",
    "full", "waning_gibbous", "third_quarter", "waning_crescent"
)

def moon_phase_name(date):
    """
    Return one of the eight MOON_NAMES strings for the given date.
    astral.moon.phase(date) → float days into synodic month (0-29.53…).
    Shift by 1.84566 and divide by 3.69134 (≈ 1/8 of synodic month) to bucket.
    """
    p   = moon.phase(date)                     # 0 ≤ p < 29.53
    idx = int((p + 1.84566) // 3.69134) % 8    # 0 – 7
    return MOON_NAMES[idx]

# ─── DIGIT GLYPHS ────────────────────────────────────────────────────────────
expected = {"0","1","2","3","4","5","6","7","8","9","colon","am","pm"}
def load_digits(folder):
    imgs = {f.stem: pygame.image.load(f) for f in folder.glob("*.png")}
    miss = expected - imgs.keys()
    if miss:
        raise FileNotFoundError(f"{folder} missing: {', '.join(sorted(miss))}")
    return imgs

DIGIT_RAW = {
    "day":   load_digits(THEME_DIR/"day"),
    "night": load_digits(THEME_DIR/"night")
}

def apply_brightness(surf,f):
    if f>=.999: return surf
    mult=int(f*255)
    s=surf.copy()
    s.fill((mult,mult,mult,255),None,pygame.BLEND_RGBA_MULT)
    return s

sample = next(iter(DIGIT_RAW["day"].values()))
ow, oh = sample.get_width(), sample.get_height()
TOTAL_UNITS=5.0
scale = min((SCREEN_W-2*PADDING)/(TOTAL_UNITS*ow),
            (SCREEN_H-TOP_RESERVED-2*PADDING)/oh)
W_FULL  = max(1, round(ow*scale))
W_HALF  = max(1, round(W_FULL*0.5))
H_ALL   = max(1, round(oh*scale))
NARROW  = {"colon","am","pm"}

DIGITS={}
for theme,bright in (("day",B_DAY),("night",B_NIGHT)):
    d={}
    for k,surf in DIGIT_RAW[theme].items():
        tw=W_HALF if k in NARROW else W_FULL
        d[k]=apply_brightness(
            pygame.transform.smoothscale(surf,(tw,H_ALL)).convert_alpha(), bright)
    DIGITS[theme]=d

STRIP_W = 4*W_FULL + 2*W_HALF
def base_origin():
    cx=(SCREEN_W-STRIP_W)//2
    cy=TOP_RESERVED + (SCREEN_H-TOP_RESERVED-H_ALL)//2
    return cx,cy

origin, next_shift = base_origin(), datetime.now(tz)+DRIFT_PERIOD

def glyph_seq(dt):
    s=dt.strftime("%I:%M%p").lower()
    return [s[0],s[1],"colon",s[3],s[4],s[5:]]

# ─── MAIN LOOP ───────────────────────────────────────────────────────────────
loc = LocationInfo(latitude=LAT, longitude=LON)
running=True
while running:
    now = datetime.now(tz)
    sun_times = sun(loc.observer, date=now.date(), tzinfo=tz)
    is_day    = sun_times["sunrise"] <= now < sun_times["sunset"]
    theme_key = "day" if is_day else "night"
    glyphs    = DIGITS[theme_key]

    # corner icon
    if is_day:
        icon = SUN_ICONS[now.strftime("%A").lower()]
    else:
        icon = MOON_ICONS[moon_phase_name(now.date())]
    icon_x = SCREEN_W - icon.get_width() - PADDING

    # drift
    if now >= next_shift:
        bx,by = base_origin()
        origin=(bx+random.randint(-DRIFT_PIXELS,DRIFT_PIXELS),
                by+random.randint(-DRIFT_PIXELS,DRIFT_PIXELS))
        next_shift = now + DRIFT_PERIOD

    for e in pygame.event.get():
        if e.type==pygame.QUIT or (e.type==pygame.KEYDOWN and e.key in (pygame.K_ESCAPE,pygame.K_q)):
            running=False

    # draw
    screen.fill((0,0,0))
    screen.blit(icon,(icon_x,PADDING))
    x,y=origin
    for k in glyph_seq(now):
        screen.blit(glyphs[k],(x,y))
        x += W_HALF if k in NARROW else W_FULL
    pygame.display.flip()
    clock.tick(FPS)

pygame.quit()
sys.exit()


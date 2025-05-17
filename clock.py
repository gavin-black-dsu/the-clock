#!/usr/bin/env python3
"""
Themed fullscreen image clock 1920×1080
• Theme chosen in config.json → images/<theme>/…
• Daytime corner icon:   sun/<weekday>.png
• Night-time corner icon: moon/<phase>.png  (phase from astral.moon.phase)
• HH:MM am/pm  (colon + am/pm = half-width)
• Per-theme brightness
• Separate sun/moon brightness
• Temperature readout centered at top with configurable font size,
  color, brightness, and padding
• Anti burn-in drift
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

PADDING, DRIFT_PIXELS   = 40, 6
DRIFT_PERIOD            = timedelta(minutes=5)
FPS                     = 30
DEFAULT_THEME           = "default"
TOUCH_DURATION          = timedelta(milliseconds=300)
TOUCH_RADIUS            = 40
TOUCH_COLOR             = (255, 255, 255)
DEFAULT_TEMP_FONT_SIZE  = 64
DEFAULT_TEMP_PADDING_TOP= 40
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
    b_sun   = clamp(data.get("brightness_sun",   1.0))
    b_moon  = clamp(data.get("brightness_moon",  1.0))
    b_temp_day   = clamp(data.get("temp_brightness_day",   1.0))
    b_temp_night = clamp(data.get("temp_brightness_night", 1.0))
    col_day   = tuple(data.get("temp_color_day",  [255, 255, 255]))
    col_night = tuple(data.get("temp_color_night", [255, 255, 255]))
    size      = int(data.get("temp_font_size", DEFAULT_TEMP_FONT_SIZE))
    pad_top   = int(data.get("temp_padding_top", DEFAULT_TEMP_PADDING_TOP))
    return (
        theme, b_day, b_night, b_sun, b_moon,
        b_temp_day, b_temp_night, col_day, col_night,
        size, pad_top,
    )

(
    THEME_NAME, B_DAY, B_NIGHT, B_SUN, B_MOON,
    B_TEMP_DAY, B_TEMP_NIGHT, TEMP_COL_DAY, TEMP_COL_NIGHT,
    TEMP_FONT_SIZE, TEMP_PADDING_TOP,
) = load_config(CONFIG_PATH)
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
pygame.mouse.set_visible(False)

# ─── CORNER ICONS (SUN & MOON) ───────────────────────────────────────────────
def apply_brightness(surf,f):
    if f>=.999:
        return surf
    mult=int(f*255)
    s=surf.copy()
    s.fill((mult,mult,mult,255),None,pygame.BLEND_RGBA_MULT)
    return s

def scale_icon(raw):
    max_w, max_h = SCREEN_W/4, SCREEN_H/3
    f = min(max_w/raw.get_width(), max_h/raw.get_height(), 1.0)
    return pygame.transform.smoothscale(raw, (int(raw.get_width()*f),
                                              int(raw.get_height()*f)))

SUN_ICONS = {
    d: apply_brightness(
        scale_icon(pygame.image.load(THEME_DIR/"sun"/f"{d}.png").convert_alpha()),
        B_SUN
    )
    for d in ("monday","tuesday","wednesday","thursday","friday","saturday","sunday")
}

MOON_NAMES = (
    "new","waxing_crescent","first_quarter","waxing_gibbous",
    "full","waning_gibbous","third_quarter","waning_crescent"
)
MOON_ICONS = {
    n: apply_brightness(
        scale_icon(pygame.image.load(THEME_DIR/"moon"/f"{n}.png").convert_alpha()),
        B_MOON
    )
    for n in MOON_NAMES
}

MAX_ICON_H = max(i.get_height() for i in (*SUN_ICONS.values(), *MOON_ICONS.values()))


# ─── TEMPERATURE ─────────────────────────────────────────────────────────────
def get_temperature():
    """Placeholder for external temperature reading (°F)."""
    return 72.0

font_path = pygame.font.match_font("comicsansms")
TEMP_FONT = pygame.font.Font(font_path or None, TEMP_FONT_SIZE)
TOP_RESERVED = max(
    MAX_ICON_H + 2 * PADDING,
    TEMP_PADDING_TOP + TEMP_FONT.get_height() + PADDING,
)


# ─── MOON-PHASE → FILENAME (8-way) ───────────────────────────────────────────
# `MOON_NAMES` defined above with icon loading

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
icon_offset = (0,0)
temp_offset = (0,0)
touches = []

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

    temp_val  = get_temperature()
    temp_txt  = f"{temp_val:.0f}\N{DEGREE SIGN}F"
    raw_col   = TEMP_COL_DAY if is_day else TEMP_COL_NIGHT
    bright    = B_TEMP_DAY if is_day else B_TEMP_NIGHT
    col       = tuple(min(255, int(c*bright)) for c in raw_col)
    temp_surf = TEMP_FONT.render(temp_txt, True, col)
    temp_x    = (SCREEN_W - temp_surf.get_width()) // 2 + temp_offset[0]
    temp_y    = TEMP_PADDING_TOP + temp_offset[1]

    # corner icon
    if is_day:
        icon = SUN_ICONS[now.strftime("%A").lower()]
    else:
        icon = MOON_ICONS[moon_phase_name(now.date())]
    icon_x = SCREEN_W - icon.get_width() - PADDING + icon_offset[0]
    icon_y = PADDING + icon_offset[1]

    # drift
    if now >= next_shift:
        bx,by = base_origin()
        origin=(bx+random.randint(-DRIFT_PIXELS,DRIFT_PIXELS),
                by+random.randint(-DRIFT_PIXELS,DRIFT_PIXELS))
        icon_offset=(random.randint(-DRIFT_PIXELS,DRIFT_PIXELS),
                     random.randint(-DRIFT_PIXELS,DRIFT_PIXELS))
        temp_offset=(random.randint(-DRIFT_PIXELS,DRIFT_PIXELS),
                     random.randint(-DRIFT_PIXELS,DRIFT_PIXELS))
        next_shift = now + DRIFT_PERIOD

    for e in pygame.event.get():
        if e.type==pygame.QUIT or (e.type==pygame.KEYDOWN and e.key in (pygame.K_ESCAPE,pygame.K_q)):
            running=False
        elif e.type==pygame.MOUSEBUTTONDOWN:
            touches.append((e.pos, now + TOUCH_DURATION))

    touches[:] = [t for t in touches if t[1] > now]

    # draw
    screen.fill((0,0,0))
    screen.blit(icon,(icon_x,icon_y))
    screen.blit(temp_surf,(temp_x,temp_y))
    x,y=origin
    for k in glyph_seq(now):
        screen.blit(glyphs[k],(x,y))
        x += W_HALF if k in NARROW else W_FULL
    for pos,_ in touches:
        pygame.draw.circle(screen, TOUCH_COLOR, pos, TOUCH_RADIUS, 3)
    pygame.display.flip()
    clock.tick(FPS)

pygame.quit()
sys.exit()


#!/usr/bin/env python3
"""Kindle 4 weather dashboard server.

Fetches a forecast from Open-Meteo (free, no API key), renders it as a
600x800 greyscale PNG sized for the Kindle 4 e-ink panel, and serves it over
plain HTTP for kindle-dash to fetch.

    python3 weather_dash.py render --demo -o preview.png   # offline layout check
    python3 weather_dash.py render -o dash.png             # one render, live data
    python3 weather_dash.py serve --port 8080              # serve /dash.png on your LAN

Location defaults to Shrewsbury. Override with --lat / --lon / --place, or the
DASH_LAT / DASH_LON / DASH_PLACE environment variables.

The Kindle can pass its battery level as /dash.png?battery=85 and it is drawn
in the header.

Requires Pillow >= 10.1   (pip install pillow certifi)
"""

from __future__ import annotations

import argparse
import io
import json
import math
import os
import ssl
import sys
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen

from PIL import Image, ImageDraw, ImageFont

# --------------------------------------------------------------------------
# Settings

W, H = 600, 800            # Kindle 4 NT panel, portrait
SS = 2                     # supersampling factor, for smooth edges after downscale
M = 30                     # page margin
CACHE_SECONDS = 10 * 60    # Open-Meteo's current conditions update every 15 min
STALE_LIMIT = 6 * 3600     # keep serving cached data this long if the API is down

# The K4 panel shows 16 grey levels; stick to a few well-separated ones.
BLACK, DARK, MID, LIGHT, PALE, WHITE = 0x00, 0x44, 0x66, 0xAA, 0xD4, 0xFF

API = "https://api.open-meteo.com/v1/forecast"

# WMO weather code -> (description, icon)
WMO = {
    0: ("Clear", "clear"),
    1: ("Mainly clear", "clear"),
    2: ("Partly cloudy", "partly"),
    3: ("Overcast", "cloudy"),
    45: ("Fog", "fog"),
    48: ("Freezing fog", "fog"),
    51: ("Light drizzle", "drizzle"),
    53: ("Drizzle", "drizzle"),
    55: ("Heavy drizzle", "drizzle"),
    56: ("Freezing drizzle", "drizzle"),
    57: ("Freezing drizzle", "drizzle"),
    61: ("Light rain", "rain"),
    63: ("Rain", "rain"),
    65: ("Heavy rain", "heavy"),
    66: ("Freezing rain", "rain"),
    67: ("Freezing rain", "heavy"),
    71: ("Light snow", "snow"),
    73: ("Snow", "snow"),
    75: ("Heavy snow", "snow"),
    77: ("Snow grains", "snow"),
    80: ("Light showers", "showers"),
    81: ("Showers", "showers"),
    82: ("Heavy showers", "heavy"),
    85: ("Snow showers", "snow"),
    86: ("Heavy snow showers", "snow"),
    95: ("Thunderstorm", "thunder"),
    96: ("Thunder and hail", "thunder"),
    99: ("Thunder and hail", "thunder"),
}

COMPASS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]


def log(msg: str) -> None:
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", file=sys.stderr, flush=True)


# --------------------------------------------------------------------------
# Fonts

FONT_FILES = {
    False: ["DejaVuSans.ttf", "LiberationSans-Regular.ttf", "Arial.ttf", "arial.ttf"],
    True: ["DejaVuSans-Bold.ttf", "LiberationSans-Bold.ttf", "Arial Bold.ttf", "arialbd.ttf"],
}
FONT_DIRS = [
    os.environ.get("DASH_FONT_DIR", ""),
    "/usr/share/fonts/truetype/dejavu",
    "/usr/share/fonts/dejavu",
    "/usr/share/fonts/TTF",
    "/usr/share/fonts/truetype/liberation",
    "/usr/share/fonts/liberation",
    "/Library/Fonts",
    "/System/Library/Fonts/Supplemental",
    "C:/Windows/Fonts",
]


@lru_cache(maxsize=None)
def font_path(bold: bool) -> str | None:
    for d in filter(None, FONT_DIRS):
        for name in FONT_FILES[bold]:
            path = Path(d) / name
            if path.is_file():
                return str(path)
    return None


@lru_cache(maxsize=None)
def font(size: float, bold: bool = False) -> ImageFont.FreeTypeFont:
    px = round(size * SS)
    path = font_path(bold)
    if path:
        return ImageFont.truetype(path, px)
    return ImageFont.load_default(px)  # Pillow >= 10.1; plainer, but works anywhere


# --------------------------------------------------------------------------
# Drawing helpers. All coordinates are in 600x800 "logical" pixels and scaled
# up by SS internally; the final image is downsampled, which anti-aliases
# every shape for free.

class Pen:
    def __init__(self, img: Image.Image):
        self.d = ImageDraw.Draw(img)

    @staticmethod
    def _s(*vals: float) -> list[float]:
        return [v * SS for v in vals]

    def text(self, x, y, s, size, bold=False, fill=BLACK, anchor="ls", halo=0.0):
        # halo: white outline so text stays legible over the rain bars
        self.d.text((x * SS, y * SS), s, font=font(size, bold), fill=fill, anchor=anchor,
                    stroke_width=round(halo * SS), stroke_fill=WHITE)

    def width(self, s, size, bold=False) -> float:
        return self.d.textlength(s, font=font(size, bold)) / SS

    def fit(self, s, size, max_w, bold=False) -> float:
        while size > 12 and self.width(s, size, bold) > max_w:
            size -= 1
        return size

    def label(self, x, y, s, size=13, fill=MID, tracking=1.2):
        """Small letter-spaced caps label, left-aligned on baseline y."""
        for ch in s:
            self.text(x, y, ch, size, bold=True, fill=fill)
            x += self.width(ch, size, bold=True) + tracking

    def label_width(self, s, size=13, tracking=1.2) -> float:
        return sum(self.width(ch, size, bold=True) + tracking for ch in s) - tracking

    def line(self, pts, width=2.0, fill=BLACK, caps=True):
        sp = [(x * SS, y * SS) for x, y in pts]
        self.d.line(sp, fill=fill, width=max(1, round(width * SS)), joint="curve")
        if caps:
            r = width * SS / 2
            for x, y in (sp[0], sp[-1]):
                self.d.ellipse((x - r, y - r, x + r, y + r), fill=fill)

    def circle(self, cx, cy, r, fill=None, outline=None, width=0.0):
        self.d.ellipse(self._s(cx - r, cy - r, cx + r, cy + r), fill=fill,
                       outline=outline, width=round(width * SS) if outline is not None else 0)

    def rect(self, x0, y0, x1, y1, fill=None, outline=None, width=0.0, radius=0.0):
        if x1 <= x0 or y1 <= y0:
            return
        self.d.rounded_rectangle(self._s(x0, y0, x1, y1), radius=radius * SS, fill=fill,
                                 outline=outline,
                                 width=round(width * SS) if outline is not None else 0)

    def polygon(self, pts, fill=BLACK):
        self.d.polygon([(x * SS, y * SS) for x, y in pts], fill=fill)


# --------------------------------------------------------------------------
# Weather icons, drawn from primitives so there are no image assets to ship.

def sun(p: Pen, cx, cy, r, sw):
    p.circle(cx, cy, r, fill=WHITE, outline=BLACK, width=sw)
    for k in range(8):
        a = k * math.pi / 4
        p.line([(cx + math.cos(a) * r * 1.45, cy + math.sin(a) * r * 1.45),
                (cx + math.cos(a) * r * 1.9, cy + math.sin(a) * r * 1.9)], width=sw)


def moon(p: Pen, cx, cy, r, sw):
    p.circle(cx, cy, r, fill=DARK)
    p.circle(cx + r * 0.48, cy - r * 0.38, r * 0.86, fill=WHITE)


def cloud(p: Pen, cx, cy, w, sw, fill=WHITE):
    """Cloud roughly w wide, centred on (cx, cy), drawn as an outlined union of shapes."""
    yb = cy + 0.20 * w
    circles = [(cx - 0.25 * w, yb - 0.17 * w, 0.17 * w),
               (cx + 0.27 * w, yb - 0.15 * w, 0.15 * w),
               (cx + 0.01 * w, yb - 0.29 * w, 0.25 * w)]
    for grow, colour in ((sw, BLACK), (0, fill)):
        for x, y, r in circles:
            p.circle(x, y, r + grow, fill=colour)
        p.rect(cx - 0.25 * w, yb - 0.22 * w - grow, cx + 0.27 * w, yb + grow, fill=colour)


def snowflake(p: Pen, cx, cy, r, sw):
    for k in range(3):
        a = k * math.pi / 3 + math.pi / 2
        dx, dy = math.cos(a) * r, math.sin(a) * r
        p.line([(cx - dx, cy - dy), (cx + dx, cy + dy)], width=sw)


def draw_icon(p: Pen, code: int, cx, cy, size, day=True):
    kind = WMO.get(code, ("", "cloudy"))[1]
    sw = max(1.6, size * 0.045)
    body = sun if day else moon

    if kind == "clear":
        body(p, cx, cy, size * 0.24, sw)
        return
    if kind == "partly":
        body(p, cx - size * 0.15, cy - size * 0.15, size * 0.17, sw)
        cloud(p, cx + size * 0.08, cy + size * 0.09, size * 0.78, sw)
        return
    if kind == "cloudy":
        cloud(p, cx + size * 0.13, cy - size * 0.11, size * 0.60, sw, fill=PALE)
        cloud(p, cx - size * 0.05, cy + size * 0.05, size * 0.84, sw)
        return
    if kind == "fog":
        cloud(p, cx, cy - size * 0.13, size * 0.80, sw)
        for i, (a, b) in enumerate([(-0.34, 0.30), (-0.26, 0.38), (-0.38, 0.20)]):
            y = cy + size * (0.18 + i * 0.11)
            p.line([(cx + a * size, y), (cx + b * size, y)], width=sw)
        return

    # Precipitation: cloud on top, precipitation underneath.
    if kind == "showers":                     # sun/moon peeking out, cloud nudged right
        body(p, cx - size * 0.20, cy - size * 0.24, size * 0.14, sw)
        cloud(p, cx + size * 0.10, cy - size * 0.08, size * 0.78, sw)
    else:
        cloud(p, cx, cy - size * 0.12, size * 0.84, sw)
    top, bot = cy + size * 0.14, cy + size * 0.40

    if kind in ("rain", "showers", "heavy"):
        xs = [-0.24, -0.08, 0.08, 0.24] if kind == "heavy" else [-0.18, 0.0, 0.18]
        if kind == "showers":
            xs = [x + 0.08 for x in xs]
        for x in xs:
            p.line([(cx + (x + 0.04) * size, top), (cx + (x - 0.04) * size, bot)], width=sw)
    elif kind == "drizzle":
        for i, x in enumerate([-0.18, 0.0, 0.18]):
            off = 0.05 * size if i % 2 else 0
            for y in (top + off, top + off + size * 0.13):
                p.line([(cx + x * size, y), (cx + (x - 0.015) * size, y + size * 0.04)], width=sw)
    elif kind == "snow":
        for i, x in enumerate([-0.2, 0.0, 0.2]):
            y = (top + bot) / 2 + (size * 0.05 if i % 2 else -size * 0.03)
            snowflake(p, cx + x * size, y, size * 0.075, sw * 0.8)
    elif kind == "thunder":
        bolt = [(0.06, 0.08), (-0.09, 0.29), (0.01, 0.29), (-0.05, 0.47),
                (0.13, 0.22), (0.03, 0.22), (0.11, 0.08)]
        p.polygon([(cx + x * size, cy + y * size) for x, y in bolt], fill=BLACK)


def droplet(p: Pen, cx, cy, s, fill=MID):
    p.circle(cx, cy + 0.18 * s, 0.34 * s, fill=fill)
    p.polygon([(cx, cy - 0.5 * s), (cx - 0.31 * s, cy + 0.06 * s), (cx + 0.31 * s, cy + 0.06 * s)],
              fill=fill)


def battery_icon(p: Pen, x_right, y_mid, pct):
    w, h = 26, 12
    x0, y0 = x_right - w - 3, y_mid - h / 2
    p.rect(x0, y0, x0 + w, y0 + h, outline=BLACK, width=1.5, radius=2)
    p.rect(x0 + w, y_mid - 3, x0 + w + 3, y_mid + 3, fill=BLACK, radius=1)
    p.rect(x0 + 2.5, y0 + 2.5, x0 + 2.5 + (w - 5) * pct / 100, y0 + h - 2.5,
           fill=BLACK if pct > 20 else MID)


# --------------------------------------------------------------------------
# Data

@dataclass
class Hour:
    time: datetime
    temp: float
    rain: int


@dataclass
class Day:
    date: date
    code: int
    hi: float
    lo: float
    rain: int
    sunrise: datetime
    sunset: datetime


@dataclass
class Forecast:
    now: datetime          # location-local, naive (matches Open-Meteo's strings)
    fetched: datetime      # when the data was pulled, location-local
    temp: float
    feels: float
    humidity: int
    wind: float
    wind_dir: str
    code: int
    is_day: bool
    today: Day
    hours: list[Hour]
    days: list[Day]


def ssl_context() -> ssl.SSLContext:
    # python.org builds on macOS ship without CA certificates; certifi fixes that.
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def fetch_forecast(lat: float, lon: float) -> dict:
    params = {
        "latitude": lat,
        "longitude": lon,
        "current": "temperature_2m,apparent_temperature,relative_humidity_2m,"
                   "weather_code,wind_speed_10m,wind_direction_10m,is_day",
        "hourly": "temperature_2m,precipitation_probability",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,"
                 "precipitation_probability_max,sunrise,sunset",
        "timezone": "auto",
        "forecast_days": 7,
        "wind_speed_unit": "mph",
    }
    req = Request(f"{API}?{urlencode(params)}", headers={"User-Agent": "kindle-weather-dash/1.0"})
    with urlopen(req, timeout=20, context=ssl_context()) as r:
        return json.load(r)


def parse(data: dict, fetched_ts: float, now: datetime | None = None) -> Forecast:
    tz = timezone(timedelta(seconds=data.get("utc_offset_seconds", 0)))
    now = now or datetime.now(tz).replace(tzinfo=None)
    fetched = datetime.fromtimestamp(fetched_ts, tz).replace(tzinfo=None)

    cur, hourly, daily = data["current"], data["hourly"], data["daily"]

    times = [datetime.fromisoformat(t) for t in hourly["time"]]
    this_hour = now.replace(minute=0, second=0, microsecond=0)
    i0 = next((i for i, t in enumerate(times) if t >= this_hour), 0)
    hours = [Hour(times[i], hourly["temperature_2m"][i],
                  int(hourly["precipitation_probability"][i] or 0))
             for i in range(i0, min(i0 + 13, len(times)))]

    days = []
    for i, d in enumerate(daily["time"]):
        days.append(Day(
            date=date.fromisoformat(d),
            code=int(daily["weather_code"][i] or 0),
            hi=daily["temperature_2m_max"][i],
            lo=daily["temperature_2m_min"][i],
            rain=int(daily["precipitation_probability_max"][i] or 0),
            sunrise=datetime.fromisoformat(daily["sunrise"][i]),
            sunset=datetime.fromisoformat(daily["sunset"][i]),
        ))
    ti = next((i for i, d in enumerate(days) if d.date == now.date()), 0)

    return Forecast(
        now=now,
        fetched=fetched,
        temp=cur["temperature_2m"],
        feels=cur["apparent_temperature"],
        humidity=int(cur["relative_humidity_2m"]),
        wind=cur["wind_speed_10m"],
        wind_dir=COMPASS[round(cur["wind_direction_10m"] / 45) % 8],
        code=int(cur["weather_code"]),
        is_day=bool(cur.get("is_day", 1)),
        today=days[ti],
        hours=hours,
        days=days[ti + 1: ti + 6],
    )


def demo_data(now: datetime | None = None) -> dict:
    """Plausible fake API response, so the layout can be checked offline."""
    now = now or datetime.now()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    hours = [midnight + timedelta(hours=i) for i in range(24 * 7)]
    temps = [round(11 + 4 * math.sin((h.hour - 9) / 24 * 2 * math.pi) + 1.5 * math.sin(i / 17), 1)
             for i, h in enumerate(hours)]
    rain = [max(0, min(100, round(45 + 45 * math.sin(i / 5.0)))) for i in range(len(hours))]
    days = [midnight.date() + timedelta(days=i) for i in range(7)]
    codes = [61, 2, 80, 3, 0, 95, 45]
    hour_now = now.hour
    return {
        "utc_offset_seconds": 3600,
        "timezone": "Europe/London",
        "current": {
            "time": now.strftime("%Y-%m-%dT%H:%M"),
            "temperature_2m": temps[hour_now],
            "apparent_temperature": temps[hour_now] - 2.3,
            "relative_humidity_2m": 84,
            "weather_code": 61,
            "wind_speed_10m": 13.6,
            "wind_direction_10m": 225,
            "is_day": int(7 <= hour_now < 18),
        },
        "hourly": {
            "time": [h.strftime("%Y-%m-%dT%H:%M") for h in hours],
            "temperature_2m": temps,
            "precipitation_probability": rain,
        },
        "daily": {
            "time": [d.isoformat() for d in days],
            "weather_code": codes,
            "temperature_2m_max": [max(temps[i * 24:(i + 1) * 24]) for i in range(7)],
            "temperature_2m_min": [min(temps[i * 24:(i + 1) * 24]) for i in range(7)],
            "precipitation_probability_max": [80, 10, 60, 25, 0, 70, 15],
            "sunrise": [f"{d.isoformat()}T07:3{i}" for i, d in enumerate(days)],
            "sunset": [f"{d.isoformat()}T17:5{7 - i}" for i, d in enumerate(days)],
        },
    }


# --------------------------------------------------------------------------
# Layout

def deg(t: float) -> str:
    return f"{round(t)}°"


def render(fc: Forecast, place: str, battery: int | None = None, stale: bool = False) -> Image.Image:
    img = Image.new("L", (W * SS, H * SS), WHITE)
    p = Pen(img)
    now = fc.now

    # Header -----------------------------------------------------------------
    p.text(M, 58, place, p.fit(place, 34, 360, bold=True), bold=True)
    p.text(M, 88, f"{now:%A} {now.day} {now:%B}", 19, fill=MID)

    updated = f"Updated {fc.fetched:%H:%M}" + ("  ·  offline" if stale else "")
    p.text(W - M, 58, updated, 16, fill=MID, anchor="rs")
    if battery is not None:
        battery_icon(p, W - M, 82, battery)
        p.text(W - M - 36, 88, f"{battery}%", 16, fill=MID, anchor="rs")

    p.line([(M, 106), (W - M, 106)], width=2, caps=False)

    # Current conditions -------------------------------------------------------
    draw_icon(p, fc.code, M + 92, 212, 180, day=fc.is_day)

    x0 = 248
    p.text(x0 - 6, 236, deg(fc.temp), 116, bold=True)
    desc = WMO.get(fc.code, ("Unknown", ""))[0]
    p.text(x0, 278, desc, p.fit(desc, 30, W - M - x0), fill=BLACK)
    p.text(x0, 310, f"High {deg(fc.today.hi)}   Low {deg(fc.today.lo)}", 20, fill=MID)

    # Stats row ----------------------------------------------------------------
    if now < fc.today.sunrise:
        sun_label, sun_time = "SUNRISE", fc.today.sunrise
    elif now < fc.today.sunset:
        sun_label, sun_time = "SUNSET", fc.today.sunset
    else:
        sun_label = "SUNRISE"
        sun_time = fc.days[0].sunrise if fc.days else fc.today.sunrise

    stats = [
        ("FEELS LIKE", deg(fc.feels)),
        (f"WIND {fc.wind_dir}", f"{round(fc.wind)} mph"),
        ("HUMIDITY", f"{fc.humidity}%"),
        (sun_label, f"{sun_time:%H:%M}"),
    ]
    y_top, y_bot = 336, 414
    p.line([(M, y_top), (W - M, y_top)], width=1, fill=LIGHT, caps=False)
    col = (W - 2 * M) / len(stats)
    for i, (lab, val) in enumerate(stats):
        x = M + i * col + (0 if i == 0 else 14)
        if i:
            p.line([(M + i * col, y_top + 14), (M + i * col, y_bot - 14)], width=1, fill=PALE, caps=False)
        p.label(x, 363, lab)
        p.text(x, 397, val, 26, bold=True)
    p.line([(M, y_bot), (W - M, y_bot)], width=1, fill=LIGHT, caps=False)

    # Next 12 hours ------------------------------------------------------------
    p.label(M, 448, "NEXT 12 HOURS")
    legend = "CHANCE OF RAIN"
    lw = p.label_width(legend, 12)
    p.label(W - M - lw, 448, legend, 12)
    p.rect(W - M - lw - 18, 438, W - M - lw - 7, 448, fill=PALE)

    hrs = fc.hours
    if len(hrs) >= 2:
        cx0, cx1 = M + 16, W - M - 16
        step = (cx1 - cx0) / (len(hrs) - 1)
        base, bar_max = 566, 82
        t_hi_y, t_lo_y = 496, 542

        temps = [h.temp for h in hrs]
        lo, hi = min(temps), max(temps)
        if hi - lo < 4:                       # don't exaggerate tiny swings
            mid = (hi + lo) / 2
            lo, hi = mid - 2, mid + 2

        def ty(t):
            return t_lo_y - (t - lo) / (hi - lo) * (t_lo_y - t_hi_y)

        xs = [cx0 + i * step for i in range(len(hrs))]
        for x, h in zip(xs, hrs):
            bh = bar_max * h.rain / 100
            p.rect(x - 11, base - bh, x + 11, base, fill=PALE, radius=2 if bh > 4 else 0)
        p.line([(cx0 - 12, base), (cx1 + 12, base)], width=1, fill=LIGHT, caps=False)

        pts = [(x, ty(h.temp)) for x, h in zip(xs, hrs)]
        p.line(pts, width=3)
        for i, ((x, y), h) in enumerate(zip(pts, hrs)):
            if i % 2 == 0:
                p.circle(x, y, 6.5, fill=WHITE)
                p.circle(x, y, 4.5, fill=BLACK)
                p.text(x, y - 11, deg(h.temp), 16, bold=True, anchor="ms", halo=3)
                p.text(x, base + 22, "Now" if i == 0 else f"{h.time:%H}", 15,
                       bold=(i == 0), fill=BLACK if i == 0 else MID, anchor="ms")

    # Five-day forecast --------------------------------------------------------
    y_line = 606
    p.line([(M, y_line), (W - M, y_line)], width=2, caps=False)
    if fc.days:
        col = (W - 2 * M) / len(fc.days)
        for i, d in enumerate(fc.days):
            cx = M + col * (i + 0.5)
            if i:
                p.line([(M + i * col, y_line + 18), (M + i * col, H - 30)], width=1, fill=PALE, caps=False)
            p.text(cx, 640, f"{d.date:%a}", 21, bold=True, anchor="ms")
            draw_icon(p, d.code, cx, 688, 62)

            hi_s, lo_s = deg(d.hi), deg(d.lo)
            ts = 22
            while ts > 14 and p.width(hi_s, ts, True) + 10 + p.width(lo_s, ts) > col - 10:
                ts -= 1                       # e.g. "-12° -15°" in a narrow column
            hw, lw_ = p.width(hi_s, ts, bold=True), p.width(lo_s, ts)
            x = cx - (hw + 10 + lw_) / 2
            p.text(x, 744, hi_s, ts, bold=True)
            p.text(x + hw + 10, 744, lo_s, ts, fill=MID)

            rain_s = f"{d.rain}%"
            rw = p.width(rain_s, 16)
            x = cx - (rw + 14) / 2
            droplet(p, x + 4, 765, 13, fill=MID if d.rain >= 30 else LIGHT)
            p.text(x + 14, 771, rain_s, 16, fill=DARK if d.rain >= 30 else MID)

    out = img.resize((W, H), Image.Resampling.LANCZOS)
    return out.point(lambda v: (v + 8) // 17 * 17)      # snap to the panel's 16 greys


def to_png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.convert("L").save(buf, "PNG", optimize=True)    # greyscale, no alpha
    return buf.getvalue()


# --------------------------------------------------------------------------
# Data source with caching, and the HTTP server

class Source:
    def __init__(self, lat: float, lon: float, demo: bool = False):
        self.lat, self.lon, self.demo = lat, lon, demo
        self.lock = threading.Lock()
        self.data: dict | None = None
        self.fetched = 0.0

    def get(self) -> tuple[dict, float, bool]:
        """Return (data, fetched_timestamp, is_stale)."""
        with self.lock:
            age = time.time() - self.fetched
            if self.data is not None and age < CACHE_SECONDS:
                return self.data, self.fetched, False
            try:
                self.data = demo_data() if self.demo else fetch_forecast(self.lat, self.lon)
                self.fetched = time.time()
                return self.data, self.fetched, False
            except Exception as e:  # network down, API error, bad JSON...
                log(f"forecast fetch failed: {e}")
                if self.data is not None and age < STALE_LIMIT:
                    return self.data, self.fetched, True
                raise


def make_png(source: Source, place: str, battery: int | None) -> bytes:
    data, fetched, stale = source.get()
    return to_png(render(parse(data, fetched), place, battery, stale))


def parse_battery(raw: str | None) -> int | None:
    try:
        return max(0, min(100, int(raw)))
    except (TypeError, ValueError):
        return None


def serve(source: Source, place: str, host: str, port: int) -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            url = urlparse(self.path)
            if url.path == "/health":
                return self._send(200, b"ok\n", "text/plain")
            if url.path not in ("/", "/dash.png"):
                return self._send(404, b"not found\n", "text/plain")
            battery = parse_battery(parse_qs(url.query).get("battery", [None])[0])
            try:
                png = make_png(source, place, battery)
            except Exception as e:
                # A non-200 makes kindle-dash keep the image it already has.
                log(f"render failed: {e}")
                return self._send(503, b"forecast unavailable\n", "text/plain")
            self._send(200, png, "image/png")

        def _send(self, status, body, ctype):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):
            log(f"{self.client_address[0]} {fmt % args}")

    server = ThreadingHTTPServer((host, port), Handler)
    log(f"serving {place} dashboard on http://{host}:{port}/dash.png")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


def main() -> None:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--lat", type=float, default=float(os.environ.get("DASH_LAT", 52.7077)))
    common.add_argument("--lon", type=float, default=float(os.environ.get("DASH_LON", -2.7540)))
    common.add_argument("--place", default=os.environ.get("DASH_PLACE", "Shrewsbury"))
    common.add_argument("--demo", action="store_true", help="use built-in sample data (no network)")

    ap = argparse.ArgumentParser(description="Kindle 4 e-ink weather dashboard")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("render", parents=[common], help="render one PNG to a file")
    r.add_argument("-o", "--output", default="dash.png")
    r.add_argument("--battery", type=int, help="battery %% to show in the header")

    s = sub.add_parser("serve", parents=[common], help="serve /dash.png over HTTP")
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=8080)

    args = ap.parse_args()
    source = Source(args.lat, args.lon, demo=args.demo)

    if args.cmd == "render":
        Path(args.output).write_bytes(make_png(source, args.place, parse_battery(args.battery)))
        log(f"wrote {args.output}")
    else:
        serve(source, args.place, args.host, args.port)


if __name__ == "__main__":
    main()

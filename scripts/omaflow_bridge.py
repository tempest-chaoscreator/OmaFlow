#!/usr/bin/env python3
"""OmaFlow bridge: JSON commands on stdin, JSON events on stdout.

Talks to fan2go (chassis + GPU fans), liquidctl (AIO pump/fan/LCD), and
hwmon/nvidia-smi for telemetry. Pillow is used only to draw the tinted
liquid-temp LCD frame.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import stat
import struct
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
import zlib
from collections import deque
from pathlib import Path

# CPU-temperature graphs: 28–98 °C. GPU stays on the 20–90 axis the
# Silent handoff was tuned against. Coolant graphs stop at 60 °C.
OLD_TEMPS = list(range(20, 91, 5))
CPU_TEMPS = list(range(28, 99, 5))
GPU_TEMPS = list(OLD_TEMPS)
LIQUID_TEMPS = list(range(28, 61, 2))
TEMPS = CPU_TEMPS
POINT_COUNT = len(CPU_TEMPS)
FAN_MIN = 20
PUMP_MIN = 50
CHANNELS = ("chassis", "cpu", "gpu", "aio", "pump")
SENSOR_CHANNELS = ("pump", "aio", "cpu")
MODES = ("silent", "static", "performance", "hell", "custom")
CPU_FAN_RE = re.compile(r"cpu[_\s-]?fan|(^|[^a-z])cpu([^a-z]|$)", re.I)
CPU_FAN_SKIP_RE = re.compile(r"opt|pump|water|flow|aio|gpu|chassis", re.I)
HISTORY_LEN = 60
POLL_S = 1.0
APPLY_DEBOUNCE_S = 0.8
FAN2GO_API = "http://127.0.0.1:9001"
CONFIG_DIR = Path.home() / ".config" / "omaflow"
STATE_PATH = CONFIG_DIR / "state.json"
YAML_PATH = CONFIG_DIR / "fan2go.yaml"
FAN2GO_DB = "/var/lib/omaflow/fan2go.db"
FAN2GO_BIN = Path("/usr/bin/fan2go")
LCD_PNG = CONFIG_DIR / "lcd-accent.png"
THEME_COLORS = Path.home() / ".local/state/omarchy/current/theme/colors.toml"
LCD_SIZE = 320
HELPER_INSTALLED = Path("/usr/lib/omaflow/omaflow-helper")

# Skip these hwmon names as chassis PWM targets (AIO / sensors / unused).
AIO_HWMON = {"z53", "z63", "z73", "nzxtkraken3", "kraken3", "liquidctl"}
SKIP_FAN_HWMON = AIO_HWMON | {
    "k10temp", "coretemp", "nvme", "jc42", "asusec", "asus",
    "iwlwifi_1", "hidpp_battery_0", "r8169",
}


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def clamp(v, lo, hi):
    try:
        n = float(v)
    except (TypeError, ValueError):
        n = lo
    return max(lo, min(hi, n))


_which_cache = {}
_which_at = {}


def which(name: str) -> str:
    now = time.time()
    if name in _which_cache and now - _which_at.get(name, 0) < 30:
        return _which_cache[name]
    found = shutil.which(name) or ""
    _which_cache[name] = found
    _which_at[name] = now
    return found


def run(cmd, timeout=8, env=None):
    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        r = subprocess.CompletedProcess(cmd, 1, "", str(exc))
        return r


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def read_int(path: Path):
    raw = read_text(path)
    if raw == "":
        return None
    try:
        return int(float(raw.split()[0]))
    except ValueError:
        return None


def milli_c(path: Path):
    n = read_int(path)
    return None if n is None else n / 1000.0


# --- presets ---------------------------------------------------------------

def _fill(values):
    out = [clamp(v, 0, 100) for v in values]
    if len(out) != POINT_COUNT:
        raise RuntimeError("preset length")
    return out


def _flat(v):
    return _fill([v] * POINT_COUNT)


def _fill_n(values, count):
    out = [int(round(clamp(v, 0, 100))) for v in values]
    if len(out) != count:
        raise RuntimeError("preset length")
    return out


def _flat_n(v, count):
    return _fill_n([v] * count, count)


# Air curves sit on CPU_TEMPS (28–98). GPU stays on GPU_TEMPS (20–90)
# and is filled separately because _fill() is locked to POINT_COUNT.
_AIR = {
    "silent": {
        "chassis": [30, 32, 34, 36, 39, 43, 47, 51, 55, 59, 63, 67, 71, 73, 73],
        # 10 points above chassis at 28 °C, rising to 15 points at 98 °C.
        "aio": [40, 42, 45, 47, 50, 55, 59, 64, 68, 72, 77, 81, 85, 88, 88],
        "pump": [50, 50, 50, 50, 54, 61, 68, 76, 83, 90, 97, 100, 100, 100, 100],
    },
    "static": {
        "chassis": [50] * POINT_COUNT,
        "aio": [50] * POINT_COUNT,
        "pump": [60] * POINT_COUNT,
    },
    "performance": {
        "chassis": [26, 32, 39, 48, 58, 68, 78, 87, 94, 98, 100, 100, 100, 100, 100],
        "aio": [26, 32, 39, 48, 58, 68, 78, 87, 94, 98, 100, 100, 100, 100, 100],
        "pump": [75, 75, 75, 75, 75, 79, 85, 92, 98, 100, 100, 100, 100, 100, 100],
    },
    "hell": {
        "chassis": [50, 56, 62, 69, 77, 85, 92, 98, 100, 100, 100, 100, 100, 100, 100],
        "aio": [50, 56, 62, 69, 77, 85, 92, 98, 100, 100, 100, 100, 100, 100, 100],
        # 75% through 38 °C, then up to 100% by 48 °C.
        "pump": [75, 75, 75, 90, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100],
    },
}

PRESETS = {}
for _mode, _chs in _AIR.items():
    PRESETS[_mode] = {
        "chassis": _fill(_chs["chassis"]),
        "cpu": _fill(_chs["chassis"]),
        "aio": _fill(_chs["aio"]),
        "pump": _fill(_chs["pump"]),
        # 0% below ~30 °C (NVIDIA auto / zero-RPM). Take over at 35 °C
        # around the 3090's ~30% floor so auto does not spin the fans
        # while Silent still says 0%.
        "gpu": _fill({
            "silent": [0, 0, 0, 30, 32, 34, 37, 41, 46, 52, 60, 68, 76, 84, 91],
            "static": [50] * POINT_COUNT,
            "performance": [20, 22, 26, 32, 40, 50, 60, 70, 80, 88, 94, 100, 100, 100, 100],
            "hell": [40, 44, 50, 56, 64, 72, 80, 88, 94, 100, 100, 100, 100, 100, 100],
        }[_mode]),
    }

# Coolant curves are 28–60 °C. Pump keeps the 50% floor.
_LIQ_N = len(LIQUID_TEMPS)
LIQUID_PRESETS = {
    "silent": {
        "pump": _fill_n([50, 50, 50, 50, 50, 50, 50, 55, 60, 65, 70, 75, 80, 85, 90, 95, 100], _LIQ_N),
        "aio": _fill_n([40, 40, 41, 41, 41, 42, 42, 47, 52, 58, 63, 68, 74, 79, 84, 90, 95], _LIQ_N),
        "cpu": _fill_n([30, 30, 30, 30, 30, 30, 30, 35, 40, 45, 50, 55, 60, 65, 70, 75, 80], _LIQ_N),
    },
    "static": {
        "pump": _flat_n(60, _LIQ_N),
        "aio": _flat_n(50, _LIQ_N),
        "cpu": _flat_n(50, _LIQ_N),
    },
    "performance": {
        "pump": _fill_n([75, 75, 75, 75, 75, 78, 81, 84, 88, 91, 94, 97, 100, 100, 100, 100, 100], _LIQ_N),
        "aio": _fill_n([45, 45, 45, 45, 45, 52, 59, 66, 73, 80, 87, 94, 100, 100, 100, 100, 100], _LIQ_N),
        "cpu": _fill_n([45, 45, 45, 45, 45, 52, 59, 66, 73, 80, 87, 94, 100, 100, 100, 100, 100], _LIQ_N),
    },
    "hell": {
        "pump": _fill_n([75, 75, 75, 75, 75, 75, 75, 75, 88, 94, 100, 100, 100, 100, 100, 100, 100], _LIQ_N),
        "aio": _fill_n([60, 60, 60, 60, 60, 70, 80, 90, 100, 100, 100, 100, 100, 100, 100, 100, 100], _LIQ_N),
        "cpu": _fill_n([60, 60, 60, 60, 60, 70, 80, 90, 100, 100, 100, 100, 100, 100, 100, 100, 100], _LIQ_N),
    },
}


def copy_points(points, minimum=0, count=POINT_COUNT):
    src = list(points) if isinstance(points, (list, tuple)) else []
    out = []
    n = int(count) if count else POINT_COUNT
    for i in range(n):
        v = src[i] if i < len(src) else minimum
        out.append(int(round(clamp(v, minimum, 100))))
    return out


def enforce_min(points, minimum):
    return [max(minimum, int(v)) for v in copy_points(points, minimum, len(points) if points else POINT_COUNT)]


def apply_monotonic(points, index, value, minimum=0, count=None):
    """Keep duty non-decreasing with temperature. Raising a point lifts
    every point to its right that would otherwise sit below it; lowering
    a point pulls every point to its left that would sit above it."""
    n = int(count) if count else (len(points) if isinstance(points, (list, tuple)) and len(points) >= 2 else POINT_COUNT)
    pts = copy_points(points, minimum, n)
    if not (0 <= index < n):
        return pts
    v = int(round(clamp(value, minimum, 100)))
    pts[index] = v
    for j in range(index + 1, n):
        if pts[j] < v:
            pts[j] = v
    for j in range(index - 1, -1, -1):
        if pts[j] > v:
            pts[j] = v
    return [max(minimum, int(p)) for p in pts]


def duty_at(points, temp, temps=None):
    axis = list(temps) if temps else list(CPU_TEMPS)
    pts = copy_points(points, 0, len(axis))
    t = float(temp)
    if t <= axis[0]:
        return pts[0]
    if t >= axis[-1]:
        return pts[-1]
    for i in range(len(axis) - 1):
        a, b = axis[i], axis[i + 1]
        if a <= t <= b:
            u = (t - a) / (b - a)
            return pts[i] + (pts[i + 1] - pts[i]) * u
    return pts[-1]


def resample(points, src, dst, minimum=0):
    src_pts = copy_points(points, minimum, len(src))
    return [int(round(clamp(duty_at(src_pts, t, src), minimum, 100))) for t in dst]


def axis_for(channel, sensor):
    if channel == "gpu":
        return GPU_TEMPS
    if channel in SENSOR_CHANNELS and sensor == "liquid":
        return LIQUID_TEMPS
    return CPU_TEMPS


def default_curves():
    curves = {mode: {ch: list(PRESETS[mode][ch]) for ch in CHANNELS} for mode in PRESETS}
    curves["custom"] = {ch: list(PRESETS["performance"][ch]) for ch in CHANNELS}
    return curves


def default_liquid_curves():
    curves = {
        mode: {ch: list(LIQUID_PRESETS[mode][ch]) for ch in SENSOR_CHANNELS}
        for mode in LIQUID_PRESETS
    }
    curves["custom"] = {ch: list(LIQUID_PRESETS["performance"][ch]) for ch in SENSOR_CHANNELS}
    return curves


def default_state():
    return {
        "mode": "silent",
        "locks": {m: True for m in ("silent", "static", "performance", "hell")},
        "presetsLocked": True,
        "gpuControl": False,
        "aioFanControl": False,
        "cpuControl": False,
        "chassisControl": True,
        "pumpControl": True,
        "curves": default_curves(),
        "liquidCurves": default_liquid_curves(),
        "sensors": {"pump": "cpu", "aio": "cpu", "cpu": "cpu"},
        "aioCpuDefault": True,
        "selectedChannel": "chassis",
        "lcdMode": "liquid",
        "lcdBrightness": 80,
        "themeSync": True,
        "accent": "",
        "pumpSensor": "cpu",
        "axisVersion": 2,
    }


# --- hardware discovery ----------------------------------------------------

def hwmon_chips():
    root = Path("/sys/class/hwmon")
    chips = []
    if not root.exists():
        return chips
    for entry in sorted(root.iterdir()):
        name = read_text(entry / "name")
        if not name:
            continue
        temps = []
        fans = []
        for p in sorted(entry.glob("temp*_input")):
            m = re.search(r"temp(\d+)_input", p.name)
            if not m:
                continue
            idx = int(m.group(1))
            label = read_text(entry / f"temp{idx}_label") or f"temp{idx}"
            temps.append({"index": idx, "label": label, "path": str(p)})
        for p in sorted(entry.glob("fan*_input")):
            m = re.search(r"fan(\d+)_input", p.name)
            if not m:
                continue
            idx = int(m.group(1))
            label = read_text(entry / f"fan{idx}_label") or f"fan{idx}"
            fans.append({
                "index": idx,
                "label": label,
                "rpm_path": str(p),
                "pwm_path": str(entry / f"pwm{idx}"),
                "enable_path": str(entry / f"pwm{idx}_enable"),
                "has_pwm": (entry / f"pwm{idx}").exists(),
            })
        chips.append({
            "path": str(entry),
            "name": name,
            "temps": temps,
            "fans": fans,
        })
    return chips


def find_temp(chips, name, label_re=None, index=None):
    for chip in chips:
        if chip["name"] != name:
            continue
        for t in chip["temps"]:
            if index is not None and t["index"] == index:
                return milli_c(Path(t["path"]))
            if label_re and re.search(label_re, t["label"], re.I):
                return milli_c(Path(t["path"]))
        if chip["temps"] and label_re is None and index is None:
            return milli_c(Path(chip["temps"][0]["path"]))
    return None


def nvidia_query():
    bin_path = which("nvidia-smi")
    if not bin_path:
        return None
    r = run([
        bin_path,
        "--query-gpu=name,temperature.gpu,fan.speed,power.draw,utilization.gpu,utilization.memory,clocks.sm,clocks.mem",
        "--format=csv,noheader,nounits",
    ])
    if r.returncode != 0 or not r.stdout.strip():
        return None
    parts = [p.strip() for p in r.stdout.strip().splitlines()[0].split(",")]
    if len(parts) < 8:
        return None

    def num(s):
        try:
            return float(s)
        except ValueError:
            return None

    return {
        "name": parts[0],
        "temp": num(parts[1]),
        "fan": num(parts[2]),
        "power": num(parts[3]),
        "util": num(parts[4]),
        "memUtil": num(parts[5]),
        "smClock": num(parts[6]),
        "memClock": num(parts[7]),
    }


def liquidctl_bin():
    return which("liquidctl")


def liquidctl_cmd(*args, timeout=10):
    bin_path = liquidctl_bin()
    if not bin_path:
        return None
    # Status can use the kernel hwmon path. Writes cannot: pwm sysfs is
    # root-only, so set/initialize go straight to the HID device.
    mutating = any(a in ("set", "initialize") for a in args)
    cmd = [bin_path]
    if mutating:
        cmd.append("--direct-access")
    cmd.extend(args)
    return run(cmd, timeout=timeout)


def parse_liquidctl_status(text: str):
    devices = []
    current = None
    for line in text.splitlines():
        raw = line.rstrip()
        if not raw:
            continue
        if not raw.startswith(" ") and not raw.startswith("├") and not raw.startswith("└") and not raw.startswith("│"):
            current = {"name": raw.strip(), "readings": {}}
            devices.append(current)
            continue
        if current is None:
            continue
        cleaned = re.sub(r"^[│├└─\s]+", "", raw)
        m = re.match(r"(.+?)\s{2,}([-\d.]+)\s+(\S+)", cleaned)
        if not m:
            continue
        key = m.group(1).strip().lower()
        try:
            val = float(m.group(2))
        except ValueError:
            continue
        unit = m.group(3)
        current["readings"][key] = {"value": val, "unit": unit}
    return devices


def aio_from_liquidctl(devices):
    for dev in devices:
        name = (dev.get("name") or "").lower()
        if "kraken" in name or "hydro" in name or "coreliquid" in name or "aio" in name:
            r = dev.get("readings") or {}
            def grab(*keys):
                for k in keys:
                    if k in r:
                        return r[k]["value"]
                return None
            return {
                "name": dev.get("name"),
                "coolant": grab("liquid temperature", "coolant temp", "liquid temp"),
                "pump_rpm": grab("pump speed"),
                "pump_duty": grab("pump duty"),
                "fan_rpm": grab("fan speed"),
                "fan_duty": grab("fan duty"),
            }
    return None


# --- PNG -------------------------------------------------------------------

def parse_hex_rgb(hex_color: str):
    h = (hex_color or "").lstrip("#")
    if len(h) == 8:
        h = h[2:]
    if len(h) < 6:
        return (136, 136, 136)
    try:
        return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    except ValueError:
        return (136, 136, 136)


def read_theme_colors() -> dict:
    out = {}
    if not THEME_COLORS.is_file():
        return out
    try:
        text = THEME_COLORS.read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        m = re.match(r'^(accent|background|foreground)\s*=\s*"?(#?[0-9A-Fa-f]{6})"?', line)
        if m:
            out[m.group(1)] = "#" + m.group(2).lstrip("#").lower()
    return out


def resolve_accent(fallback: str = "") -> str:
    theme = read_theme_colors()
    if theme.get("accent"):
        return theme["accent"]
    h = (fallback or "").strip().lower()
    if re.fullmatch(r"#[0-9a-f]{6}", h):
        return h
    return "#888888"


def _rgb_to_hsv(r, g, b):
    mx, mn = max(r, g, b), min(r, g, b)
    d = mx - mn
    if mx < 1e-6:
        return 0.0, 0.0, 0.0
    s = d / mx
    if d < 1e-6:
        h = 0.0
    elif mx == r:
        h = 60 * (((g - b) / d) % 6)
    elif mx == g:
        h = 60 * ((b - r) / d + 2)
    else:
        h = 60 * ((r - g) / d + 4)
    if h < 0:
        h += 360
    return h, s, mx


def _hsv_to_hex(h, s, v):
    s = max(0.0, min(1.0, s))
    v = max(0.0, min(1.0, v))
    c = v * s
    x = c * (1 - abs((h / 60) % 2 - 1))
    m = v - c
    if h < 60:
        rp, gp, bp = c, x, 0.0
    elif h < 120:
        rp, gp, bp = x, c, 0.0
    elif h < 180:
        rp, gp, bp = 0.0, c, x
    elif h < 240:
        rp, gp, bp = 0.0, x, c
    elif h < 300:
        rp, gp, bp = x, 0.0, c
    else:
        rp, gp, bp = c, 0.0, x
    return "#{:02x}{:02x}{:02x}".format(
        int(round((rp + m) * 255)),
        int(round((gp + m) * 255)),
        int(round((bp + m) * 255)),
    )


def vivid_hex(hex_color: str) -> str:
    """Lift saturation so AIO LEDs don't render a muted accent as white."""
    r, g, b = [c / 255.0 for c in parse_hex_rgb(hex_color)]
    h, s, v = _rgb_to_hsv(r, g, b)
    if s < 0.05:
        return hex_color
    return _hsv_to_hex(h, max(s, 0.68), max(v, 0.82))


def lcd_hex(hex_color: str) -> str:
    """Kraken LCD washes pastel accents toward white. Keep the hue, crank
    saturation, and hold value down so Ristretto stays orange and Osaka
    Jade stays green instead of fog."""
    r, g, b = [c / 255.0 for c in parse_hex_rgb(hex_color)]
    h, s, v = _rgb_to_hsv(r, g, b)
    if s < 0.04:
        return hex_color
    return _hsv_to_hex(h, min(1.0, max(s * 1.45, 0.75)), min(0.84, max(v, 0.72)))


_lcd_font_cache = {}


def _lcd_font(size: int):
    cached = _lcd_font_cache.get(size)
    if cached is not None:
        return cached
    from PIL import ImageFont
    font = None
    for path in (
        "/usr/share/fonts/TTF/JetBrainsMonoNerdFont-Bold.ttf",
        "/usr/share/fonts/TTF/JetBrainsMonoNerdFont-Regular.ttf",
        "/usr/share/fonts/TTF/JetBrainsMono-Bold.ttf",
    ):
        if Path(path).is_file():
            try:
                font = ImageFont.truetype(path, size)
                break
            except Exception:
                continue
    if font is None:
        font = ImageFont.load_default()
    _lcd_font_cache[size] = font
    return font


def render_liquid_png(path: Path, temp, accent: str, background: str, size: int = LCD_SIZE) -> None:
    from PIL import Image, ImageDraw
    bg = parse_hex_rgb("#0a0c0b")
    ac = parse_hex_rgb(accent)
    img = Image.new("RGB", (size, size), bg)
    draw = ImageDraw.Draw(img)
    m = 18
    draw.ellipse([m, m, size - m - 1, size - m - 1], outline=ac, width=14)
    if temp is None or not isinstance(temp, (int, float)):
        number = "—"
    else:
        number = f"{float(temp):.1f}"
    font = _lcd_font(92)
    label_font = _lcd_font(26)
    nb = draw.textbbox((0, 0), number, font=font)
    nw, nh = nb[2] - nb[0], nb[3] - nb[1]
    draw.text(
        ((size - nw) / 2 - nb[0], (size - nh) / 2 - nb[1] - 8),
        number,
        font=font,
        fill=ac,
        stroke_width=3,
        stroke_fill=(0, 0, 0),
    )
    label = "LIQUID  °C"
    lb = draw.textbbox((0, 0), label, font=label_font)
    lw = lb[2] - lb[0]
    draw.text(
        ((size - lw) / 2 - lb[0], size * 0.70),
        label,
        font=label_font,
        fill=ac,
        stroke_width=2,
        stroke_fill=(0, 0, 0),
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, "PNG")
    img.close()


def write_solid_png(path: Path, hex_color: str, size: int = 320) -> None:
    r, g, b = parse_hex_rgb(hex_color)
    raw = b"".join(b"\x00" + bytes([r, g, b]) * size for _ in range(size))

    def chunk(tag: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")
    path.write_bytes(png)


# --- fan2go yaml -----------------------------------------------------------

def steps_yaml(points, temps, minimum=0, indent="        ") -> str:
    lines = []
    pts = copy_points(points, minimum, len(temps))
    for temp, duty in zip(temps, pts):
        lines.append(f"{indent}- {int(temp)}: {int(duty)}%")
    return "\n".join(lines)


def staircase_curve(cid: str, sensor: str, points, temps, hysteresis=6, minimum=0) -> list[str]:
    return [
        f"  - id: {cid}",
        "    staircase:",
        f"      sensor: {sensor}",
        "      hysteresis:",
        f"        down: {int(hysteresis)}",
        "      steps:",
        steps_yaml(points, temps, minimum),
    ]


def fan_common(never_stop: bool, min_pwm: int, max_pwm: int, start_pwm: int, curve: str) -> list[str]:
    return [
        f"    neverStop: {'true' if never_stop else 'false'}",
        f"    minPwm: {int(min_pwm)}",
        f"    startPwm: {int(start_pwm)}",
        f"    maxPwm: {int(max_pwm)}",
        "    pwmMap: identity",
        "    setPwmToGetPwmMap: identity",
        f"    curve: {curve}",
        "    controlAlgorithm:",
        "      direct:",
        "        maxPwmChangePerCycle: 4",
        "    useUnscaledCurveValues: false",
    ]


def is_cpu_fan_label(label: str) -> bool:
    text = label or ""
    if CPU_FAN_SKIP_RE.search(text):
        return False
    return CPU_FAN_RE.search(text) is not None


AIO_PUMP_RE = re.compile(r"aio[_\s-]?pump|pump[_\s-]?fan|(^|[^a-z])pump([^a-z]|$)", re.I)
AIO_PUMP_SKIP_RE = re.compile(r"opt|water|flow|cpu|gpu|chassis", re.I)


def is_aio_pump_label(label: str) -> bool:
    text = label or ""
    if AIO_PUMP_SKIP_RE.search(text):
        return False
    return AIO_PUMP_RE.search(text) is not None


def fan_is_unused(fan: dict) -> bool:
    rpm_path = Path(fan.get("rpm_path") or "")
    enable_path = Path(fan.get("enable_path") or "")
    rpm = read_int(rpm_path) if rpm_path.exists() else None
    enable = read_int(enable_path) if enable_path.exists() else None
    return rpm == 0 and enable == 0


def coolant_platform(chips):
    for chip in chips:
        if chip["name"] in AIO_HWMON and chip.get("temps"):
            return chip["name"], int(chip["temps"][0]["index"])
    return None


def build_fan2go_yaml(state, chips, has_nvidia: bool = False, usb_pump: bool = False) -> str:
    mode = state["mode"]
    curves_state = state["curves"][mode]
    chassis_pts = copy_points(curves_state["chassis"], 0, len(CPU_TEMPS))
    if mode != "silent":
        chassis_pts = [max(FAN_MIN, v) for v in chassis_pts]
    sensors = [
        "  - id: cpu_tctl",
        "    hwmon:",
        "      platform: k10temp",
        "      index: 1",
    ]
    curves = staircase_curve("chassis_curve", "cpu_tctl", chassis_pts, CPU_TEMPS, hysteresis=6)
    cpu_on = state.get("cpuControl") is True
    cpu_sensor = (state.get("sensors") or {}).get("cpu") or "cpu"
    min_pwm = 51 if mode != "silent" else 32
    start_pwm = min_pwm
    chassis_on = state.get("chassisControl", True) is not False
    pump_on = state.get("pumpControl", True) is not False and not usb_pump
    pump_sensor = (state.get("sensors") or {}).get("pump") or state.get("pumpSensor") or "cpu"
    chassis_ids = []
    cpu_ids = []
    pump_ids = []
    for chip in chips:
        name = chip["name"]
        if name in SKIP_FAN_HWMON or not any(c.isalpha() for c in name):
            continue
        if name.startswith("r8169"):
            continue
        for fan in chip["fans"]:
            if not fan["has_pwm"] or fan_is_unused(fan):
                continue
            entry = (name, fan["index"])
            label = fan.get("label") or ""
            if is_cpu_fan_label(label):
                if cpu_on:
                    cpu_ids.append(entry)
            elif is_aio_pump_label(label):
                if pump_on:
                    pump_ids.append(entry)
            elif chassis_on:
                chassis_ids.append(entry)
    coolant = coolant_platform(chips)
    coolant_added = False

    def add_coolant():
        nonlocal coolant_added
        if coolant_added or not coolant:
            return False
        platform, index = coolant
        sensors.append("  - id: coolant")
        sensors.append("    hwmon:")
        sensors.append(f"      platform: {platform}")
        sensors.append(f"      index: {index}")
        coolant_added = True
        return True

    if cpu_ids:
        if cpu_sensor == "liquid" and add_coolant():
            cpu_pts = copy_points(state["liquidCurves"][mode]["cpu"], 0, len(LIQUID_TEMPS))
            curves += staircase_curve("cpu_curve", "coolant", cpu_pts, LIQUID_TEMPS, hysteresis=2)
            cpu_curve_id = "cpu_curve"
        else:
            cpu_pts = copy_points(curves_state.get("cpu") or curves_state["chassis"], 0, len(CPU_TEMPS))
            if mode != "silent":
                cpu_pts = [max(FAN_MIN, v) for v in cpu_pts]
            curves += staircase_curve("cpu_curve", "cpu_tctl", cpu_pts, CPU_TEMPS, hysteresis=6)
            cpu_curve_id = "cpu_curve"
    else:
        cpu_curve_id = "chassis_curve"
    if pump_ids:
        if pump_sensor == "liquid" and add_coolant():
            pump_pts = copy_points(state["liquidCurves"][mode]["pump"], PUMP_MIN, len(LIQUID_TEMPS))
            curves += staircase_curve("pump_curve", "coolant", pump_pts, LIQUID_TEMPS, hysteresis=2, minimum=PUMP_MIN)
        else:
            pump_pts = copy_points(curves_state["pump"], PUMP_MIN, len(CPU_TEMPS))
            curves += staircase_curve("pump_curve", "cpu_tctl", pump_pts, CPU_TEMPS, hysteresis=6, minimum=PUMP_MIN)
    pump_min_pwm = int(round(PUMP_MIN * 255 / 100.0))
    fans = []
    for name, idx in chassis_ids:
        fid = f"{name}_{idx}"
        fans += [
            f"  - id: {fid}",
            "    hwmon:",
            f"      platform: {name}",
            f"      rpmChannel: {idx}",
            f"      pwmChannel: {idx}",
        ] + fan_common(True, min_pwm, 255, start_pwm, "chassis_curve")
    for name, idx in cpu_ids:
        fid = f"{name}_{idx}"
        fans += [
            f"  - id: {fid}",
            "    hwmon:",
            f"      platform: {name}",
            f"      rpmChannel: {idx}",
            f"      pwmChannel: {idx}",
        ] + fan_common(True, min_pwm, 255, start_pwm, cpu_curve_id)
    for name, idx in pump_ids:
        fid = f"{name}_{idx}"
        fans += [
            f"  - id: {fid}",
            "    hwmon:",
            f"      platform: {name}",
            f"      rpmChannel: {idx}",
            f"      pwmChannel: {idx}",
        ] + fan_common(True, pump_min_pwm, 255, pump_min_pwm, "pump_curve")
    body = "\n".join([
        "# Generated by OmaFlow. Overwritten when a mode or curve is applied.",
        f"dbPath: {FAN2GO_DB}",
        "runFanInitializationInParallel: false",
        "tempRollingWindowSize: 12",
        "fanController:",
        "  adjustmentTickRate: 1s",
        "api:",
        "  enabled: true",
        "  host: 127.0.0.1",
        "  port: 9001",
        "sensors:",
        "\n".join(sensors),
        "curves:",
        "\n".join(curves),
        "fans:",
        "\n".join(fans) if fans else "  []",
        "",
    ])
    return body


def fan2go_api(path: str):
    try:
        with urllib.request.urlopen(FAN2GO_API + path, timeout=0.4) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError, OSError):
        return None


# --- helper / apply --------------------------------------------------------

def settings_path(raw: str):
    if not raw:
        return None
    path = Path(str(raw)).expanduser()
    try:
        path = path.resolve()
    except OSError:
        return None
    home = Path.home().resolve()
    if path != home and home not in path.parents:
        return None
    if path.suffix.lower() != ".json":
        return None
    return path


def helper_trusted() -> bool:
    """The root helper only. Never the copy in the writable plugin checkout."""
    try:
        st = HELPER_INSTALLED.lstat()
    except OSError:
        return False
    if not stat.S_ISREG(st.st_mode) or stat.S_ISLNK(st.st_mode):
        return False
    if st.st_uid != 0 or st.st_mode & 0o022:
        return False
    return True


def system_fan2go() -> bool:
    """Packaged binary only. A fan2go earlier on PATH is not the root service."""
    try:
        listed = FAN2GO_BIN.lstat()
        st = FAN2GO_BIN.stat()
    except OSError:
        return False
    if stat.S_ISLNK(listed.st_mode):
        target = os.path.realpath(FAN2GO_BIN)
        if not target.startswith("/usr/"):
            return False
    if not stat.S_ISREG(st.st_mode) or st.st_uid != 0 or st.st_mode & 0o022:
        return False
    return os.access(FAN2GO_BIN, os.X_OK)


def helper_bin():
    return str(HELPER_INSTALLED) if helper_trusted() else ""


def pkexec_helper(*args):
    helper = helper_bin()
    if not helper:
        return False, "helper missing"
    if os.geteuid() == 0:
        cmd = [helper, *args]
    elif os.path.isfile("/usr/bin/pkexec") and os.access("/usr/bin/pkexec", os.X_OK):
        cmd = ["/usr/bin/pkexec", helper, *args]
    else:
        return False, "pkexec missing"
    r = run(cmd, timeout=25)
    ok = r.returncode == 0
    return ok, (r.stdout + r.stderr).strip()


def release_pwm_auto(hwmon_name: str, channel: int) -> bool:
    """Hand a header back to the firmware curve (pwm_enable=2)."""
    root = Path("/sys/class/hwmon")
    enable = None
    if root.exists():
        for entry in root.iterdir():
            if read_text(entry / "name") == hwmon_name:
                path = entry / f"pwm{channel}_enable"
                if path.exists():
                    enable = path
                break
    if enable is not None:
        try:
            enable.write_text("2", encoding="utf-8")
            return True
        except OSError:
            pass
    ok, _ = pkexec_helper("release-pwm", hwmon_name, str(channel))
    return ok


def write_pwm_user_or_helper(hwmon_name: str, channel: int, pwm: int, privileged: bool = False) -> bool:
    root = Path("/sys/class/hwmon")
    target = None
    enable = None
    if root.exists():
        for entry in root.iterdir():
            if read_text(entry / "name") == hwmon_name:
                p = entry / f"pwm{channel}"
                if p.exists():
                    target = p
                    enable = entry / f"pwm{channel}_enable"
                    break
    if target is not None:
        try:
            if enable is not None and enable.exists():
                enable.write_text("1", encoding="utf-8")
            target.write_text(str(int(pwm)), encoding="utf-8")
            return True
        except OSError:
            pass
    if not privileged:
        return False
    ok, _ = pkexec_helper("write-pwm", hwmon_name, str(channel), str(int(pwm)))
    return ok


def set_nvidia_fan(percent: int) -> bool:
    ns = which("nvidia-settings")
    if not ns:
        return False
    pct = int(clamp(percent, 0, 100))
    env = os.environ.copy()
    r = run([ns, "-a", "[gpu:0]/GPUFanControlState=1"], timeout=6, env=env)
    if r.returncode != 0:
        return False
    ok = False
    # Set fans one by one so a missing fan:2/3 does not fail fan:0.
    for i in range(0, 4):
        r = run([ns, "-a", f"[fan:{i}]/GPUTargetFanSpeed={pct}"], timeout=6, env=env)
        if r.returncode == 0:
            ok = True
    return ok


def restore_nvidia_auto() -> bool:
    ns = which("nvidia-settings")
    if not ns:
        return False
    env = os.environ.copy()
    r = run([ns, "-a", "[gpu:0]/GPUFanControlState=0"], timeout=6, env=env)
    return r.returncode == 0


def liquidctl_profile(channel: str, points, temps, minimum: int) -> bool:
    axis = list(temps)
    pts = copy_points(points, minimum, len(axis))
    args = ["--match", "kraken", "set", channel, "speed"]
    for temp, duty in zip(axis, pts):
        args += [str(int(temp)), str(int(duty))]
    r = liquidctl_cmd(*args, timeout=12)
    if r is None:
        return False
    if r.returncode == 0:
        return True
    # Some AIOs reject long profiles; fall back to 7 keypoints.
    last = len(axis) - 1
    key_i = sorted({round(i * last / 6) for i in range(7)})
    args = ["--match", "kraken", "set", channel, "speed"]
    for i in key_i:
        args += [str(int(axis[i])), str(int(pts[i]))]
    r = liquidctl_cmd(*args, timeout=12)
    return r is not None and r.returncode == 0


def liquidctl_fixed_speed(channel: str, percent: int) -> bool:
    pct = int(clamp(percent, 0, 100))
    r = liquidctl_cmd("--match", "kraken", "set", channel, "speed", str(pct), timeout=12)
    return r is not None and r.returncode == 0


def _legacy_v110():
    """1.1.0 factory curves on the old 20–90 axis. Kept so unedited
    curves upgrade before they are moved onto 28–98 / 28–60."""
    n = len(OLD_TEMPS)
    return {
        "silent": {
            "chassis": [29, 29, 31, 33, 35, 37, 41, 45, 49, 53, 57, 61, 65, 69, 73],
            "gpu": [0, 0, 0, 30, 32, 34, 37, 41, 46, 52, 60, 68, 76, 84, 91],
            "aio": [29, 29, 31, 33, 35, 37, 41, 45, 49, 53, 57, 61, 65, 69, 73],
            "pump": [50, 50, 50, 50, 50, 50, 57, 64, 71, 79, 86, 93, 100, 100, 100],
        },
        "static": {
            "chassis": [50] * n,
            "gpu": [50] * n,
            "aio": [50] * n,
            "pump": [60] * n,
        },
        "performance": {
            "chassis": [22, 24, 28, 34, 42, 52, 62, 72, 82, 90, 96, 100, 100, 100, 100],
            "gpu": [20, 22, 26, 32, 40, 50, 60, 70, 80, 88, 94, 100, 100, 100, 100],
            "aio": [22, 24, 28, 34, 42, 52, 62, 72, 82, 90, 96, 100, 100, 100, 100],
            "pump": [75, 75, 75, 75, 75, 75, 75, 81, 88, 94, 100, 100, 100, 100, 100],
        },
        "hell": {
            "chassis": [45, 48, 52, 58, 65, 72, 80, 88, 95, 100, 100, 100, 100, 100, 100],
            "gpu": [40, 44, 50, 56, 64, 72, 80, 88, 94, 100, 100, 100, 100, 100, 100],
            "aio": [45, 48, 52, 58, 65, 72, 80, 88, 95, 100, 100, 100, 100, 100, 100],
            "pump": [75, 75, 75, 75, 75, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100],
        },
    }


def _upgrade_legacy_curve(points, replacements, fallback):
    cur = copy_points(points, 0, len(OLD_TEMPS))
    for old in replacements:
        if cur == copy_points(old, 0, len(OLD_TEMPS)):
            return copy_points(fallback, 0, len(OLD_TEMPS))
    return cur


def migrate_legacy_curves(data, base):
    """Move a 1.x state onto axisVersion 2.

    Chassis and CPU-sensor pump curves were authored against 20–90 and
    move to 28–98, keeping the duty at each temperature. AIO curves were
    uploaded to the cooler as liquid temperatures, so they move to the
    28–60 coolant axis. GPU stays on 20–90.
    """
    v110 = _legacy_v110()
    v110["custom"] = {ch: list(v110["performance"][ch]) for ch in v110["performance"]}
    ancient = {
        "silent": {
            "chassis": [
                [18, 18, 20, 22, 24, 26, 30, 34, 38, 42, 46, 50, 54, 58, 62],
                [26, 26, 28, 30, 32, 34, 38, 42, 46, 50, 54, 58, 62, 66, 70],
            ],
            "gpu": [
                [0, 0, 0, 0, 0, 0, 0, 0, 20, 30, 42, 54, 64, 72, 80],
                [0, 0, 0, 0, 0, 0, 18, 24, 30, 38, 46, 54, 62, 70, 78],
                [0, 0, 0, 0, 0, 0, 0, 0, 28, 38, 50, 62, 72, 80, 88],
                [0, 0, 0, 0, 0, 0, 0, 0, 31, 41, 53, 65, 75, 83, 91],
            ],
            "aio": [
                [18, 18, 20, 22, 24, 26, 30, 34, 38, 42, 46, 50, 54, 58, 62],
                [26, 26, 28, 30, 32, 34, 38, 42, 46, 50, 54, 58, 62, 66, 70],
            ],
            "pump": [
                [50, 50, 50, 50, 50, 52, 54, 56, 58, 60, 62, 64, 66, 68, 70],
                [58, 58, 58, 58, 58, 60, 62, 64, 66, 68, 70, 72, 74, 76, 78],
                [61, 61, 61, 61, 63, 65, 67, 69, 71, 73, 75, 77, 79, 81],
            ],
        },
        "static": {
            "chassis": [[25] * 15, [40] * 15],
            "gpu": [[25] * 15, [40] * 15],
            "aio": [[25] * 15, [40] * 15],
            "pump": [[50] * 15],
        },
        "performance": {
            "pump": [[50, 50, 52, 55, 58, 62, 68, 74, 80, 85, 90, 94, 96, 98, 100]],
        },
        "hell": {
            "pump": [[60, 62, 65, 70, 75, 80, 85, 90, 94, 98, 100, 100, 100, 100, 100]],
        },
    }
    stored = data.get("curves") or {}
    legacy = {}
    for mode in MODES:
        src = stored.get(mode) or {}
        legacy[mode] = {}
        for ch in ("chassis", "gpu", "aio", "pump"):
            fallback = v110[mode][ch]
            raw = src.get(ch, fallback)
            legacy[mode][ch] = _upgrade_legacy_curve(raw, ancient.get(mode, {}).get(ch, []), fallback)
    pump_was_liquid = data.get("pumpSensor") == "liquid"
    for mode in MODES:
        base["curves"][mode]["chassis"] = resample(legacy[mode]["chassis"], OLD_TEMPS, CPU_TEMPS, 0)
        base["curves"][mode]["cpu"] = list(base["curves"][mode]["chassis"])
        base["curves"][mode]["gpu"] = copy_points(legacy[mode]["gpu"], 0, len(GPU_TEMPS))
        base["curves"][mode]["aio"] = list(PRESETS["performance" if mode == "custom" else mode]["aio"])
        if pump_was_liquid:
            base["liquidCurves"][mode]["pump"] = resample(legacy[mode]["pump"], OLD_TEMPS, LIQUID_TEMPS, PUMP_MIN)
            base["curves"][mode]["pump"] = list(PRESETS["performance" if mode == "custom" else mode]["pump"])
        else:
            base["curves"][mode]["pump"] = resample(legacy[mode]["pump"], OLD_TEMPS, CPU_TEMPS, PUMP_MIN)
        base["liquidCurves"][mode]["aio"] = resample(legacy[mode]["aio"], OLD_TEMPS, LIQUID_TEMPS, 0)
    base["sensors"] = {
        "pump": "liquid" if pump_was_liquid else "cpu",
        "aio": "liquid",
        "cpu": "cpu",
    }
    base["pumpSensor"] = base["sensors"]["pump"]
    base["axisVersion"] = 2


def load_current_curves(data, base):
    curves = data.get("curves") or {}
    liquid = data.get("liquidCurves") or {}
    for mode in MODES:
        src = curves.get(mode) or {}
        for ch in CHANNELS:
            temps = GPU_TEMPS if ch == "gpu" else CPU_TEMPS
            minimum = PUMP_MIN if ch == "pump" else 0
            if ch in src:
                base["curves"][mode][ch] = copy_points(src[ch], minimum, len(temps))
        src_l = liquid.get(mode) or {}
        for ch in SENSOR_CHANNELS:
            minimum = PUMP_MIN if ch == "pump" else 0
            if ch in src_l:
                base["liquidCurves"][mode][ch] = copy_points(src_l[ch], minimum, len(LIQUID_TEMPS))
    sensors = data.get("sensors") or {}
    for ch in SENSOR_CHANNELS:
        if sensors.get(ch) in ("cpu", "liquid"):
            base["sensors"][ch] = sensors[ch]
    if data.get("pumpSensor") in ("cpu", "liquid") and "pump" not in sensors:
        base["sensors"]["pump"] = data["pumpSensor"]
    base["pumpSensor"] = base["sensors"]["pump"]
    base["axisVersion"] = 2


_OLD_SILENT_AIO_LIQ = [30, 30, 30, 30, 30, 30, 30, 35, 40, 45, 50, 55, 60, 65, 70, 75, 80]


def upgrade_curves(base) -> bool:
    """Replace an untouched Silent AIO curve with the stronger factory one."""
    changed = False
    if list(base["curves"]["silent"]["aio"]) == [30, 32, 34, 36, 39, 43, 47, 51, 55, 59, 63, 67, 71, 73, 73]:
        base["curves"]["silent"]["aio"] = list(PRESETS["silent"]["aio"])
        changed = True
    if list(base["liquidCurves"]["silent"]["aio"]) == _OLD_SILENT_AIO_LIQ:
        base["liquidCurves"]["silent"]["aio"] = list(LIQUID_PRESETS["silent"]["aio"])
        changed = True
    return changed


class OmaFlow:
    def __init__(self):
        self.lock = threading.RLock()
        self.state = default_state()
        self.chips = []
        self.nvidia = None
        self.aio = None
        self.liquid_devices = []
        self.fans = []
        self.temps = {}
        self.history = {
            "cpu": deque(maxlen=HISTORY_LEN),
            "gpu": deque(maxlen=HISTORY_LEN),
            "coolant": deque(maxlen=HISTORY_LEN),
        }
        self.last_error = ""
        self.apply_error = ""
        self.fan2go_running = False
        self.fan2go_installed = system_fan2go()
        self.liquidctl_installed = bool(liquidctl_bin())
        self.helper_ready = helper_trusted()
        self._apply_timer = None
        self._save_timer = None
        self._stop = threading.Event()
        self._last_yaml = ""
        self._last_lcd_key = None
        self._last_lcd_push = 0.0
        self._lcd_busy = False
        self._last_pwm = {}
        self._last_gpu_duty = None
        self._last_pump_duty = None
        self._last_aio_duty = None
        self._nvidia_manual = False
        self._curve_rev = 0
        self._liquidctl_inited = False
        self._release_cpu_fans = False
        self._usb_pump = False
        self.notice = ""
        self._chips_cached = []
        self._chips_at = 0.0
        self._last_liquidctl_status = 0.0
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        (Path.home() / ".local/share/omaflow").mkdir(parents=True, exist_ok=True)
        self._save_after_load = False
        self.load_state()
        if self._save_after_load:
            self.save_state()

    def load_state(self) -> None:
        if not STATE_PATH.exists():
            return
        try:
            data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        base = default_state()
        if data.get("mode") in MODES:
            base["mode"] = data["mode"]
        locks = data.get("locks") or {}
        for m in ("silent", "static", "performance", "hell"):
            if m in locks:
                base["locks"][m] = bool(locks[m])
        if "presetsLocked" in data:
            base["presetsLocked"] = bool(data["presetsLocked"])
        else:
            base["presetsLocked"] = all(base["locks"].get(m, True) for m in ("silent", "static", "performance", "hell"))
        if "gpuControl" in data:
            base["gpuControl"] = bool(data["gpuControl"])
        if "aioFanControl" in data:
            base["aioFanControl"] = bool(data["aioFanControl"])
        if "cpuControl" in data:
            base["cpuControl"] = bool(data["cpuControl"])
        if "chassisControl" in data:
            base["chassisControl"] = bool(data["chassisControl"])
        if "pumpControl" in data:
            base["pumpControl"] = bool(data["pumpControl"])
        if int(data.get("axisVersion") or 1) >= 2:
            load_current_curves(data, base)
        else:
            migrate_legacy_curves(data, base)
            self._save_after_load = True
        if upgrade_curves(base):
            self._save_after_load = True
        if not data.get("aioCpuDefault"):
            base["sensors"]["aio"] = "cpu"
            base["aioCpuDefault"] = True
            self._save_after_load = True
        else:
            base["aioCpuDefault"] = True
        if data.get("selectedChannel") in CHANNELS:
            base["selectedChannel"] = data["selectedChannel"]
        if data.get("lcdMode") in ("liquid", "accent", "off"):
            base["lcdMode"] = data["lcdMode"]
        if "lcdBrightness" in data:
            base["lcdBrightness"] = int(clamp(data["lcdBrightness"], 0, 100))
        if "themeSync" in data:
            base["themeSync"] = bool(data["themeSync"])
        if data.get("accent"):
            base["accent"] = str(data["accent"])
        self.state = base

    def _save_now(self) -> None:
        with self.lock:
            tmp = STATE_PATH.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self.state, indent=2) + "\n", encoding="utf-8")
            tmp.replace(STATE_PATH)

    def save_state(self, immediate=True) -> None:
        if self._save_timer is not None:
            self._save_timer.cancel()
            self._save_timer = None
        if immediate:
            self._save_now()
            return
        self._save_timer = threading.Timer(0.5, self._save_now)
        self._save_timer.daemon = True
        self._save_timer.start()

    def is_locked(self, mode=None) -> bool:
        m = mode or self.state["mode"]
        if m == "custom":
            return False
        if "presetsLocked" in self.state:
            return self.state.get("presetsLocked", True) is not False
        return self.state["locks"].get(m, True) is not False

    def poll(self) -> None:
        now = time.time()
        if now - self._chips_at > 15 or not self._chips_cached:
            self._chips_cached = hwmon_chips()
            self._chips_at = now
        self.chips = self._chips_cached
        self.fan2go_installed = system_fan2go()
        self.liquidctl_installed = bool(liquidctl_bin())
        self.helper_ready = helper_trusted()
        self.fan2go_running = fan2go_api("/fan") is not None
        self.nvidia = nvidia_query()
        cpu = find_temp(self.chips, "k10temp", label_re=r"Tctl", index=1)
        ccd1 = find_temp(self.chips, "k10temp", label_re=r"Tccd1")
        ccd2 = find_temp(self.chips, "k10temp", label_re=r"Tccd2")
        mb = find_temp(self.chips, "asusec", label_re=r"Motherboard")
        vrm = find_temp(self.chips, "asusec", label_re=r"^VRM$")
        asusec_cpu = find_temp(self.chips, "asusec", label_re=r"^CPU$")
        coolant = find_temp(self.chips, "z53", index=1)
        gpu = self.nvidia["temp"] if self.nvidia else None

        if self.liquidctl_installed and now - self._last_liquidctl_status >= 5:
            st = liquidctl_cmd("status")
            self._last_liquidctl_status = now
            if st and st.returncode == 0:
                self.liquid_devices = parse_liquidctl_status(st.stdout)
                aio = aio_from_liquidctl(self.liquid_devices)
                if aio:
                    self.aio = aio
                    if aio.get("coolant") is not None:
                        coolant = aio["coolant"]
        elif self.aio and self.aio.get("coolant") is not None and coolant is None:
            coolant = self.aio["coolant"]

        self.temps = {
            "cpu": cpu if cpu is not None else asusec_cpu,
            "cpuCcd1": ccd1,
            "cpuCcd2": ccd2,
            "gpu": gpu,
            "coolant": coolant,
            "motherboard": mb,
            "vrm": vrm,
        }
        for key in ("cpu", "gpu", "coolant"):
            v = self.temps.get(key)
            self.history[key].append(None if v is None else round(float(v), 1))
        self.maybe_refresh_lcd()

        fans = []
        for chip in self.chips:
            kind = "aio" if chip["name"] in AIO_HWMON else "chassis"
            if chip["name"] in SKIP_FAN_HWMON and chip["name"] not in AIO_HWMON:
                continue
            for fan in chip["fans"]:
                rpm = read_int(Path(fan["rpm_path"]))
                pwm = read_int(Path(fan["pwm_path"])) if fan["has_pwm"] else None
                duty = None if pwm is None else round(pwm * 100.0 / 255.0, 1)
                label = fan["label"]
                item_kind = kind
                if re.search(r"pump", label, re.I):
                    item_kind = "pump"
                elif kind == "aio" and re.search(r"fan", label, re.I):
                    item_kind = "aio"
                elif kind == "chassis" and fan["has_pwm"] and is_cpu_fan_label(label):
                    item_kind = "cpu"
                elif kind == "chassis" and fan["has_pwm"] and is_aio_pump_label(label):
                    item_kind = "header-pump"
                fans.append({
                    "id": f"{chip['name']}_{fan['index']}",
                    "label": label,
                    "kind": item_kind,
                    "hwmon": chip["name"],
                    "channel": fan["index"],
                    "rpm": rpm,
                    "pwm": pwm,
                    "duty": duty,
                })
        if self.nvidia:
            fans.append({
                "id": "gpu_fan",
                "label": "GPU",
                "kind": "gpu",
                "hwmon": "nvidia",
                "channel": 1,
                "rpm": None,
                "pwm": None,
                "duty": self.nvidia.get("fan"),
            })
        self.fans = fans
        if self.aio:
            self._usb_pump = True

        if not self.fan2go_running:
            self.tick_control()
        else:
            self.tick_gpu()
        self.tick_pump()
        self.tick_aio()

    def sensor_of(self, channel: str) -> str:
        sensors = self.state.get("sensors") or {}
        value = sensors.get(channel)
        if value in ("cpu", "liquid"):
            return value
        if channel == "pump" and self.state.get("pumpSensor") in ("cpu", "liquid"):
            return self.state["pumpSensor"]
        return "cpu"

    def active_curve(self, mode: str, channel: str):
        sensor = self.sensor_of(channel)
        if channel in SENSOR_CHANNELS and sensor == "liquid":
            return self.state["liquidCurves"][mode][channel], LIQUID_TEMPS
        temps = GPU_TEMPS if channel == "gpu" else CPU_TEMPS
        return self.state["curves"][mode][channel], temps

    def tick_gpu(self) -> None:
        if not self.state.get("gpuControl"):
            return
        gpu = self.temps.get("gpu")
        if gpu is None:
            return
        mode = self.state["mode"]
        duty = int(round(duty_at(self.state["curves"][mode]["gpu"], gpu, GPU_TEMPS)))
        last = self._last_gpu_duty
        if last is not None:
            # Stay in auto until the curve reaches the 3090's ~30% floor.
            if last == 0 and duty < 30:
                duty = 0
            elif last is not None and duty > 0 and abs(duty - last) < 4:
                duty = last
        if duty == last:
            return
        # Below the curve's floor, NVIDIA auto is what actually stops the fans.
        if duty < 1:
            if restore_nvidia_auto():
                self._last_gpu_duty = 0
                self._nvidia_manual = False
            return
        if set_nvidia_fan(duty):
            self._last_gpu_duty = duty
            self._nvidia_manual = True

    def _temp_for(self, channel: str):
        sensor = self.sensor_of(channel)
        if channel == "gpu":
            return self.temps.get("gpu")
        if sensor == "liquid":
            return self.temps.get("coolant")
        return self.temps.get("cpu")

    def tick_fixed(self, channel: str, last_attr: str, hysteresis: int, minimum: int) -> bool:
        if not self.liquidctl_installed:
            return False
        if self.sensor_of(channel) != "cpu":
            return True
        temp = self._temp_for(channel)
        if temp is None:
            return False
        points, temps = self.active_curve(self.state["mode"], channel)
        duty = int(round(duty_at(points, temp, temps)))
        duty = max(minimum, min(100, duty))
        last = getattr(self, last_attr)
        if last is not None and abs(duty - last) < hysteresis:
            return True
        liquid_channel = "pump" if channel == "pump" else "fan"
        if liquidctl_fixed_speed(liquid_channel, duty):
            setattr(self, last_attr, duty)
            return True
        return False

    def tick_pump(self) -> bool:
        if self.state.get("pumpControl", True) is False:
            return True
        if not (self._usb_pump or self.aio):
            return True
        if self.sensor_of("pump") != "cpu":
            return True
        return self.tick_fixed("pump", "_last_pump_duty", 3, PUMP_MIN)

    def tick_aio(self) -> bool:
        if not self.state.get("aioFanControl"):
            return True
        if self.sensor_of("aio") != "cpu":
            return True
        return self.tick_fixed("aio", "_last_aio_duty", 3, 0)

    def _write_kind(self, kind: str, duty: float) -> None:
        pwm = int(round(clamp(duty, 0, 100) * 255 / 100.0))
        for fan in self.fans:
            if fan.get("kind") != kind or not fan.get("hwmon") or not fan.get("channel"):
                continue
            key = f"{fan['hwmon']}:{fan['channel']}"
            last = self._last_pwm.get(key)
            if last is not None and abs(pwm - last) < 8:
                continue
            if write_pwm_user_or_helper(fan["hwmon"], fan["channel"], pwm, True):
                self._last_pwm[key] = pwm

    def tick_control(self) -> None:
        mode = self.state["mode"]
        cpu = self.temps.get("cpu")
        if cpu is not None and self.state.get("chassisControl", True) is not False:
            duty = duty_at(self.state["curves"][mode]["chassis"], cpu, CPU_TEMPS)
            if mode != "silent":
                duty = max(FAN_MIN, duty)
            self._write_kind("chassis", duty)
        if self.state.get("cpuControl"):
            points, temps = self.active_curve(mode, "cpu")
            source = self._temp_for("cpu")
            if source is not None:
                cpu_duty = duty_at(points, source, temps)
                if mode != "silent":
                    cpu_duty = max(FAN_MIN, cpu_duty)
                self._write_kind("cpu", cpu_duty)
        if self.state.get("pumpControl", True) is not False and not (self._usb_pump or self.aio):
            points, temps = self.active_curve(mode, "pump")
            source = self._temp_for("pump")
            if source is not None:
                pump_duty = max(PUMP_MIN, duty_at(points, source, temps))
                self._write_kind("header-pump", pump_duty)
        self.tick_gpu()

    def _release_headers(self, usb_pump: bool) -> None:
        """pwm_enable=2 hands a header back to the firmware curve.

        This never writes a duty of 0. Fans the bridge is not driving stay
        on the BIOS curve instead of stopping.
        """
        chassis_on = self.state.get("chassisControl", True) is not False
        cpu_on = self.state.get("cpuControl") is True
        pump_on = self.state.get("pumpControl", True) is not False and not usb_pump
        for fan in self.fans:
            kind = fan.get("kind")
            if not fan.get("hwmon") or not fan.get("channel"):
                continue
            managed = (
                (kind == "chassis" and chassis_on)
                or (kind == "cpu" and cpu_on)
                or (kind == "header-pump" and pump_on)
            )
            if kind in ("chassis", "cpu", "header-pump") and not managed:
                release_pwm_auto(fan["hwmon"], fan["channel"])

    def apply_now(self) -> None:
        mode = self.state["mode"]
        curves = self.state["curves"][mode]
        errors = []

        usb_pump = self._usb_pump or bool(self.aio)
        yaml_text = build_fan2go_yaml(
            self.state,
            self.chips or hwmon_chips(),
            bool(which("nvidia-smi")),
            usb_pump=usb_pump,
        )
        yaml_changed = yaml_text != self._last_yaml
        YAML_PATH.write_text(yaml_text, encoding="utf-8")
        if self.fan2go_installed and yaml_changed:
            ok, msg = pkexec_helper("install-config", yaml_text)
            if not ok:
                errors.append("fan2go config: " + (msg or "failed"))
            else:
                ok, msg = pkexec_helper("restart-fan2go")
                if not ok:
                    errors.append("fan2go restart: " + (msg or "failed"))
                else:
                    self._last_yaml = yaml_text

        if self.liquidctl_installed:
            if not self._liquidctl_inited:
                liquidctl_cmd("initialize", "all")
                self._liquidctl_inited = True
            pump_on = self.state.get("pumpControl", True) is not False
            if pump_on and usb_pump and self.sensor_of("pump") == "liquid":
                pts, temps = self.active_curve(mode, "pump")
                if not liquidctl_profile("pump", pts, temps, PUMP_MIN):
                    errors.append("AIO pump curve failed")
            if self.state.get("aioFanControl") and self.sensor_of("aio") == "liquid":
                pts, temps = self.active_curve(mode, "aio")
                floor = FAN_MIN if mode != "silent" else 0
                if not liquidctl_profile("fan", pts, temps, floor):
                    errors.append("AIO fan curve failed")
            self.apply_lcd()
        if not self.state.get("gpuControl"):
            if self._nvidia_manual:
                restore_nvidia_auto()
                self._nvidia_manual = False
            self._last_gpu_duty = None
        else:
            self._nvidia_manual = True

        if not self.fan2go_running:
            self.tick_control()
        else:
            self.tick_gpu()
        # Mode changes must push the cooler even when the last duty was close.
        # A flat Kraken curve does not follow CPU temp by itself.
        self._last_pump_duty = None
        self._last_aio_duty = None
        if self.state.get("pumpControl", True) is not False and not self.tick_pump():
            errors.append("AIO pump speed failed")
        if self.state.get("aioFanControl") and self.sensor_of("aio") == "cpu" and not self.tick_aio():
            errors.append("AIO fan speed failed")
        self._release_headers(usb_pump)

        self.apply_error = "; ".join(errors)
        if self.apply_error:
            self.last_error = self.apply_error
        self.save_state()

    def apply_lcd(self) -> None:
        if not self.liquidctl_installed:
            return
        mode = self.state["lcdMode"]
        brightness = int(self.state["lcdBrightness"])
        theme = read_theme_colors()
        accent = resolve_accent(self.state.get("accent") or "")
        background = theme.get("background") or "#111111"
        if accent:
            self.state["accent"] = accent
        punch = lcd_hex(accent)
        coolant = None
        if self.temps.get("coolant") is not None:
            coolant = round(float(self.temps["coolant"]), 1)
        elif self.aio and self.aio.get("coolant") is not None:
            coolant = round(float(self.aio["coolant"]), 1)
        key = (mode, bool(self.state.get("themeSync")), punch, brightness, coolant)
        if mode == "off":
            write_solid_png(LCD_PNG, "#000000")
            liquidctl_cmd("--match", "kraken", "set", "lcd", "screen", "static", str(LCD_PNG))
            liquidctl_cmd("--match", "kraken", "set", "lcd", "screen", "brightness", "0")
            self._last_lcd_key = key
            self._last_lcd_push = time.time()
            return
        liquidctl_cmd("--match", "kraken", "set", "lcd", "screen", "brightness", str(brightness))
        if mode == "accent":
            write_solid_png(LCD_PNG, punch, LCD_SIZE)
            liquidctl_cmd("--match", "kraken", "set", "lcd", "screen", "static", str(LCD_PNG), timeout=20)
        elif mode == "liquid" and self.state.get("themeSync"):
            if key == self._last_lcd_key:
                return
            try:
                render_liquid_png(LCD_PNG, coolant, punch, background, LCD_SIZE)
                liquidctl_cmd("--match", "kraken", "set", "lcd", "screen", "static", str(LCD_PNG), timeout=20)
            except Exception:
                log(traceback.format_exc())
                liquidctl_cmd("--match", "kraken", "set", "lcd", "screen", "liquid")
        else:
            liquidctl_cmd("--match", "kraken", "set", "lcd", "screen", "liquid")
        if self.state.get("themeSync") and accent:
            hexcol = vivid_hex(accent).lstrip("#")
            liquidctl_cmd("--match", "kraken", "set", "external", "color", "fixed", hexcol)
        self._last_lcd_key = key
        self._last_lcd_push = time.time()

    def maybe_refresh_lcd(self) -> None:
        if not self.liquidctl_installed:
            return
        if self.state.get("lcdMode") != "liquid" or not self.state.get("themeSync"):
            return
        if time.time() - self._last_lcd_push < 4:
            return
        coolant = self.temps.get("coolant")
        t = None if coolant is None else round(float(coolant), 1)
        accent = resolve_accent(self.state.get("accent") or "")
        punch = lcd_hex(accent)
        key = ("liquid", True, punch, int(self.state.get("lcdBrightness") or 80), t)
        if key == self._last_lcd_key or self._lcd_busy:
            return
        self._lcd_busy = True

        def worker():
            try:
                with self.lock:
                    self.apply_lcd()
            except Exception:
                log(traceback.format_exc())
            finally:
                self._lcd_busy = False

        threading.Thread(target=worker, daemon=True).start()

    def schedule_apply(self) -> None:
        if self._apply_timer is not None:
            self._apply_timer.cancel()

        def fire():
            with self.lock:
                try:
                    self.apply_now()
                except Exception as exc:
                    self.last_error = str(exc)
                    log(traceback.format_exc())
                self.emit_state()

        self._apply_timer = threading.Timer(APPLY_DEBOUNCE_S, fire)
        self._apply_timer.daemon = True
        self._apply_timer.start()

    def snapshot(self, telemetry=False) -> dict:
        gpu = self.nvidia or {}
        hist = {k: list(v) for k, v in self.history.items()}
        needs_setup = not self.helper_ready
        hottest = None
        for key in ("cpu", "gpu", "coolant"):
            v = self.temps.get(key)
            if v is None:
                continue
            if hottest is None or v > hottest:
                hottest = v
        payload = {
            "event": "state",
            "ready": True,
            "needsSetup": needs_setup,
            "fan2go": {
                "installed": self.fan2go_installed,
                "running": self.fan2go_running,
            },
            "liquidctl": {
                "installed": self.liquidctl_installed,
                "hasAio": bool(self.aio),
                "name": (self.aio or {}).get("name") or "",
            },
            "helperReady": self.helper_ready,
            "mode": self.state["mode"],
            "locks": self.state["locks"],
            "presetsLocked": self.state.get("presetsLocked", True) is not False,
            "gpuControl": self.state.get("gpuControl") is True,
            "aioFanControl": self.state.get("aioFanControl") is True,
            "cpuControl": self.state.get("cpuControl") is True,
            "chassisControl": self.state.get("chassisControl", True) is not False,
            "pumpControl": self.state.get("pumpControl", True) is not False,
            "cpuFanPresent": any(f.get("kind") == "cpu" for f in self.fans),
            "pumpHeader": any(f.get("kind") == "header-pump" for f in self.fans),
            "usbPump": self._usb_pump or bool(self.aio),
            "sensors": {
                "pump": self.sensor_of("pump"),
                "aio": self.sensor_of("aio"),
                "cpu": self.sensor_of("cpu"),
            },
            "pumpSensor": self.sensor_of("pump"),
            "notice": self.notice,
            "locked": self.is_locked(),
            "selectedChannel": self.state["selectedChannel"],
            "temps": self.temps,
            "hottest": hottest,
            "gpu": {
                "name": gpu.get("name") or "",
                "util": gpu.get("util"),
                "memUtil": gpu.get("memUtil"),
                "power": gpu.get("power"),
                "fan": gpu.get("fan"),
                "smClock": gpu.get("smClock"),
                "memClock": gpu.get("memClock"),
            },
            "aio": self.aio or {},
            "fans": self.fans,
            "history": hist,
            "lcdMode": self.state["lcdMode"],
            "lcdBrightness": self.state["lcdBrightness"],
            "themeSync": self.state["themeSync"],
            "accent": self.state.get("accent") or "",
            "error": self.last_error,
            "applyError": self.apply_error,
            "curveRev": self._curve_rev,
        }
        if not telemetry:
            payload["curves"] = self.state["curves"]
            payload["liquidCurves"] = self.state.get("liquidCurves") or {}
            payload["tempsAxis"] = CPU_TEMPS
        return payload

    def emit_state(self, telemetry=False) -> None:
        emit(self.snapshot(telemetry=telemetry))

    def _store_curve(self, mode: str, channel: str, points) -> None:
        if channel in SENSOR_CHANNELS and self.sensor_of(channel) == "liquid":
            self.state["liquidCurves"][mode][channel] = list(points)
        else:
            self.state["curves"][mode][channel] = list(points)

    def _set_sensor(self, channel: str, sensor: str) -> None:
        if channel not in SENSOR_CHANNELS or sensor not in ("cpu", "liquid"):
            return
        self.state.setdefault("sensors", {})[channel] = sensor
        if channel == "pump":
            self.state["pumpSensor"] = sensor
            self._last_pump_duty = None
        if channel == "aio":
            self._last_aio_duty = None
        self.save_state()
        self.schedule_apply()
        self.emit_state()

    def _export_settings(self, raw_path: str) -> None:
        path = settings_path(raw_path)
        if path is None:
            self.notice = ""
            self.last_error = "export path must be a .json file inside your home directory"
            self.emit_state()
            return
        doc = {
            "format": "omaflow-settings",
            "version": 1,
            "mode": self.state["mode"],
            "presetsLocked": self.state.get("presetsLocked", True) is not False,
            "locks": self.state.get("locks") or {},
            "gpuControl": self.state.get("gpuControl") is True,
            "aioFanControl": self.state.get("aioFanControl") is True,
            "cpuControl": self.state.get("cpuControl") is True,
            "chassisControl": self.state.get("chassisControl", True) is not False,
            "pumpControl": self.state.get("pumpControl", True) is not False,
            "sensors": {
                "pump": self.sensor_of("pump"),
                "aio": self.sensor_of("aio"),
                "cpu": self.sensor_of("cpu"),
            },
            "curves": self.state.get("curves") or {},
            "liquidCurves": self.state.get("liquidCurves") or {},
            "lcdMode": self.state.get("lcdMode"),
            "lcdBrightness": self.state.get("lcdBrightness"),
            "themeSync": self.state.get("themeSync") is True,
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
        except OSError as exc:
            self.notice = ""
            self.last_error = "export failed: " + str(exc)
            self.emit_state()
            return
        self.last_error = ""
        self.notice = "Exported settings to " + str(path)
        self.emit_state()

    def _import_settings(self, raw_path: str) -> None:
        path = settings_path(raw_path)
        if path is None or not path.is_file():
            self.notice = ""
            self.last_error = "import path must be an existing .json file inside your home directory"
            self.emit_state()
            return
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            self.notice = ""
            self.last_error = "import failed: " + str(exc)
            self.emit_state()
            return
        if not isinstance(doc, dict) or doc.get("format") != "omaflow-settings":
            self.notice = ""
            self.last_error = "that file is not an OmaFlow settings export"
            self.emit_state()
            return
        base = default_state()
        base["accent"] = self.state.get("accent") or ""
        base["selectedChannel"] = self.state.get("selectedChannel") or "chassis"
        doc["axisVersion"] = 2
        load_current_curves(doc, base)
        for key in ("gpuControl", "aioFanControl", "cpuControl", "chassisControl", "pumpControl", "themeSync"):
            if key in doc:
                base[key] = bool(doc[key])
        if "presetsLocked" in doc:
            base["presetsLocked"] = bool(doc["presetsLocked"])
            for m in ("silent", "static", "performance", "hell"):
                base["locks"][m] = base["presetsLocked"]
        if doc.get("mode") in MODES:
            base["mode"] = doc["mode"]
        if doc.get("lcdMode") in ("liquid", "accent", "off"):
            base["lcdMode"] = doc["lcdMode"]
        if "lcdBrightness" in doc:
            base["lcdBrightness"] = int(clamp(doc["lcdBrightness"], 0, 100))
        self.state = base
        self._last_pump_duty = None
        self._last_aio_duty = None
        self._last_yaml = ""
        self.notice = "Imported settings from " + path.name
        self.last_error = ""
        self._curve_rev += 1
        self.save_state()
        self.schedule_apply()
        self.emit_state()

    def handle(self, msg: dict) -> None:
        op = msg.get("op")
        if op == "quit":
            self._stop.set()
            return
        if op == "refresh":
            self.poll()
            self.emit_state()
            return
        if op == "set_mode":
            mode = str(msg.get("mode") or "")
            if mode in MODES:
                self.state["mode"] = mode
                self.save_state()
                self.schedule_apply()
                self.emit_state()
            return
        if op == "set_lock":
            mode = str(msg.get("mode") or "")
            locked_flag = bool(msg.get("locked", True))
            if mode in ("silent", "static", "performance", "hell"):
                self.state["locks"][mode] = locked_flag
            self.state["presetsLocked"] = locked_flag
            for m in ("silent", "static", "performance", "hell"):
                self.state["locks"][m] = locked_flag
            self.save_state()
            self.emit_state()
            return
        if op == "set_presets_locked":
            locked_flag = bool(msg.get("locked", True))
            self.state["presetsLocked"] = locked_flag
            for m in ("silent", "static", "performance", "hell"):
                self.state["locks"][m] = locked_flag
            self.save_state()
            self.emit_state()
            return
        if op == "set_gpu_control":
            self.state["gpuControl"] = bool(msg.get("enabled", False))
            self.save_state()
            self.schedule_apply()
            self.emit_state()
            return
        if op == "set_cpu_control":
            self.state["cpuControl"] = bool(msg.get("enabled", False))
            self.save_state()
            self.schedule_apply()
            self.emit_state()
            return
        if op == "set_chassis_control":
            self.state["chassisControl"] = bool(msg.get("enabled", True))
            self.save_state()
            self.schedule_apply()
            self.emit_state()
            return
        if op == "set_pump_control":
            self.state["pumpControl"] = bool(msg.get("enabled", True))
            self._last_pump_duty = None
            self.save_state()
            self.schedule_apply()
            self.emit_state()
            return
        if op == "set_pump_sensor":
            self._set_sensor(str(msg.get("channel") or "pump"), str(msg.get("sensor") or ""))
            return
        if op == "set_aio_fan_control":
            self.state["aioFanControl"] = bool(msg.get("enabled", False))
            self._last_aio_duty = None
            self.save_state()
            self.schedule_apply()
            self.emit_state()
            return
        if op == "set_channel":
            ch = str(msg.get("channel") or "")
            if ch in CHANNELS:
                self.state["selectedChannel"] = ch
                self.save_state()
                self.emit_state()
            return
        if op == "set_point":
            mode = str(msg.get("mode") or self.state["mode"])
            ch = str(msg.get("channel") or self.state["selectedChannel"])
            idx = int(msg.get("index", -1))
            if mode == "custom" or not self.is_locked(mode):
                if mode in MODES and ch in CHANNELS:
                    points, temps = self.active_curve(mode, ch)
                    if 0 <= idx < len(temps):
                        minimum = PUMP_MIN if ch == "pump" else 0
                        pts = apply_monotonic(points, idx, msg.get("value"), minimum, len(temps))
                        if ch == "pump":
                            pts = [max(PUMP_MIN, int(v)) for v in pts]
                        self._store_curve(mode, ch, pts)
                        self._curve_rev += 1
                        self.save_state(immediate=False)
                        self.emit_state()
            return
        if op == "apply":
            self.apply_now()
            self.emit_state()
            return
        if op == "reset_curve":
            mode = str(msg.get("mode") or self.state["mode"])
            ch = str(msg.get("channel") or self.state["selectedChannel"])
            if mode == "custom":
                src_mode = "performance"
            else:
                src_mode = mode
            if mode in MODES and ch in CHANNELS and src_mode in PRESETS:
                if mode == "custom" or not self.is_locked(mode):
                    minimum = PUMP_MIN if ch == "pump" else 0
                    if ch in SENSOR_CHANNELS and self.sensor_of(ch) == "liquid":
                        pts = copy_points(LIQUID_PRESETS[src_mode][ch], minimum, len(LIQUID_TEMPS))
                    else:
                        temps = GPU_TEMPS if ch == "gpu" else CPU_TEMPS
                        pts = copy_points(PRESETS[src_mode][ch], minimum, len(temps))
                    self._store_curve(mode, ch, pts)
                    self._curve_rev += 1
                    self.save_state()
                    self.schedule_apply()
                    self.emit_state()
            return
        if op == "export_settings":
            self._export_settings(str(msg.get("path") or ""))
            return
        if op == "import_settings":
            self._import_settings(str(msg.get("path") or ""))
            return
        if op == "set_lcd":
            mode = str(msg.get("mode") or "")
            if mode in ("liquid", "accent", "off"):
                self.state["lcdMode"] = mode
            if "brightness" in msg:
                self.state["lcdBrightness"] = int(clamp(msg.get("brightness"), 0, 100))
            self.save_state()
            self.apply_lcd()
            self.emit_state()
            return
        if op == "set_theme_sync":
            self.state["themeSync"] = bool(msg.get("enabled", True))
            self.save_state()
            self.apply_lcd()
            self.emit_state()
            return
        if op == "set_accent":
            acc = str(msg.get("hex") or "")
            if acc:
                if not acc.startswith("#"):
                    acc = "#" + acc
                self.state["accent"] = acc.lower()
                self.save_state()
                if self.state.get("themeSync") and self.state.get("lcdMode") in ("accent", "liquid"):
                    self.apply_lcd()
            return
        if op == "setup_status":
            self.emit_state()
            return


def stdin_loop(app: OmaFlow) -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        with app.lock:
            try:
                app.handle(msg)
            except Exception:
                log(traceback.format_exc())
        if app._stop.is_set():
            break


def main() -> None:
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    app = OmaFlow()
    emit({
        "event": "hello",
        "fan2go": str(FAN2GO_BIN) if system_fan2go() else None,
        "liquidctl": liquidctl_bin(),
        "helper": helper_bin(),
    })
    with app.lock:
        try:
            app.poll()
            if not app.state.get("gpuControl"):
                existing = YAML_PATH.read_text(encoding="utf-8") if YAML_PATH.exists() else ""
                if "gpu_fan" in existing:
                    app.schedule_apply()
            if app.liquidctl_installed and app.state.get("pumpControl", True) is not False:
                usb_pump = app._usb_pump or bool(app.aio)
                if usb_pump and app.sensor_of("pump") == "liquid":
                    pts, temps = app.active_curve(app.state["mode"], "pump")
                    if liquidctl_profile("pump", pts, temps, PUMP_MIN):
                        app.apply_error = ""
                elif usb_pump:
                    app.tick_pump()
                    app.apply_error = ""
            app.emit_state()
        except Exception:
            log(traceback.format_exc())
            emit({"event": "state", "ready": False, "error": "poll failed", "needsSetup": True})

    t = threading.Thread(target=stdin_loop, args=(app,), daemon=True)
    t.start()
    while not app._stop.is_set():
        time.sleep(POLL_S)
        with app.lock:
            try:
                app.poll()
                app.emit_state(telemetry=True)
            except Exception:
                log(traceback.format_exc())


if __name__ == "__main__":
    main()

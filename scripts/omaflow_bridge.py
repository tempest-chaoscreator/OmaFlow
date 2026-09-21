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

TEMPS = list(range(20, 91, 5))  # 15 points, 20–90 °C
POINT_COUNT = len(TEMPS)
FAN_MIN = 20
PUMP_MIN = 50
CHANNELS = ("chassis", "gpu", "aio", "pump")
MODES = ("silent", "static", "performance", "hell", "custom")
HISTORY_LEN = 60
POLL_S = 1.0
APPLY_DEBOUNCE_S = 0.8
FAN2GO_API = "http://127.0.0.1:9001"
CONFIG_DIR = Path.home() / ".config" / "omaflow"
STATE_PATH = CONFIG_DIR / "state.json"
YAML_PATH = CONFIG_DIR / "fan2go.yaml"
LCD_PNG = CONFIG_DIR / "lcd-accent.png"
THEME_COLORS = Path.home() / ".local/state/omarchy/current/theme/colors.toml"
LCD_SIZE = 320
HELPER_INSTALLED = Path("/usr/local/lib/omaflow/omaflow-helper")

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


PRESETS = {
    "silent": {
        "chassis": _fill([29, 29, 31, 33, 35, 37, 41, 45, 49, 53, 57, 61, 65, 69, 73]),
        # 0% below ~30 °C (NVIDIA auto / zero-RPM). Take over at 35 °C
        # around the 3090's ~30% floor so auto does not spin the fans
        # while Silent still says 0%.
        "gpu": _fill([0, 0, 0, 30, 32, 34, 37, 41, 46, 52, 60, 68, 76, 84, 91]),
        "aio": _fill([29, 29, 31, 33, 35, 37, 41, 45, 49, 53, 57, 61, 65, 69, 73]),
        "pump": _fill([50, 50, 50, 50, 50, 50, 57, 64, 71, 79, 86, 93, 100, 100, 100]),
    },
    "static": {
        "chassis": _flat(50),
        "gpu": _flat(50),
        "aio": _flat(50),
        "pump": _flat(60),
    },
    "performance": {
        "chassis": _fill([22, 24, 28, 34, 42, 52, 62, 72, 82, 90, 96, 100, 100, 100, 100]),
        "gpu": _fill([20, 22, 26, 32, 40, 50, 60, 70, 80, 88, 94, 100, 100, 100, 100]),
        "aio": _fill([22, 24, 28, 34, 42, 52, 62, 72, 82, 90, 96, 100, 100, 100, 100]),
        "pump": _fill([75, 75, 75, 75, 75, 75, 75, 81, 88, 94, 100, 100, 100, 100, 100]),
    },
    "hell": {
        "chassis": _fill([45, 48, 52, 58, 65, 72, 80, 88, 95, 100, 100, 100, 100, 100, 100]),
        "gpu": _fill([40, 44, 50, 56, 64, 72, 80, 88, 94, 100, 100, 100, 100, 100, 100]),
        "aio": _fill([45, 48, 52, 58, 65, 72, 80, 88, 95, 100, 100, 100, 100, 100, 100]),
        # 75% until 40 °C, then one step to 100% at 45 °C.
        "pump": _fill([75, 75, 75, 75, 75, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100]),
    },
}


def copy_points(points, minimum=0):
    src = list(points) if isinstance(points, (list, tuple)) else []
    out = []
    for i in range(POINT_COUNT):
        v = src[i] if i < len(src) else minimum
        out.append(int(round(clamp(v, minimum, 100))))
    return out


def enforce_min(points, minimum):
    return [max(minimum, int(v)) for v in copy_points(points, minimum)]


def apply_monotonic(points, index, value, minimum=0):
    """Keep duty non-decreasing with temperature. Raising a point lifts
    every point to its right that would otherwise sit below it; lowering
    a point pulls every point to its left that would sit above it."""
    pts = copy_points(points, minimum)
    if not (0 <= index < POINT_COUNT):
        return pts
    v = int(round(clamp(value, minimum, 100)))
    pts[index] = v
    for j in range(index + 1, POINT_COUNT):
        if pts[j] < v:
            pts[j] = v
    for j in range(index - 1, -1, -1):
        if pts[j] > v:
            pts[j] = v
    return enforce_min(pts, minimum)


def duty_at(points, temp):
    pts = copy_points(points)
    t = clamp(temp, TEMPS[0], TEMPS[-1])
    if t <= TEMPS[0]:
        return pts[0]
    if t >= TEMPS[-1]:
        return pts[-1]
    for i in range(POINT_COUNT - 1):
        a, b = TEMPS[i], TEMPS[i + 1]
        if a <= t <= b:
            u = (t - a) / (b - a)
            return pts[i] + (pts[i + 1] - pts[i]) * u
    return pts[-1]


def default_curves():
    curves = {mode: {ch: list(PRESETS[mode][ch]) for ch in CHANNELS} for mode in PRESETS}
    curves["custom"] = {ch: list(PRESETS["performance"][ch]) for ch in CHANNELS}
    return curves


def default_state():
    return {
        "mode": "silent",
        "locks": {m: True for m in ("silent", "static", "performance", "hell")},
        "presetsLocked": True,
        "gpuControl": False,
        "aioFanControl": False,
        "curves": default_curves(),
        "selectedChannel": "chassis",
        "lcdMode": "liquid",
        "lcdBrightness": 80,
        "themeSync": True,
        "accent": "",
        "pumpSensor": "cpu",
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

def steps_yaml(points, indent="        ") -> str:
    lines = []
    for temp, duty in zip(TEMPS, copy_points(points)):
        lines.append(f"{indent}- {temp}: {int(duty)}%")
    return "\n".join(lines)


def staircase_curve(cid: str, sensor: str, points, hysteresis=6) -> list[str]:
    return [
        f"  - id: {cid}",
        "    staircase:",
        f"      sensor: {sensor}",
        "      hysteresis:",
        f"        down: {int(hysteresis)}",
        "      steps:",
        steps_yaml(points),
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


def fan_is_unused(fan: dict) -> bool:
    rpm_path = Path(fan.get("rpm_path") or "")
    enable_path = Path(fan.get("enable_path") or "")
    rpm = read_int(rpm_path) if rpm_path.exists() else None
    enable = read_int(enable_path) if enable_path.exists() else None
    return rpm == 0 and enable == 0


def build_fan2go_yaml(state, chips, has_nvidia: bool = False) -> str:
    mode = state["mode"]
    chassis_pts = copy_points(state["curves"][mode]["chassis"])
    if mode != "silent":
        chassis_pts = enforce_min(chassis_pts, FAN_MIN)
    sensors = [
        "  - id: cpu_tctl",
        "    hwmon:",
        "      platform: k10temp",
        "      index: 1",
    ]
    curves = staircase_curve("chassis_curve", "cpu_tctl", chassis_pts, hysteresis=6)
    min_pwm = 51 if mode != "silent" else 32
    start_pwm = min_pwm
    fans = []
    for chip in chips:
        name = chip["name"]
        if name in SKIP_FAN_HWMON or not any(c.isalpha() for c in name):
            continue
        if name.startswith("r8169"):
            continue
        for fan in chip["fans"]:
            if not fan["has_pwm"] or fan_is_unused(fan):
                continue
            idx = fan["index"]
            fid = f"{name}_{idx}"
            fans += [
                f"  - id: {fid}",
                "    hwmon:",
                f"      platform: {name}",
                f"      rpmChannel: {idx}",
                f"      pwmChannel: {idx}",
            ] + fan_common(True, min_pwm, 255, start_pwm, "chassis_curve")
    body = "\n".join([
        "# Generated by OmaFlow. Overwritten when a mode or curve is applied.",
        f"dbPath: {Path.home() / '.local/share/omaflow/fan2go.db'}",
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

def helper_bin():
    if HELPER_INSTALLED.is_file():
        return str(HELPER_INSTALLED)
    here = Path(__file__).resolve().parent / "omaflow_helper.py"
    return str(here) if here.is_file() else ""


def pkexec_helper(*args):
    helper = helper_bin()
    if not helper:
        return False, "helper missing"
    pkexec = which("pkexec")
    cmd = [pkexec, helper, *args] if pkexec and os.geteuid() != 0 else [helper, *args]
    # Direct python if the installed helper is the .py in the plugin.
    if helper.endswith(".py") and os.geteuid() != 0 and pkexec:
        cmd = [pkexec, sys.executable, helper, *args]
    r = run(cmd, timeout=25)
    ok = r.returncode == 0
    return ok, (r.stdout + r.stderr).strip()


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


def liquidctl_profile(channel: str, points, minimum: int) -> bool:
    pts = enforce_min(points, minimum)
    args = ["--match", "kraken", "set", channel, "speed"]
    for temp, duty in zip(TEMPS, pts):
        args += [str(int(temp)), str(int(duty))]
    r = liquidctl_cmd(*args, timeout=12)
    if r is None:
        return False
    if r.returncode == 0:
        return True
    # Some AIOs reject long profiles; fall back to 7 keypoints.
    key_i = [0, 2, 4, 6, 8, 11, 14]
    args = ["--match", "kraken", "set", channel, "speed"]
    for i in key_i:
        args += [str(TEMPS[i]), str(int(pts[i]))]
    r = liquidctl_cmd(*args, timeout=12)
    return r is not None and r.returncode == 0


def liquidctl_fixed_speed(channel: str, percent: int) -> bool:
    pct = int(clamp(percent, 0, 100))
    r = liquidctl_cmd("--match", "kraken", "set", channel, "speed", str(pct), timeout=12)
    return r is not None and r.returncode == 0


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
        self.fan2go_installed = bool(which("fan2go"))
        self.liquidctl_installed = bool(liquidctl_bin())
        self.helper_ready = HELPER_INSTALLED.is_file()
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
        self._nvidia_manual = False
        self._curve_rev = 0
        self._liquidctl_inited = False
        self._chips_cached = []
        self._chips_at = 0.0
        self._last_liquidctl_status = 0.0
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        (Path.home() / ".local/share/omaflow").mkdir(parents=True, exist_ok=True)
        self.load_state()

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
        curves = data.get("curves") or {}
        for mode in MODES:
            src = curves.get(mode) or {}
            for ch in CHANNELS:
                minimum = PUMP_MIN if ch == "pump" else 0
                if ch in src:
                    base["curves"][mode][ch] = copy_points(src[ch], minimum)
                    if ch == "pump":
                        base["curves"][mode][ch] = enforce_min(base["curves"][mode][ch], PUMP_MIN)
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
        if data.get("pumpSensor") in ("cpu", "liquid"):
            base["pumpSensor"] = data["pumpSensor"]
        # Replace factory curves the user has not edited.
        old_factory = {
            "silent": {
                "chassis": [18, 18, 20, 22, 24, 26, 30, 34, 38, 42, 46, 50, 54, 58, 62],
                "gpu": [0, 0, 0, 0, 0, 0, 0, 0, 20, 30, 42, 54, 64, 72, 80],
                "aio": [18, 18, 20, 22, 24, 26, 30, 34, 38, 42, 46, 50, 54, 58, 62],
                "pump": [50, 50, 50, 50, 50, 52, 54, 56, 58, 60, 62, 64, 66, 68, 70],
            },
            "static": {
                "chassis": [25] * POINT_COUNT,
                "gpu": [25] * POINT_COUNT,
                "aio": [25] * POINT_COUNT,
            },
        }
        old_silent_gpu_v1 = [0, 0, 0, 0, 0, 0, 18, 24, 30, 38, 46, 54, 62, 70, 78]
        for mode, channels in old_factory.items():
            for ch, old in channels.items():
                if copy_points(base["curves"][mode][ch]) == copy_points(old):
                    base["curves"][mode][ch] = list(PRESETS[mode][ch])
        if copy_points(base["curves"]["silent"]["gpu"]) == copy_points(old_silent_gpu_v1):
            base["curves"]["silent"]["gpu"] = list(PRESETS["silent"]["gpu"])
        old_silent_v2 = {
            "chassis": [26, 26, 28, 30, 32, 34, 38, 42, 46, 50, 54, 58, 62, 66, 70],
            "gpu": [0, 0, 0, 0, 0, 0, 0, 0, 28, 38, 50, 62, 72, 80, 88],
            "aio": [26, 26, 28, 30, 32, 34, 38, 42, 46, 50, 54, 58, 62, 66, 70],
            "pump": [58, 58, 58, 58, 58, 60, 62, 64, 66, 68, 70, 72, 74, 76, 78],
        }
        for ch, old in old_silent_v2.items():
            if copy_points(base["curves"]["silent"][ch]) == copy_points(old):
                base["curves"]["silent"][ch] = list(PRESETS["silent"][ch])
        old_silent_gpu_v3 = [0, 0, 0, 0, 0, 0, 0, 0, 31, 41, 53, 65, 75, 83, 91]
        if copy_points(base["curves"]["silent"]["gpu"]) == copy_points(old_silent_gpu_v3):
            base["curves"]["silent"]["gpu"] = list(PRESETS["silent"]["gpu"])
        old_pumps = {
            "silent": [
                [50, 50, 50, 50, 50, 52, 54, 56, 58, 60, 62, 64, 66, 68, 70],
                [58, 58, 58, 58, 58, 60, 62, 64, 66, 68, 70, 72, 74, 76, 78],
                [61, 61, 61, 61, 61, 63, 65, 67, 69, 71, 73, 75, 77, 79, 81],
            ],
            "static": [[50] * POINT_COUNT],
            "performance": [[50, 50, 52, 55, 58, 62, 68, 74, 80, 85, 90, 94, 96, 98, 100]],
            "hell": [[60, 62, 65, 70, 75, 80, 85, 90, 94, 98, 100, 100, 100, 100, 100]],
        }
        for mode, olds in old_pumps.items():
            cur = copy_points(base["curves"][mode]["pump"], PUMP_MIN)
            if any(cur == copy_points(old, PUMP_MIN) for old in olds):
                base["curves"][mode]["pump"] = list(PRESETS[mode]["pump"])
        if copy_points(base["curves"]["custom"]["pump"], PUMP_MIN) == copy_points(old_pumps["performance"][0], PUMP_MIN):
            base["curves"]["custom"]["pump"] = list(PRESETS["performance"]["pump"])
        for ch in ("chassis", "gpu", "aio"):
            if copy_points(base["curves"]["static"][ch]) in ([25] * POINT_COUNT, [40] * POINT_COUNT):
                base["curves"]["static"][ch] = list(PRESETS["static"][ch])
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
        self.fan2go_installed = bool(which("fan2go"))
        self.liquidctl_installed = bool(liquidctl_bin())
        self.helper_ready = HELPER_INSTALLED.is_file()
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

        if not self.fan2go_running:
            self.tick_control()
        else:
            self.tick_gpu()
        self.tick_pump()

    def tick_gpu(self) -> None:
        if not self.state.get("gpuControl"):
            return
        gpu = self.temps.get("gpu")
        if gpu is None:
            return
        mode = self.state["mode"]
        duty = int(round(duty_at(self.state["curves"][mode]["gpu"], gpu)))
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

    def tick_pump(self) -> None:
        if not self.liquidctl_installed:
            return
        if self.state.get("pumpSensor", "cpu") != "cpu":
            return
        cpu = self.temps.get("cpu")
        if cpu is None:
            return
        mode = self.state["mode"]
        duty = int(round(duty_at(self.state["curves"][mode]["pump"], cpu)))
        duty = max(PUMP_MIN, min(100, duty))
        last = self._last_pump_duty
        if last is not None and abs(duty - last) < 3:
            return
        if liquidctl_fixed_speed("pump", duty):
            self._last_pump_duty = duty

    def tick_control(self) -> None:
        mode = self.state["mode"]
        curves = self.state["curves"][mode]
        cpu = self.temps.get("cpu")
        if cpu is not None:
            duty = duty_at(curves["chassis"], cpu)
            if mode != "silent":
                duty = max(FAN_MIN, duty)
            pwm = int(round(duty * 255 / 100.0))
            for fan in self.fans:
                if fan["kind"] == "chassis" and fan.get("hwmon") and fan.get("channel"):
                    key = f"{fan['hwmon']}:{fan['channel']}"
                    last = self._last_pwm.get(key)
                    if last is not None and abs(pwm - last) < 8:
                        continue
                    if write_pwm_user_or_helper(fan["hwmon"], fan["channel"], pwm):
                        self._last_pwm[key] = pwm
        self.tick_gpu()

    def apply_now(self) -> None:
        mode = self.state["mode"]
        curves = self.state["curves"][mode]
        errors = []

        yaml_text = build_fan2go_yaml(self.state, self.chips or hwmon_chips(), bool(which("nvidia-smi")))
        yaml_changed = yaml_text != self._last_yaml
        YAML_PATH.write_text(yaml_text, encoding="utf-8")
        if self.fan2go_installed and yaml_changed:
            ok, msg = pkexec_helper("install-config", str(YAML_PATH))
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
            if self.state.get("pumpSensor", "cpu") == "liquid":
                if not liquidctl_profile("pump", curves["pump"], PUMP_MIN):
                    errors.append("AIO pump curve failed")
            if self.state.get("aioFanControl"):
                if not liquidctl_profile("fan", curves["aio"], FAN_MIN if mode != "silent" else 0):
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
        self.tick_pump()

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
            "pumpSensor": self.state.get("pumpSensor") or "cpu",
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
            payload["tempsAxis"] = TEMPS
        return payload

    def emit_state(self, telemetry=False) -> None:
        emit(self.snapshot(telemetry=telemetry))

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
            if not self.state["gpuControl"] and self.state.get("selectedChannel") == "gpu":
                self.state["selectedChannel"] = "chassis"
            self.save_state()
            self.schedule_apply()
            self.emit_state()
            return
        if op == "set_pump_sensor":
            sensor = str(msg.get("sensor") or "")
            if sensor in ("cpu", "liquid"):
                self.state["pumpSensor"] = sensor
                self._last_pump_duty = None
                self.save_state()
                self.schedule_apply()
                self.emit_state()
            return
        if op == "set_aio_fan_control":
            self.state["aioFanControl"] = bool(msg.get("enabled", False))
            if not self.state["aioFanControl"] and self.state.get("selectedChannel") == "aio":
                self.state["selectedChannel"] = "chassis"
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
                if mode in MODES and ch in CHANNELS and 0 <= idx < POINT_COUNT:
                    minimum = PUMP_MIN if ch == "pump" else 0
                    pts = apply_monotonic(
                        self.state["curves"][mode][ch],
                        idx,
                        msg.get("value"),
                        minimum,
                    )
                    if ch == "pump":
                        pts = enforce_min(pts, PUMP_MIN)
                    self.state["curves"][mode][ch] = pts
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
                    self.state["curves"][mode][ch] = copy_points(PRESETS[src_mode][ch], minimum)
                    self._curve_rev += 1
                    self.save_state()
                    self.schedule_apply()
                    self.emit_state()
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
        "fan2go": which("fan2go"),
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
            if app.liquidctl_installed:
                if app.state.get("pumpSensor", "cpu") == "liquid":
                    curves = app.state["curves"][app.state["mode"]]
                    if liquidctl_profile("pump", curves["pump"], PUMP_MIN):
                        app.apply_error = ""
                else:
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

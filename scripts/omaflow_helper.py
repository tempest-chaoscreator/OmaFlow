#!/usr/bin/env python3
"""Privileged helper for OmaFlow.

Allowed operations only:
  write-pwm <hwmon-name> <channel> <0-255>
  install-config <src-yaml>
  restart-fan2go

The setup script installs a polkit rule so members of `wheel` can run this
without a password. The plugin never holds root itself.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

HWMON_ROOT = Path("/sys/class/hwmon")
NAME_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
FAN2GO_UNIT = "fan2go.service"
FAN2GO_DST = Path("/etc/fan2go/fan2go.yaml")


def die(msg: str, code: int = 1) -> None:
    print(msg, file=sys.stderr)
    raise SystemExit(code)


def require_root() -> None:
    if os.geteuid() != 0:
        die("omaflow-helper must run as root")


def hwmon_by_name(name: str) -> Path:
    if not NAME_RE.match(name):
        die(f"refusing hwmon name {name!r}")
    matches = []
    if not HWMON_ROOT.exists():
        die("no /sys/class/hwmon")
    for entry in HWMON_ROOT.iterdir():
        nfile = entry / "name"
        try:
            if nfile.read_text(encoding="utf-8").strip() == name:
                matches.append(entry)
        except OSError:
            continue
    if not matches:
        die(f"no hwmon device named {name}")
    return matches[0]


def write_pwm(name: str, channel: str, value: str) -> None:
    if not re.fullmatch(r"[1-9][0-9]?", channel):
        die(f"bad pwm channel {channel!r}")
    try:
        pwm = int(value)
    except ValueError:
        die(f"bad pwm value {value!r}")
    pwm = max(0, min(255, pwm))
    dev = hwmon_by_name(name)
    pwm_path = dev / f"pwm{channel}"
    enable_path = dev / f"pwm{channel}_enable"
    if not pwm_path.exists():
        die(f"{pwm_path} does not exist")
    if enable_path.exists():
        enable_path.write_text("1", encoding="utf-8")
    pwm_path.write_text(str(pwm), encoding="utf-8")
    print(f"ok {name} pwm{channel}={pwm}")


def install_config(src: str) -> None:
    path = Path(src)
    if not path.is_file():
        die(f"missing config {src}")
    text = path.read_text(encoding="utf-8", errors="replace")
    if "fans:" not in text or "curves:" not in text:
        die("refusing config that does not look like fan2go yaml")
    FAN2GO_DST.parent.mkdir(parents=True, exist_ok=True)
    Path("/var/lib/omaflow").mkdir(parents=True, exist_ok=True)
    # Keep a copy of any already-analyzed curve DB so a dbPath change does
    # not send fans through the PWM sweep again.
    try:
        home = Path(src).resolve().parents[2]
        user_db = home / ".local/share/omaflow" / "fan2go.db"
        var_db = Path("/var/lib/omaflow/fan2go.db")
        if user_db.is_file() and (not var_db.exists() or var_db.stat().st_size < user_db.stat().st_size):
            shutil.copy2(user_db, var_db)
    except (OSError, IndexError, ValueError):
        pass
    tmp = FAN2GO_DST.with_suffix(".yaml.omaflow-tmp")
    tmp.write_text(text, encoding="utf-8")
    os.chmod(tmp, 0o644)
    tmp.replace(FAN2GO_DST)
    print(f"ok wrote {FAN2GO_DST}")


def restart_fan2go() -> None:
    if shutil.which("systemctl") is None:
        die("systemctl not found")
    r = subprocess.run(
        ["systemctl", "restart", FAN2GO_UNIT],
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        die(r.stderr.strip() or f"systemctl restart {FAN2GO_UNIT} failed")
    print("ok restarted fan2go")


def main(argv: list[str]) -> None:
    require_root()
    if len(argv) < 2:
        die("usage: omaflow-helper write-pwm|install-config|restart-fan2go ...")
    op = argv[1]
    if op == "write-pwm":
        if len(argv) != 5:
            die("usage: omaflow-helper write-pwm <hwmon-name> <channel> <0-255>")
        write_pwm(argv[2], argv[3], argv[4])
    elif op == "install-config":
        if len(argv) != 3:
            die("usage: omaflow-helper install-config <src-yaml>")
        install_config(argv[2])
    elif op == "restart-fan2go":
        restart_fan2go()
    else:
        die(f"unknown op {op}")


if __name__ == "__main__":
    main(sys.argv)

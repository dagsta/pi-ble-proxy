"""Hinweise auf typische Ursachen für Bluetooth-Probleme auf dem Raspberry Pi."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

RFKILL = Path("/sys/class/rfkill")
HWMON = Path("/sys/class/hwmon")
MIN_PASSIVE_VERSION = (5, 56)  # Advertisement-Monitor-API in BlueZ


def _read(path: Path) -> str | None:
    try:
        return path.read_text().strip()
    except OSError:
        return None


def rfkill_blocked(root: Path = RFKILL) -> str | None:
    """"hard", "soft" oder None, je nachdem ob ein Bluetooth-Funkschalter blockiert."""
    for dev in sorted(root.glob("rfkill*")):
        if _read(dev / "type") != "bluetooth":
            continue
        if _read(dev / "hard") == "1":
            return "hard"
        if _read(dev / "soft") == "1":
            return "soft"
    return None


def undervoltage(root: Path = HWMON) -> bool:
    """True, wenn der Pi gerade Unterspannung meldet (Treiber rpi_volt)."""
    for dev in root.glob("hwmon*"):
        if _read(dev / "name") == "rpi_volt" and _read(dev / "in0_lcrit_alarm") == "1":
            return True
    return False


def rfkill_hint(root: Path = RFKILL) -> str | None:
    blocked = rfkill_blocked(root)
    if blocked == "soft":
        return "Bluetooth ist per rfkill blockiert: sudo rfkill unblock bluetooth"
    if blocked == "hard":
        return "Bluetooth ist per Hardware-Schalter (rfkill hard block) gesperrt"
    return None


def undervoltage_hint(root: Path = HWMON) -> str | None:
    if undervoltage(root):
        return ("Unterspannung am Raspberry Pi erkannt – das verursacht oft scheinbare Bluetooth-Fehler; "
                "Netzteil mit 5,1 V und mindestens 3 A (Pi 4) verwenden")
    return None


def hints() -> list[str]:
    return [h for h in (rfkill_hint(), undervoltage_hint()) if h]


def parse_version(text: str) -> tuple[int, int] | None:
    match = re.search(r"(\d+)\.(\d+)", text)
    return (int(match.group(1)), int(match.group(2))) if match else None


async def bluez_version() -> tuple[int, int] | None:
    """BlueZ-Version über ``bluetoothctl --version``; None, wenn nicht ermittelbar."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "bluetoothctl", "--version", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(proc.communicate(), 3)
    except (OSError, asyncio.TimeoutError):
        return None
    return parse_version(out.decode(errors="replace"))

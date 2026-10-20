"""Zähler und Diagnosewerte; speist Log, Überwachung und die Sensoren in Home Assistant."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

THERMAL = Path("/sys/class/thermal/thermal_zone0/temp")


@dataclass
class Stats:
    received: int = 0  # Advertisements vom Scanner
    sent: int = 0  # an HA verschickt (je Abonnent)
    dropped: int = 0  # verworfen (Puffer voll, langsamer Client)
    connects: int = 0
    connect_failures: int = 0
    notifications: int = 0
    started: float = field(default_factory=time.monotonic)
    last_advertisement: float = field(default_factory=time.monotonic)

    SNAPSHOT_FIELDS = ("received", "sent", "dropped", "connects", "connect_failures", "notifications")

    def snapshot(self) -> dict[str, int]:
        return {name: getattr(self, name) for name in self.SNAPSHOT_FIELDS}

    def seconds_since_advertisement(self) -> float:
        return time.monotonic() - self.last_advertisement


def cpu_temperature() -> float | None:
    try:
        return int(THERMAL.read_text()) / 1000
    except (OSError, ValueError):
        return None

"""Diagnosewerte, die Home Assistant als Sensoren des Geräts anzeigt."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from . import messages as m
from .stats import cpu_temperature

if TYPE_CHECKING:
    from .server import ApiServer


def fnv1_hash(text: str) -> int:
    """Schlüssel der Entität: FNV-1 über die Objekt-ID, wie bei ESPHome."""
    h = 2166136261
    for byte in text.encode():
        h = (h * 16777619) & 0xFFFFFFFF
        h ^= byte
    return h


@dataclass(frozen=True)
class Sensor:
    object_id: str
    name: str
    unit: str
    decimals: int
    device_class: str
    state_class: int
    value: Callable[["ApiServer"], float | None]

    @property
    def key(self) -> int:
        return fnv1_hash(self.object_id)

    def description(self) -> m.ListEntitiesSensorResponse:
        return m.ListEntitiesSensorResponse(
            object_id=self.object_id, key=self.key, name=self.name, unit_of_measurement=self.unit,
            accuracy_decimals=self.decimals, device_class=self.device_class,
            state_class=self.state_class, entity_category=m.ENTITY_CATEGORY_DIAGNOSTIC,
        )

    def state(self, server: "ApiServer") -> m.SensorStateResponse:
        value = self.value(server)
        if value is None:
            return m.SensorStateResponse(key=self.key, missing_state=True)
        return m.SensorStateResponse(key=self.key, state=float(value))


SENSORS = [
    Sensor("cpu_temperature", "CPU Temperature", "°C", 1, "temperature", m.STATE_CLASS_MEASUREMENT,
           lambda server: cpu_temperature()),
    Sensor("advertisements_received", "Advertisements Received", "", 0, "", m.STATE_CLASS_TOTAL_INCREASING,
           lambda server: server.stats.received),
    Sensor("active_connections", "Active Connections", "", 0, "", m.STATE_CLASS_MEASUREMENT,
           lambda server: sum(1 for link in server.links.values() if link.conn)),
]

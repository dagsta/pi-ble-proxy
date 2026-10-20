"""Schnittstelle für aktive GATT-Verbindungen (Implementierung: ``bluez.py``)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

# Fehlercodes, die an HA gemeldet werden (ESP-IDF-/GATT-Werte)
ERR_GENERIC = 133  # GATT_ERROR
ERR_NOT_PERMITTED = 3
ERR_NOT_FOUND = 10
ERR_TIMEOUT = 8

# Characteristic-Properties (Bluetooth Core Spec, Vol 3 Part G 3.3.1.1)
PROP_READ, PROP_WRITE_NO_RESPONSE, PROP_WRITE = 0x02, 0x04, 0x08
PROP_NOTIFY, PROP_INDICATE = 0x10, 0x20


class GattError(Exception):
    def __init__(self, message: str, code: int = ERR_GENERIC) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class Descriptor:
    uuid: str
    handle: int


@dataclass
class Characteristic:
    uuid: str
    handle: int
    properties: int
    descriptors: list[Descriptor] = field(default_factory=list)


@dataclass
class Service:
    uuid: str
    handle: int
    characteristics: list[Characteristic] = field(default_factory=list)


class GattConnection(Protocol):
    mtu: int

    async def services(self) -> list[Service]: ...
    async def read(self, handle: int) -> bytes: ...
    async def write(self, handle: int, data: bytes, response: bool) -> None: ...
    async def read_descriptor(self, handle: int) -> bytes: ...
    async def write_descriptor(self, handle: int, data: bytes) -> None: ...
    async def start_notify(self, handle: int, callback: Callable[[bytes], None]) -> None: ...
    async def stop_notify(self, handle: int) -> None: ...
    async def disconnect(self) -> None: ...


class GattBackend(Protocol):
    async def start(self) -> None: ...
    async def start_discovery(self) -> None: ...
    scan_mode: str  # "active" | "passive"
    available: bool

    async def set_scan_mode(self, mode: str) -> None: ...
    async def check(self) -> str | None: ...  # offenes Problem oder None
    async def stop(self) -> None: ...

    async def connect(
        self, address: int, address_type: int, on_disconnect: Callable[[], None]
    ) -> GattConnection: ...

    async def pair(self, address: int) -> None: ...
    async def unpair(self, address: int) -> None: ...  # entfernt auch den BlueZ-Cache


def uuid_to_words(uuid: str) -> list[int]:
    n = int(uuid.replace("-", ""), 16)
    return [n >> 64, n & ((1 << 64) - 1)]

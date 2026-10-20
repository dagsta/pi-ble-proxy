"""Schnittstelle zwischen Bluetooth-Quelle und API-Server."""

from __future__ import annotations

import asyncio
import random
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class Advertisement:
    address: int  # 48-Bit-MAC als Zahl
    address_type: int  # 0 = public, 1 = random
    rssi: int
    data: bytes  # Roh-AD-Strukturen (Advertisement ggf. + Scan Response)


class Scanner(Protocol):
    adapter_address: str  # "AA:BB:CC:DD:EE:FF", nach start() gültig

    async def start(
        self, on_advertisement: Callable[[Advertisement], None], *, discover: bool = True
    ) -> None: ...  # discover=False: nur mithören, ein anderer führt den Scan

    async def stop(self) -> None: ...

    async def check(self) -> str | None: ...  # Problem oder None


def format_mac(value: int) -> str:
    return ":".join(f"{b:02X}" for b in value.to_bytes(6, "big"))


class SimulatedScanner:
    """Erzeugt künstliche Advertisements, zum Testen ohne Bluetooth-Hardware."""

    adapter_address = "00:00:00:00:00:01"

    def __init__(self, interval: float = 1.0) -> None:
        self._interval = interval
        self._task: asyncio.Task | None = None

    async def start(
        self, on_advertisement: Callable[[Advertisement], None], *, discover: bool = True
    ) -> None:
        async def run() -> None:
            while True:
                # Flags + Complete Local Name "SIM" + 16-Bit-Service-UUID-Liste
                data = b"\x02\x01\x06\x04\x09SIM\x03\x03\x0f\x18"
                on_advertisement(
                    Advertisement(0xA4C138000001, 0, random.randint(-90, -40), data)
                )
                await asyncio.sleep(self._interval)

        self._task = asyncio.create_task(run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None

    async def check(self) -> str | None:
        return None

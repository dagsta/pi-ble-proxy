"""Konfiguration aus TOML (``tomllib`` ist in der Standardbibliothek)."""

from __future__ import annotations

import base64
import binascii
import re
import tomllib
import uuid
from dataclasses import dataclass
from pathlib import Path

_NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?$")


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Config:
    name: str
    friendly_name: str
    mac: str  # "AA:BB:CC:DD:EE:FF", Identität des Geräts in HA
    host: str
    port: int
    encryption_key: bytes
    adapter_index: int
    max_connections: int
    max_gatt_connections: int
    pause_scan_on_connect: bool
    log_level: str
    scan_mode: str = "active"
    watchdog_interval: float = 30
    watchdog_failures: int = 6
    silence_timeout: float = 300
    stats_interval: float = 3600


def default_mac() -> str:
    node = uuid.getnode()
    return ":".join(f"{b:02X}" for b in node.to_bytes(6, "big"))


def decode_key(value: str) -> bytes:
    try:
        key = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ConfigError("api.encryption_key ist kein gültiges Base64") from exc
    if len(key) != 32:
        raise ConfigError("api.encryption_key muss 32 Bytes ergeben (siehe: genkey)")
    return key


def load(path: str | Path) -> Config:
    try:
        with open(path, "rb") as f:
            raw = tomllib.load(f)
    except FileNotFoundError:
        raise ConfigError(f"Konfigurationsdatei nicht gefunden: {path}") from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: {exc}") from exc

    device, api, bt = raw.get("device", {}), raw.get("api", {}), raw.get("bluetooth", {})
    wd = raw.get("watchdog", {})
    name = device.get("name", "pi-ble-proxy")
    if not _NAME_RE.match(name):
        raise ConfigError("device.name: nur a-z, 0-9 und '-' (wie bei ESPHome), max. 32 Zeichen")
    if "encryption_key" not in api:
        raise ConfigError("api.encryption_key fehlt (erzeugen mit: python -m pi_ble_proxy genkey)")

    adapter = str(bt.get("adapter", "hci0"))
    m = re.fullmatch(r"hci(\d+)", adapter)
    if not m:
        raise ConfigError("bluetooth.adapter muss die Form 'hci0' haben")

    scan_mode = str(bt.get("scan_mode", "active"))
    if scan_mode not in ("active", "passive"):
        raise ConfigError("bluetooth.scan_mode muss \"active\" oder \"passive\" sein")

    mac = device.get("mac", default_mac()).upper()
    if not re.fullmatch(r"([0-9A-F]{2}:){5}[0-9A-F]{2}", mac):
        raise ConfigError("device.mac muss die Form AA:BB:CC:DD:EE:FF haben")

    port = int(api.get("port", 6053))
    if not 0 <= port <= 65535:
        raise ConfigError("api.port außerhalb des gültigen Bereichs")

    return Config(
        name=name,
        friendly_name=device.get("friendly_name", name),
        mac=mac,
        host=api.get("host", "0.0.0.0"),
        port=port,
        encryption_key=decode_key(api["encryption_key"]),
        adapter_index=int(m.group(1)),
        max_connections=int(api.get("max_connections", 4)),
        max_gatt_connections=int(bt.get("max_connections", 3)),
        pause_scan_on_connect=bool(bt.get("pause_scan_on_connect", True)),
        scan_mode=scan_mode,
        watchdog_interval=float(wd.get("interval", 30)),
        watchdog_failures=int(wd.get("max_failures", 6)),
        silence_timeout=float(wd.get("silence_timeout", 300)),
        stats_interval=float(raw.get("logging", {}).get("stats_interval", 3600)),
        log_level=raw.get("logging", {}).get("level", "INFO").upper(),
    )

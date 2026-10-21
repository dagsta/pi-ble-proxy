"""Bluetooth-Scanner über die Linux-Management-API (HCI_CHANNEL_CONTROL).

Der Kernel liefert hier die Advertisements im Rohformat (AD-Strukturen plus
RSSI), die BlueZ per D-Bus nur vorverarbeitet weitergibt. Der Socket läuft
parallel zu ``bluetoothd``; das Binden an den Kontrollkanal braucht
CAP_NET_ADMIN. Protokoll: ``doc/mgmt-api.txt`` in den BlueZ-Quellen.
"""

from __future__ import annotations

import asyncio
import ctypes
import logging
import os
import socket
import struct
from collections.abc import Callable

from . import diagnose
from .scanner import Advertisement, format_mac

log = logging.getLogger(__name__)

HCI_DEV_NONE = 0xFFFF
HCI_CHANNEL_CONTROL = 3
MGMT_INDEX_NONE = 0xFFFF

OP_READ_INFO = 0x0004
OP_START_DISCOVERY = 0x0023
OP_STOP_DISCOVERY = 0x0024

EV_CMD_COMPLETE = 0x0001
EV_CMD_STATUS = 0x0002
EV_INDEX_ADDED = 0x0004
EV_INDEX_REMOVED = 0x0005
EV_DEVICE_FOUND = 0x0012
EV_DISCOVERING = 0x0013
EV_ADV_MONITOR_DEVICE_FOUND = 0x002F  # passiver Scan: Monitor-Handle (u16) + Device-Found-Daten

STATUS_BUSY = 0x0A
SETTING_POWERED = 0x0001
SETTING_LE = 0x0200

# mgmt-Adresstypen als Bitmaske (Discovery) bzw. Wert (Events)
ADDR_LE_PUBLIC = 1
ADDR_LE_RANDOM = 2
DISCOVERY_LE = 0x06  # LE public | LE random

_HEADER = struct.Struct("<HHH")  # Opcode/Event, Controller-Index, Parameterlänge


class MgmtError(RuntimeError):
    pass


class _SockaddrHci(ctypes.Structure):
    _fields_ = [
        ("family", ctypes.c_ushort),
        ("dev", ctypes.c_ushort),
        ("channel", ctypes.c_ushort),
    ]


def open_mgmt_socket() -> socket.socket:
    """Öffnet einen nicht blockierenden Socket auf dem mgmt-Kanal.

    ``socket.bind`` kann in Python nur den RAW-Kanal, daher der Aufruf über libc.
    """
    if not hasattr(socket, "AF_BLUETOOTH"):
        raise MgmtError("Dieses Python wurde ohne Bluetooth-Unterstützung gebaut (socket.AF_BLUETOOTH fehlt); "
                        "das System-Python von Debian/Raspberry Pi OS (/usr/bin/python3) verwenden")
    sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_RAW | socket.SOCK_NONBLOCK, socket.BTPROTO_HCI)
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        addr = _SockaddrHci(socket.AF_BLUETOOTH, HCI_DEV_NONE, HCI_CHANNEL_CONTROL)
        if libc.bind(sock.fileno(), ctypes.byref(addr), ctypes.sizeof(addr)) != 0:
            errno = ctypes.get_errno()
            raise OSError(errno, f"bind(HCI_CHANNEL_CONTROL) fehlgeschlagen "
                                 f"(CAP_NET_ADMIN vorhanden?): {os.strerror(errno)}")
    except BaseException:
        sock.close()
        raise
    return sock


def parse_device_found(params: bytes) -> Advertisement | None:
    """MGMT_EV_DEVICE_FOUND: Adresse(6, LE) Typ(1) RSSI(s8) Flags(u32) Länge(u16) Daten."""
    if len(params) < 14:
        return None
    bdaddr, addr_type, rssi, _flags, eir_len = struct.unpack_from("<6sBbIH", params)
    if addr_type not in (ADDR_LE_PUBLIC, ADDR_LE_RANDOM):
        return None  # Classic-Geräte interessieren den BLE-Proxy nicht
    eir = params[14 : 14 + eir_len]
    return Advertisement(
        address=int.from_bytes(bdaddr, "little"),
        address_type=addr_type - 1,
        rssi=rssi,
        data=bytes(eir),
    )


class MgmtScanner:
    def __init__(self, adapter_index: int = 0, *, sock: socket.socket | None = None) -> None:
        self._index = adapter_index
        self._sock = sock
        self._loop: asyncio.AbstractEventLoop | None = None
        self._callback: Callable[[Advertisement], None] | None = None
        self._pending: dict[int, asyncio.Future] = {}
        self._want_scanning = False
        self._discover = True
        self._restart_handle: asyncio.TimerHandle | None = None
        self.adapter_address = "00:00:00:00:00:00"
        self.present = True  # False, solange der Kernel den Adapter nicht meldet
        self.dead: str | None = None  # Grund, sobald der Socket unbrauchbar ist

    # Lebenszyklus ----------------------------------------------------------
    async def start(
        self, on_advertisement: Callable[[Advertisement], None], *, discover: bool = True
    ) -> None:
        self._discover = discover
        self._loop = asyncio.get_running_loop()
        self._callback = on_advertisement
        if self._sock is None:
            self._sock = open_mgmt_socket()
        self._loop.add_reader(self._sock.fileno(), self._on_readable)

        problem = await self._read_info()
        if problem:
            raise MgmtError(problem)

        self._want_scanning = discover
        if discover:
            await self._start_discovery()
        log.info("Scanner läuft auf hci%d (%s)", self._index, self.adapter_address)

    async def _read_info(self) -> str | None:
        """Liest den Adapterzustand; gibt eine Problembeschreibung zurück oder None."""
        status, info = await self._command(OP_READ_INFO)
        if status != 0 or len(info) < 20:
            return f"hci{self._index} nicht vorhanden (mgmt-Status {status})"
        bdaddr, _version, _manufacturer, _supported, current = struct.unpack_from("<6sBHII", info)
        self.adapter_address = format_mac(int.from_bytes(bdaddr, "little"))
        if not current & SETTING_POWERED:
            hint = diagnose.rfkill_hint() or "bluetoothctl power on"
            return f"hci{self._index} ist nicht eingeschaltet ({hint})"
        if not current & SETTING_LE:
            return f"hci{self._index} unterstützt kein Bluetooth Low Energy"
        return None

    async def check(self) -> str | None:
        """Gesundheitsprüfung: Adapter da und eingeschaltet? Gibt das Problem zurück oder None."""
        try:
            problem = await asyncio.wait_for(self._read_info(), 3)
        except MgmtError as exc:
            return str(exc)
        except (asyncio.TimeoutError, OSError) as exc:
            return f"mgmt-Socket antwortet nicht ({type(exc).__name__})"
        self.present = problem is None or "nicht eingeschaltet" in problem
        return problem

    async def stop(self) -> None:
        self._want_scanning = False
        if self._restart_handle:
            self._restart_handle.cancel()
        if self._sock is None:
            return
        if self._discover:
            try:
                await asyncio.wait_for(self._command(OP_STOP_DISCOVERY, bytes([DISCOVERY_LE])), 2)
            except (asyncio.TimeoutError, OSError, MgmtError):
                pass
        self._loop.remove_reader(self._sock.fileno())
        self._sock.close()
        self._sock = None

    # Discovery -------------------------------------------------------------
    async def _start_discovery(self) -> None:
        status, _ = await self._command(OP_START_DISCOVERY, bytes([DISCOVERY_LE]))
        # BUSY: bluetoothd (oder jemand anderes) scannt bereits, wir hören dann nur mit
        if status not in (0, STATUS_BUSY):
            raise MgmtError(f"START_DISCOVERY abgelehnt (mgmt-Status 0x{status:02x})")

    def _schedule_restart(self, delay: float = 0.1) -> None:
        # Der Kernel beendet LE-Discovery nach ~10 s; wir starten sofort neu.
        if self._want_scanning and self._restart_handle is None:
            self._restart_handle = self._loop.call_later(delay, self._restart)

    def _restart(self) -> None:
        self._restart_handle = None
        if self._want_scanning:
            task = self._loop.create_task(self._start_discovery())
            task.add_done_callback(self._restart_done)

    def _restart_done(self, task: asyncio.Task) -> None:
        if task.cancelled():
            return
        if exc := task.exception():
            log.debug("Discovery-Neustart fehlgeschlagen: %s", exc)
            self._schedule_restart(1.0)

    # Socket ----------------------------------------------------------------
    async def _command(self, opcode: int, params: bytes = b"") -> tuple[int, bytes]:
        if self.dead:
            raise MgmtError(f"mgmt-Socket geschlossen: {self.dead}")
        future = self._loop.create_future()
        self._pending[opcode] = future
        self._sock.send(_HEADER.pack(opcode, self._index, len(params)) + params)
        try:
            return await asyncio.wait_for(future, 5)
        finally:
            self._pending.pop(opcode, None)

    def _on_readable(self) -> None:
        while self._sock is not None:
            try:
                packet = self._sock.recv(65535)
            except BlockingIOError:
                return
            except InterruptedError:
                continue
            except OSError as exc:
                self._socket_died(str(exc))
                return
            if not packet:  # leere Antwort = Gegenstelle hat geschlossen; ohne Abbruch drehte sich die Schleife endlos
                self._socket_died("vom Kernel geschlossen")
                return
            if len(packet) >= _HEADER.size:
                self._on_packet(packet)

    def _socket_died(self, reason: str) -> None:
        """Socket ist nicht mehr nutzbar: nicht weiter abfragen, Wartende abbrechen, Überwachung melden lassen."""
        if self.dead:
            return
        self.dead = reason
        log.error("mgmt-Socket unbrauchbar (%s); die Überwachung meldet das als Störung", reason)
        if self._sock is not None:
            self._loop.remove_reader(self._sock.fileno())
        for future in self._pending.values():
            if not future.done():
                future.set_exception(MgmtError(f"mgmt-Socket geschlossen: {reason}"))

    def _on_packet(self, packet: bytes) -> None:
        event, index, length = _HEADER.unpack_from(packet)
        params = packet[_HEADER.size : _HEADER.size + length]
        if index != self._index and index != MGMT_INDEX_NONE:
            return
        if event == EV_DEVICE_FOUND:
            if (adv := parse_device_found(params)) and self._callback:
                self._callback(adv)
        elif event == EV_ADV_MONITOR_DEVICE_FOUND:
            if (adv := parse_device_found(params[2:])) and self._callback:
                self._callback(adv)
        elif event in (EV_CMD_COMPLETE, EV_CMD_STATUS) and len(params) >= 3:
            opcode, status = struct.unpack_from("<HB", params)
            future = self._pending.get(opcode)
            if future and not future.done() and index == self._index:
                future.set_result((status, params[3:]))
        elif event == EV_DISCOVERING and len(params) >= 2:
            if params[1] == 0:
                self._schedule_restart()
        elif event == EV_INDEX_REMOVED and index == self._index:
            log.error("Adapter hci%d wurde entfernt", self._index)
            self.present = False
        elif event == EV_INDEX_ADDED and index == self._index:
            log.info("Adapter hci%d ist wieder da", self._index)
            self.present = True

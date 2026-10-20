"""GATT-Backend auf Basis von BlueZ (D-Bus, über ``dbus-fast``).

Die ATT-Handles, die HA erwartet, stecken im Objektpfad von BlueZ
(``.../service000a/char000b/desc000d``) und werden von dort gelesen.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable

from dbus_fast import BusType, Message, MessageType, Variant
from dbus_fast.aio import MessageBus

from . import gatt
from .agent import AGENT_CAPABILITY, AGENT_PATH, MONITOR_APP, MONITOR_PATH, AdvertisementMonitor, PairingAgent
from .gatt import Characteristic, Descriptor, GattError, Service

log = logging.getLogger(__name__)

BLUEZ = "org.bluez"
DEVICE, ADAPTER = "org.bluez.Device1", "org.bluez.Adapter1"
AGENT_MANAGER, MONITOR_MANAGER = "org.bluez.AgentManager1", "org.bluez.AdvertisementMonitorManager1"
DBUS = "org.freedesktop.DBus"
SERVICE, CHAR, DESC = "org.bluez.GattService1", "org.bluez.GattCharacteristic1", "org.bluez.GattDescriptor1"
PROPS, OBJMGR = "org.freedesktop.DBus.Properties", "org.freedesktop.DBus.ObjectManager"

CONNECT_TIMEOUT = 20
CONNECT_DEADLINE = 25  # Gesamtdauer inkl. Wiederholungen
CONNECT_ATTEMPTS = 3
RETRY_DELAY = 0.5
RESOLVE_TIMEOUT = 15
PAIR_TIMEOUT = 30
DISCOVER_TIMEOUT = 10
DEFAULT_MTU = 23
CCCD_UUID_PREFIX = "00002902-"

_FLAGS = {
    "broadcast": 0x01, "read": 0x02, "write-without-response": 0x04, "write": 0x08,
    "notify": 0x10, "indicate": 0x20, "authenticated-signed-writes": 0x40,
    "extended-properties": 0x80,
}
_HANDLE_RE = re.compile(r"/(?:service|char|desc)([0-9a-fA-F]{4})$")
_ERROR_CODES = {"NotPermitted": gatt.ERR_NOT_PERMITTED, "DoesNotExist": gatt.ERR_NOT_FOUND}


def device_path(adapter_index: int, address: int) -> str:
    mac = "_".join(f"{b:02X}" for b in address.to_bytes(6, "big"))
    return f"/org/bluez/hci{adapter_index}/dev_{mac}"


def _unwrap(value):
    return value.value if isinstance(value, Variant) else value


class BluezBackend:
    def __init__(self, adapter_index: int = 0, *, bus_address: str | None = None,
                 pause_scan: bool = True, scan_mode: str = "active") -> None:
        self._pause_scan = pause_scan
        self._scan_mode = scan_mode
        self._agent = PairingAgent()
        self._monitor = AdvertisementMonitor()
        self._monitor_registered = False
        self._tasks: set[asyncio.Task] = set()
        self.available = False  # False, solange bluetoothd nicht auf D-Bus erreichbar ist
        self._pausers = 0
        self._index = adapter_index
        self._adapter = f"/org/bluez/hci{adapter_index}"
        self._bus_address = bus_address
        self._bus: MessageBus | None = None
        self._conns: dict[str, BluezConnection] = {}
        self._discovering = False

    # Lebenszyklus ----------------------------------------------------------
    async def start(self) -> None:
        kwargs = {"bus_address": self._bus_address} if self._bus_address else {"bus_type": BusType.SYSTEM}
        try:
            self._bus = await MessageBus(**kwargs).connect()
        except Exception as exc:
            raise GattError(f"D-Bus nicht erreichbar: {exc}") from exc
        self._bus.add_message_handler(self._on_message)
        rules = (
            f"type='signal',sender='{BLUEZ}',interface='{PROPS}',member='PropertiesChanged',path_namespace='/org/bluez'",
            f"type='signal',sender='{DBUS}',interface='{DBUS}',member='NameOwnerChanged',arg0='{BLUEZ}'",
        )
        for rule in rules:
            await self.call("/org/freedesktop/DBus", DBUS, "AddMatch", "s", [rule], dest=DBUS)
        self._bus.export(AGENT_PATH, self._agent)
        self._bus.export(MONITOR_PATH, self._monitor)
        await self._register_agent()
        self.available = True

    @property
    def scan_mode(self) -> str:
        return self._scan_mode

    async def _register_agent(self) -> None:
        """Ohne Agent scheitert das Pairing; fehlt er, bleibt der Rest trotzdem nutzbar."""
        try:
            await self.call("/org/bluez", AGENT_MANAGER, "RegisterAgent", "os", [AGENT_PATH, AGENT_CAPABILITY])
            await self.call("/org/bluez", AGENT_MANAGER, "RequestDefaultAgent", "o", [AGENT_PATH])
        except GattError as exc:
            log.warning("Pairing-Agent nicht registriert (Pairing eingeschränkt): %s", exc)

    async def start_discovery(self) -> None:
        """Startet den Scan im eingestellten Modus (BlueZ scannt, nur dann kennt es Geräte)."""
        await self._begin_scanning()

    async def _begin_scanning(self) -> None:
        if self._scan_mode == "passive":
            await self._stop_active_discovery()
            await self._register_monitor()
        else:
            await self._unregister_monitor()
            await self._start_active_discovery()

    async def _start_active_discovery(self) -> None:
        flt = {"Transport": Variant("s", "le"), "DuplicateData": Variant("b", True)}
        await self.call(self._adapter, ADAPTER, "SetDiscoveryFilter", "a{sv}", [flt])
        try:
            await self.call(self._adapter, ADAPTER, "StartDiscovery")
        except GattError as exc:
            if "InProgress" not in str(exc):
                raise
        self._discovering = True

    async def _stop_active_discovery(self) -> None:
        if self._discovering:
            self._discovering = False
            await self._quiet(ADAPTER, "StopDiscovery")

    async def _register_monitor(self) -> None:
        if self._monitor_registered:
            return
        await self.call(self._adapter, MONITOR_MANAGER, "RegisterMonitor", "o", [MONITOR_APP])
        self._monitor_registered = True

    async def _unregister_monitor(self) -> None:
        if self._monitor_registered:
            self._monitor_registered = False
            try:
                await self.call(self._adapter, MONITOR_MANAGER, "UnregisterMonitor", "o", [MONITOR_APP])
            except GattError as exc:
                log.debug("UnregisterMonitor: %s", exc)

    async def set_scan_mode(self, mode: str) -> None:
        """Wechselt zwischen aktivem und passivem Scan; bei Fehlern bleibt der alte Modus."""
        if mode not in ("active", "passive"):
            raise GattError(f"unbekannter Scan-Modus {mode!r}")
        if mode == self._scan_mode:
            return
        previous, self._scan_mode = self._scan_mode, mode
        try:
            await self._begin_scanning()
        except GattError:
            self._scan_mode = previous
            try:
                await self._begin_scanning()
            except GattError as exc:
                log.error("Rückkehr zum Scan-Modus %s fehlgeschlagen: %s", previous, exc)
            raise
        log.info("Scan-Modus: %s", mode)

    async def stop(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        if self.available:
            await self._stop_active_discovery()
            await self._unregister_monitor()
        for conn in list(self._conns.values()):
            await conn.disconnect()
        if self._bus:
            self._bus.disconnect()
            self._bus = None

    # D-Bus-Hilfen ----------------------------------------------------------
    async def call(self, path: str, iface: str, member: str, signature: str = "",
                   body: list | None = None, *, dest: str = BLUEZ):
        reply = await self._bus.call(Message(destination=dest, path=path, interface=iface,
                                             member=member, signature=signature, body=body or []))
        if reply.message_type == MessageType.ERROR:
            name = reply.error_name or ""
            text = reply.body[0] if reply.body else name
            code = _ERROR_CODES.get(name.rsplit(".", 1)[-1], gatt.ERR_GENERIC)
            raise GattError(f"{name}: {text}", code)
        return reply.body

    async def get_all(self, path: str, iface: str) -> dict:
        (props,) = await self.call(path, PROPS, "GetAll", "s", [iface])
        return {k: _unwrap(v) for k, v in props.items()}

    async def managed_objects(self) -> dict:
        (objects,) = await self.call("/", OBJMGR, "GetManagedObjects")
        return objects

    def _spawn(self, coro) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _on_message(self, msg: Message) -> None:
        if msg.message_type != MessageType.SIGNAL:
            return
        if msg.member == "NameOwnerChanged" and msg.body and msg.body[0] == BLUEZ:
            if msg.body[2]:
                self._spawn(self._bluez_back())
            else:
                self._bluez_gone()
            return
        if msg.member != "PropertiesChanged":
            return
        iface, changed, _invalidated = msg.body
        for path, conn in self._conns.items():
            if msg.path == path or msg.path.startswith(path + "/"):
                conn.on_properties(msg.path, iface, {k: _unwrap(v) for k, v in changed.items()})

    # Überwachung ---------------------------------------------------------------
    def _bluez_gone(self) -> None:
        """bluetoothd wurde beendet oder neu gestartet: Scan, Agent und Verbindungen sind weg."""
        if not self.available:
            return
        log.warning("bluetoothd ist nicht mehr erreichbar; alle Verbindungen sind verloren")
        self.available = False
        self._discovering = False
        self._monitor_registered = False
        conns, self._conns = list(self._conns.values()), {}
        for conn in conns:
            conn.lost()

    async def _bluez_back(self) -> None:
        log.info("bluetoothd ist wieder da, stelle Scan wieder her")
        try:
            await self._restore()
        except GattError as exc:
            log.warning("Wiederherstellung noch nicht möglich (%s), die Überwachung versucht es weiter", exc)

    async def _restore(self) -> None:
        """Wartet, bis BlueZ den Adapter anbietet, und richtet Agent und Scan neu ein."""
        for _ in range(60):
            try:
                await self.get_all(self._adapter, ADAPTER)
                break
            except GattError:
                await asyncio.sleep(0.5)
        else:
            raise GattError(f"Adapter hci{self._index} wird von BlueZ nicht angeboten")
        await self._register_agent()
        await self._begin_scanning()
        self.available = True
        log.info("Scan wiederhergestellt")

    async def check(self) -> str | None:
        """Prüft BlueZ und den Adapter und repariert, was geht. Gibt das offene Problem zurück."""
        try:
            if not self.available:
                await self._restore()
            props = await self.get_all(self._adapter, ADAPTER)
            if not props.get("Powered"):
                log.warning("Adapter hci%d war ausgeschaltet, schalte ihn ein", self._index)
                await self.call(self._adapter, PROPS, "Set", "ssv", [ADAPTER, "Powered", Variant("b", True)])
                await self._begin_scanning()
            elif self._discovering and self._pausers == 0 and not props.get("Discovering"):
                log.warning("BlueZ-Scan war gestoppt, starte ihn neu")
                await self._start_active_discovery()
        except GattError as exc:
            return str(exc)
        return None

    # GattBackend -----------------------------------------------------------
    async def connect(self, address: int, address_type: int,
                      on_disconnect: Callable[[], None]) -> "BluezConnection":
        path = device_path(self._index, address)
        if path in self._conns:
            raise GattError("bereits verbunden")
        conn = BluezConnection(self, path, on_disconnect)
        self._conns[path] = conn
        try:
            await self._ensure_known(path)
            if not (await self.get_all(path, DEVICE)).get("Connected"):
                await self._connect_with_timeout(path)
            await conn.load()
        except BaseException:
            self._conns.pop(path, None)
            await self._quiet_disconnect(path)
            raise
        return conn

    async def _connect_with_timeout(self, path: str) -> None:
        """Connect mit Scan-Pause und Wiederholung bei ``abort-by-local``.

        BlueZ bricht den Aufbau gern lokal ab, wenn dabei gescannt wird (bekannt auf dem Pi);
        ESPHome stoppt den Scan deshalb während des Verbindens, wir tun dasselbe.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + CONNECT_DEADLINE
        async with self._scan_paused():
            for attempt in range(1, CONNECT_ATTEMPTS + 1):
                try:
                    timeout = min(CONNECT_TIMEOUT, max(deadline - loop.time(), 1))
                    await asyncio.wait_for(self.call(path, DEVICE, "Connect"), timeout)
                    return
                except asyncio.TimeoutError:
                    raise GattError("Verbindungsaufbau Zeitüberschreitung", gatt.ERR_TIMEOUT) from None
                except GattError as exc:
                    if "abort-by-local" not in str(exc) or attempt == CONNECT_ATTEMPTS \
                            or loop.time() + RETRY_DELAY >= deadline:
                        raise
                    log.info("Connect-Versuch %d/%d abgebrochen (%s), wiederhole", attempt,
                             CONNECT_ATTEMPTS, exc)
                    await asyncio.sleep(RETRY_DELAY)

    def _scan_paused(self):
        backend = self

        class _Pause:
            async def __aenter__(self):
                if backend._pause_scan and backend._discovering:
                    backend._pausers += 1
                    if backend._pausers == 1:
                        await backend._quiet(ADAPTER, "StopDiscovery")
                return self

            async def __aexit__(self, *exc):
                if backend._pause_scan and backend._discovering:
                    backend._pausers -= 1
                    if backend._pausers == 0:
                        await backend._quiet(ADAPTER, "StartDiscovery")

        return _Pause()

    async def _quiet(self, iface: str, member: str) -> None:
        try:
            await self.call(self._adapter, iface, member)
        except GattError as exc:
            log.warning("%s fehlgeschlagen: %s", member, exc)

    async def _quiet_disconnect(self, path: str) -> None:
        try:
            await self.call(path, DEVICE, "Disconnect")
        except GattError:
            pass

    async def _ensure_known(self, path: str) -> None:
        """BlueZ kennt das Gerät nur, wenn es es schon gesehen hat; sonst kurz scannen."""
        if await self._exists(path):
            return
        log.debug("%s unbekannt, starte BlueZ-Discovery", path)
        started = True
        try:
            await self.call(self._adapter, ADAPTER, "StartDiscovery")
        except GattError as exc:
            started = False  # z. B. InProgress, weil wir ohnehin scannen
            log.info("StartDiscovery für unbekanntes Gerät: %s", exc)
        try:
            deadline = asyncio.get_running_loop().time() + DISCOVER_TIMEOUT
            while asyncio.get_running_loop().time() < deadline:
                if await self._exists(path):
                    return
                await asyncio.sleep(0.25)
            log.warning("%s: BlueZ kennt das Gerät nicht (Scan läuft: %s)", path, self._discovering)
            raise GattError("Gerät nicht gefunden", gatt.ERR_NOT_FOUND)
        finally:
            if started:
                try:
                    await self.call(self._adapter, ADAPTER, "StopDiscovery")
                except GattError:
                    pass

    async def _exists(self, path: str) -> bool:
        try:
            await self.get_all(path, DEVICE)
            return True
        except GattError:
            return False

    async def pair(self, address: int) -> None:
        path = device_path(self._index, address)
        await self._ensure_known(path)
        self._agent.allowed = path  # nur dieses Gerät darf jetzt Pairing-Fragen auslösen
        try:
            if not (await self.get_all(path, DEVICE)).get("Paired"):
                async with self._scan_paused():
                    await asyncio.wait_for(self.call(path, DEVICE, "Pair"), PAIR_TIMEOUT)
            await self.call(path, PROPS, "Set", "ssv", [DEVICE, "Trusted", Variant("b", True)])
        except asyncio.TimeoutError:
            raise GattError("Pairing Zeitüberschreitung", gatt.ERR_TIMEOUT) from None
        finally:
            self._agent.allowed = None

    async def unpair(self, address: int) -> None:
        path = device_path(self._index, address)
        if path in self._conns:
            await self._conns[path].disconnect()
        try:
            await self.call(self._adapter, ADAPTER, "RemoveDevice", "o", [path])
        except GattError as exc:
            if "DoesNotExist" not in str(exc):
                raise


class BluezConnection:
    def __init__(self, backend: BluezBackend, path: str, on_disconnect: Callable[[], None]) -> None:
        self._b, self._path, self._on_disconnect = backend, path, on_disconnect
        self.mtu = DEFAULT_MTU
        self._paths: dict[int, tuple[str, str]] = {}  # Handle -> (Pfad, Interface)
        self._props: dict[int, int] = {}  # Characteristic-Handle -> Property-Bits
        self._cccd: set[int] = set()  # Handles der Client Characteristic Configuration Descriptors
        self._services: list[Service] = []
        self._notify: dict[str, Callable[[bytes], None]] = {}
        self._resolved = asyncio.Event()
        self._closed = False
        self._loaded = False

    # Aufbau ----------------------------------------------------------------
    async def load(self) -> None:
        if (await self._b.get_all(self._path, DEVICE)).get("ServicesResolved"):
            self._resolved.set()
        try:
            await asyncio.wait_for(self._resolved.wait(), RESOLVE_TIMEOUT)
        except asyncio.TimeoutError:
            raise GattError("Service-Erkennung Zeitüberschreitung", gatt.ERR_TIMEOUT) from None
        await self._read_services()
        self._loaded = True

    async def _read_services(self) -> None:
        prefix = self._path + "/"
        objects = {p: {i: {k: _unwrap(v) for k, v in pr.items()} for i, pr in ifs.items()}
                   for p, ifs in (await self._b.managed_objects()).items() if p.startswith(prefix)}

        def handle(p: str) -> int:
            m = _HANDLE_RE.search(p)
            return int(m.group(1), 16) if m else 0

        mtu = 0
        services: dict[str, Service] = {}
        chars: dict[str, Characteristic] = {}
        for p, ifs in sorted(objects.items(), key=lambda kv: handle(kv[0])):
            if SERVICE in ifs:
                services[p] = Service(ifs[SERVICE]["UUID"], handle(p))
                self._paths[handle(p)] = (p, SERVICE)
            elif CHAR in ifs:
                c = ifs[CHAR]
                flags = sum(_FLAGS.get(f, 0) for f in c.get("Flags", []))
                chars[p] = Characteristic(c["UUID"], handle(p), flags)
                self._props[handle(p)] = flags
                services[c["Service"]].characteristics.append(chars[p])
                self._paths[handle(p)] = (p, CHAR)
                mtu = max(mtu, c.get("MTU", 0))
            elif DESC in ifs:
                d = ifs[DESC]
                chars[d["Characteristic"]].descriptors.append(Descriptor(d["UUID"], handle(p)))
                if d["UUID"].lower().startswith(CCCD_UUID_PREFIX):
                    self._cccd.add(handle(p))
                self._paths[handle(p)] = (p, DESC)
        self._services = list(services.values())
        self.mtu = mtu or DEFAULT_MTU

    def lost(self) -> None:
        """bluetoothd ist weg: wie ein vom Gerät beendete Verbindung melden."""
        if self._loaded and not self._closed:
            self._closed = True
            self._on_disconnect()

    def on_properties(self, path: str, iface: str, changed: dict) -> None:
        if path == self._path and iface == DEVICE:
            if changed.get("ServicesResolved"):
                self._resolved.set()
            if changed.get("Connected") is False and self._loaded and not self._closed:
                self._closed = True
                self._on_disconnect()
        elif iface == CHAR and "Value" in changed and (cb := self._notify.get(path)):
            cb(bytes(changed["Value"]))

    # GattConnection --------------------------------------------------------
    def _lookup(self, handle: int, iface: str) -> str:
        entry = self._paths.get(handle)
        if entry is None or entry[1] != iface:
            raise GattError(f"unbekanntes Handle {handle}", gatt.ERR_NOT_FOUND)
        return entry[0]

    async def services(self) -> list[Service]:
        return self._services

    async def read(self, handle: int) -> bytes:
        (value,) = await self._b.call(self._lookup(handle, CHAR), CHAR, "ReadValue", "a{sv}", [{}])
        return bytes(value)

    def _write_type(self, handle: int, response: bool) -> str:
        """BlueZ lehnt Schreibarten ab, die die Characteristic nicht anbietet (ein ESP32 sendet sie
        trotzdem). Deshalb: gewünschte Art, wenn erlaubt, sonst die, die die Characteristic kann."""
        flags = self._props.get(handle, 0)
        can_request, can_command = bool(flags & gatt.PROP_WRITE), bool(flags & gatt.PROP_WRITE_NO_RESPONSE)
        if response:
            return "request" if can_request or not can_command else "command"
        return "command" if can_command or not can_request else "request"

    async def write(self, handle: int, data: bytes, response: bool) -> None:
        path = self._lookup(handle, CHAR)
        wtype = self._write_type(handle, response)
        log.debug("Write %s: %d Bytes, Art %s (gewünscht: %s, Flags 0x%02x)", path, len(data), wtype,
                  "request" if response else "command", self._props.get(handle, 0))
        await self._b.call(path, CHAR, "WriteValue", "aya{sv}", [bytes(data), {"type": Variant("s", wtype)}])

    async def read_descriptor(self, handle: int) -> bytes:
        (value,) = await self._b.call(self._lookup(handle, DESC), DESC, "ReadValue", "a{sv}", [{}])
        return bytes(value)

    async def write_descriptor(self, handle: int, data: bytes) -> None:
        if handle in self._cccd:
            # HA schreibt den CCCD selbst (ein ESP32 tut das nicht von allein). BlueZ verbietet das und
            # setzt ihn über StartNotify/StopNotify; das ist hier bereits geschehen oder folgt.
            self._lookup(handle, DESC)
            log.debug("CCCD-Write 0x%04x (%s) nicht an BlueZ weitergereicht", handle, bytes(data).hex())
            return
        await self._b.call(self._lookup(handle, DESC), DESC, "WriteValue", "aya{sv}", [bytes(data), {}])

    async def start_notify(self, handle: int, callback: Callable[[bytes], None]) -> None:
        path = self._lookup(handle, CHAR)
        self._notify[path] = callback
        try:
            await self._b.call(path, CHAR, "StartNotify")
        except GattError as exc:
            if "Already" not in str(exc) and "InProgress" not in str(exc):
                self._notify.pop(path, None)
                raise

    async def stop_notify(self, handle: int) -> None:
        path = self._lookup(handle, CHAR)
        self._notify.pop(path, None)
        await self._b.call(path, CHAR, "StopNotify")

    async def disconnect(self) -> None:
        self._closed = True
        self._b._conns.pop(self._path, None)
        await self._b._quiet_disconnect(self._path)

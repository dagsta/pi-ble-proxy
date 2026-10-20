"""ESPHome-Native-API-Server (verschlüsselt, Noise) mit Bluetooth-Proxy-Funktionen."""

from __future__ import annotations

import asyncio
import logging
import struct
import time
from collections.abc import Awaitable, Callable

from . import __version__
from . import messages as m
from . import gatt as g
from .config import Config
from .noise import CipherState, Handshake, HandshakeError
from .protobuf import DecodeError, Message
from .scanner import Advertisement, Scanner, format_mac
from .sensors import SENSORS
from .stats import Stats
from .supervisor import Supervisor
from .gatt import GattBackend, GattConnection, GattError

log = logging.getLogger(__name__)

API_VERSION = (1, 13)
ESPHOME_VERSION = "2025.10.0"  # Kompatibilitätsangabe für HA (Versionsprüfungen)

# bluetooth_proxy_feature_flags
FEATURE_PASSIVE_SCAN = 1 << 0
FEATURE_ACTIVE_CONNECTIONS = 1 << 1
FEATURE_REMOTE_CACHING = 1 << 2
FEATURE_PAIRING = 1 << 3
FEATURE_CACHE_CLEARING = 1 << 4
FEATURE_RAW_ADVERTISEMENTS = 1 << 5
FEATURE_STATE_AND_MODE = 1 << 6
FEATURES_ACTIVE = (FEATURE_ACTIVE_CONNECTIONS | FEATURE_REMOTE_CACHING
                   | FEATURE_PAIRING | FEATURE_CACHE_CLEARING)

FLUSH_INTERVAL = 0.1  # Advertisements werden gebündelt alle 100 ms verschickt
MAX_PER_PACKET = 64
MAX_PENDING = 4096
MAX_WRITE_BUFFER = 256 * 1024  # langsame Clients: Advertisements verwerfen statt puffern
HANDSHAKE_TIMEOUT = 10
IDLE_TIMEOUT = 120  # HA schickt alle ~20 s einen Ping


class ApiServer:
    def __init__(self, config: Config, scanner: Scanner, gatt: GattBackend | None = None) -> None:
        self.config = config
        self.scanner = scanner
        self.gatt = gatt
        self.links: dict[int, Link] = {}  # Adresse -> belegter Verbindungsslot
        self.feature_flags = FEATURE_PASSIVE_SCAN | FEATURE_RAW_ADVERTISEMENTS
        self._server: asyncio.Server | None = None
        self._sessions: set[Session] = set()
        self._pending: list[Advertisement] = []
        self._flush_handle: asyncio.TimerHandle | None = None
        self.stats = Stats()
        self.failed: asyncio.Future | None = None  # wird gesetzt, wenn sich der Dienst beenden soll
        self.scanner_problem: str | None = None
        self._supervisor: Supervisor | None = None

    @property
    def sessions(self) -> set["Session"]:
        return self._sessions

    @property
    def port(self) -> int:
        return self._server.sockets[0].getsockname()[1]

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self.failed = self._loop.create_future()
        discover = True  # wer scannt: BlueZ (bevorzugt) oder wir selbst über mgmt
        if self.gatt is not None:
            try:
                await self.gatt.start()
                await self.gatt.start_discovery()
                self.feature_flags |= FEATURES_ACTIVE | FEATURE_STATE_AND_MODE
                discover = False
            except GattError as exc:
                log.warning("Aktive Verbindungen deaktiviert: %s", exc)
                self.gatt = None
        await self.scanner.start(self.on_advertisement, discover=discover)
        self._server = await asyncio.start_server(self._accept, self.config.host, self.config.port)
        log.info("API lauscht auf %s:%d als '%s'", self.config.host, self.port, self.config.name)
        self._supervisor = Supervisor(self)
        self._supervisor.start()

    async def stop(self) -> None:
        if self._supervisor:
            await self._supervisor.stop()
        if self._server:
            self._server.close()
        for session in list(self._sessions):
            session.close()
        await self.scanner.stop()
        if self.gatt is not None:
            await self.gatt.stop()
        if self._server:
            await self._server.wait_closed()

    # Betrieb ---------------------------------------------------------------------
    def fail(self, reason: str) -> None:
        """Dauerhafte Störung: Prozess beenden, systemd startet ihn neu."""
        log.error("%s – beende mich", reason)
        if self.failed and not self.failed.done():
            self.failed.set_exception(RuntimeError(reason))

    def set_scanner_problem(self, problem: str | None) -> None:
        changed = (problem is None) != (self.scanner_problem is None)
        self.scanner_problem = problem
        if changed:
            self.broadcast_scanner_state()

    def scanner_state(self) -> m.BluetoothScannerStateResponse:
        modes = {"active": m.MODE_ACTIVE, "passive": m.MODE_PASSIVE}
        current = self.gatt.scan_mode if self.gatt is not None else "active"
        return m.BluetoothScannerStateResponse(
            state=m.SCANNER_FAILED if self.scanner_problem else m.SCANNER_RUNNING,
            mode=modes[current], configured_mode=modes[self.config.scan_mode])

    def broadcast_scanner_state(self) -> None:
        for session in self._sessions:
            session.send_scanner_state()

    # GATT-Slots ----------------------------------------------------------------
    @property
    def free_slots(self) -> int:
        return max(self.config.max_gatt_connections - len(self.links), 0)

    def broadcast_free(self) -> None:
        for session in self._sessions:
            session.send_connections_free()

    def link_lost(self, address: int) -> None:
        link = self.links.pop(address, None)
        if link is None:
            return
        log.info("Verbindung zu %s vom Gerät/BlueZ beendet", format_mac(address))
        link.session.send(m.BluetoothDeviceConnectionResponse(address=address, connected=False))
        self.broadcast_free()

    async def drop_links(self, session: "Session") -> None:
        for address, link in list(self.links.items()):
            if link.session is session:
                self.links.pop(address, None)
                if link.task and not link.task.done():
                    link.task.cancel()
                elif link.conn:
                    await link.conn.disconnect()
        self.broadcast_free()

    # Advertisements ----------------------------------------------------------
    def on_advertisement(self, adv: Advertisement) -> None:
        self.stats.received += 1
        self.stats.last_advertisement = time.monotonic()
        if not any(s.wants_advertisements for s in self._sessions):
            return
        if len(self._pending) >= MAX_PENDING:
            self.stats.dropped += 1
            return
        self._pending.append(adv)
        if self._flush_handle is None:
            self._flush_handle = self._loop.call_later(FLUSH_INTERVAL, self._flush)

    def _flush(self) -> None:
        self._flush_handle = None
        batch, self._pending = self._pending, []
        subscribers = [s for s in self._sessions if s.wants_advertisements]
        for i in range(0, len(batch), MAX_PER_PACKET):
            chunk = batch[i : i + MAX_PER_PACKET]
            payload = m.BluetoothLERawAdvertisementsResponse(
                advertisements=[
                    m.BluetoothLERawAdvertisement(a.address, a.rssi, a.address_type, a.data)
                    for a in chunk
                ]
            ).encode()
            for session in subscribers:
                if session.write_backlog() > MAX_WRITE_BUFFER:
                    self.stats.dropped += len(chunk)
                else:
                    self.stats.sent += len(chunk)
                    session.send_raw(m.BluetoothLERawAdvertisementsResponse.ID, payload)

    # Verbindungen ------------------------------------------------------------
    async def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        if len(self._sessions) >= self.config.max_connections:
            log.warning("Verbindung von %s abgelehnt: zu viele Clients", peer)
            writer.close()
            return
        session = Session(self, reader, writer, peer)
        self._sessions.add(session)
        try:
            await session.run()
        except (asyncio.IncompleteReadError, ConnectionError, asyncio.TimeoutError) as exc:
            log.debug("%s: Verbindung beendet (%s)", peer, type(exc).__name__)
        except Exception:
            log.exception("%s: unerwarteter Fehler", peer)
        finally:
            self._sessions.discard(session)
            session.close()
            await self.drop_links(session)
            log.info("%s: getrennt", peer)


class Link:
    """Ein belegter GATT-Slot; ``conn`` ist bis zum Verbindungsaufbau None."""

    def __init__(self, session: "Session") -> None:
        self.session = session
        self.conn: GattConnection | None = None
        self.task: asyncio.Task | None = None


class Session:
    def __init__(self, server: ApiServer, reader, writer, peer) -> None:
        self.server, self.reader, self.writer, self.peer = server, reader, writer, peer
        self.wants_advertisements = False
        self.wants_free = False
        self.wants_states = False
        self._tasks: set[asyncio.Task] = set()
        self._send: CipherState | None = None
        self._recv: CipherState | None = None
        self._closing = False
        self._handlers: dict[int, Callable[[Message], Awaitable[None] | None]] = {
            m.HelloRequest.ID: self._hello,
            m.AuthenticationRequest.ID: self._authenticate,
            m.PingRequest.ID: lambda _: self.send(m.PingResponse()),
            m.DisconnectRequest.ID: self._disconnect,
            m.DeviceInfoRequest.ID: self._device_info,
            m.ListEntitiesRequest.ID: self._list_entities,
            m.SubscribeStatesRequest.ID: self._subscribe_states,
            m.BluetoothScannerSetModeRequest.ID: lambda r: self._spawn(self._set_scanner_mode(r)),
            m.SubscribeHomeassistantServicesRequest.ID: lambda _: None,
            m.SubscribeHomeAssistantStatesRequest.ID: lambda _: None,
            m.SubscribeBluetoothLEAdvertisementsRequest.ID: self._subscribe_adv,
            m.UnsubscribeBluetoothLEAdvertisementsRequest.ID: self._unsubscribe_adv,
            m.SubscribeBluetoothConnectionsFreeRequest.ID: self._subscribe_free,
            m.BluetoothDeviceRequest.ID: self._device_request,
            m.BluetoothGATTGetServicesRequest.ID: lambda r: self._spawn(self._get_services(r)),
            m.BluetoothGATTReadRequest.ID: lambda r: self._spawn(self._read(r, False)),
            m.BluetoothGATTReadDescriptorRequest.ID: lambda r: self._spawn(self._read(r, True)),
            m.BluetoothGATTWriteRequest.ID: lambda r: self._spawn(self._write(r)),
            m.BluetoothGATTWriteDescriptorRequest.ID: lambda r: self._spawn(self._write_descriptor(r)),
            m.BluetoothGATTNotifyRequest.ID: lambda r: self._spawn(self._notify(r)),
        }

    # Transport ---------------------------------------------------------------
    def write_backlog(self) -> int:
        return self.writer.transport.get_write_buffer_size()

    def close(self) -> None:
        self._closing = True
        self.wants_advertisements = False
        for task in list(self._tasks):
            task.cancel()
        if not self.writer.is_closing():
            self.writer.close()

    def _write_frame(self, body: bytes) -> None:
        self.writer.write(b"\x01" + struct.pack(">H", len(body)) + body)

    async def _read_frame(self) -> bytes:
        header = await self.reader.readexactly(3)
        if header[0] != 0x01:
            raise ConnectionError(
                "Klartext-Protokoll nicht unterstützt" if header[0] == 0 else "ungültiges Preamble"
            )
        return await self.reader.readexactly(int.from_bytes(header[1:], "big"))

    def send(self, message: Message) -> None:
        self.send_raw(message.ID, message.encode())

    def send_raw(self, msg_id: int, payload: bytes) -> None:
        if self._send is None or self._closing:
            return
        plain = struct.pack(">HH", msg_id, len(payload)) + payload
        self._write_frame(self._send.encrypt(b"", plain))

    # Ablauf ------------------------------------------------------------------
    async def run(self) -> None:
        log.info("%s: verbunden", self.peer)
        if not await asyncio.wait_for(self._handshake(), HANDSHAKE_TIMEOUT):
            return
        while not self._closing:
            frame = await asyncio.wait_for(self._read_frame(), IDLE_TIMEOUT)
            try:
                plain = self._recv.decrypt(b"", frame)
            except Exception:
                log.warning("%s: Entschlüsselung fehlgeschlagen, trenne", self.peer)
                return
            if len(plain) < 4:
                return
            msg_id, _length = struct.unpack_from(">HH", plain)
            await self._dispatch(msg_id, plain[4:])
            await self.writer.drain()

    async def _handshake(self) -> bool:
        cfg = self.server.config
        await self._read_frame()  # leeres Client-Hello
        self._write_frame(b"\x01" + cfg.name.encode() + b"\x00" + cfg.mac.encode() + b"\x00")
        client_msg = await self._read_frame()
        if not client_msg or client_msg[0] != 0:
            raise ConnectionError("ungültige Handshake-Nachricht")
        hs = Handshake(cfg.encryption_key, initiator=False)
        try:
            hs.read_message1(client_msg[1:])
            reply = hs.write_message2()
        except HandshakeError as exc:
            log.warning("%s: Handshake abgelehnt (%s) – falscher Schlüssel?", self.peer, exc)
            self._write_frame(b"\x01" + str(exc).encode())
            await self.writer.drain()
            return False
        self._write_frame(b"\x00" + reply)
        await self.writer.drain()
        self._send, self._recv = hs.split()
        return True

    async def _dispatch(self, msg_id: int, payload: bytes) -> None:
        handler = self._handlers.get(msg_id)
        if handler is None:
            log.debug("%s: Nachricht %d ignoriert", self.peer, msg_id)
            return
        try:
            message = m.REGISTRY[msg_id].decode(payload)
        except DecodeError as exc:
            log.warning("%s: Nachricht %d nicht lesbar: %s", self.peer, msg_id, exc)
            return
        result = handler(message)
        if result is not None:
            await result

    # Handler -----------------------------------------------------------------
    def _hello(self, req: m.HelloRequest) -> None:
        log.info("%s: Client '%s' (API %d.%d)", self.peer, req.client_info,
                 req.api_version_major, req.api_version_minor)
        self.send(m.HelloResponse(*API_VERSION, f"pi-ble-proxy {__version__}", self.server.config.name))

    def _authenticate(self, _: m.AuthenticationRequest) -> None:
        self.send(m.AuthenticationResponse())  # Verschlüsselung ersetzt das Passwort

    def _disconnect(self, _: m.DisconnectRequest) -> None:
        self.send(m.DisconnectResponse())
        self._closing = True

    def _device_info(self, _: m.DeviceInfoRequest) -> None:
        cfg = self.server.config
        self.send(
            m.DeviceInfoResponse(
                name=cfg.name,
                friendly_name=cfg.friendly_name,
                mac_address=cfg.mac,
                esphome_version=ESPHOME_VERSION,
                compilation_time=time.strftime("%b %d %Y, %H:%M:%S"),
                model="Raspberry Pi",
                manufacturer="pi-ble-proxy",
                bluetooth_proxy_feature_flags=self.server.feature_flags,
                bluetooth_mac_address=self.server.scanner.adapter_address,
            )
        )

    def _list_entities(self, _: m.ListEntitiesRequest) -> None:
        for sensor in SENSORS:
            self.send(sensor.description())
        self.send(m.ListEntitiesDoneResponse())

    def _subscribe_states(self, _: m.SubscribeStatesRequest) -> None:
        self.wants_states = True
        self.send_states()

    def send_states(self) -> None:
        if self.wants_states:
            for sensor in SENSORS:
                self.send(sensor.state(self.server))

    def send_scanner_state(self) -> None:
        if self.server.feature_flags & FEATURE_STATE_AND_MODE:
            self.send(self.server.scanner_state())

    async def _set_scanner_mode(self, req: m.BluetoothScannerSetModeRequest) -> None:
        mode = "active" if req.mode == m.MODE_ACTIVE else "passive"
        try:
            if self.server.gatt is not None:
                await self.server.gatt.set_scan_mode(mode)
        except GattError as exc:
            log.warning("%s: Scan-Modus %s nicht möglich: %s", self.peer, mode, exc)
        self.server.broadcast_scanner_state()  # meldet den tatsächlichen Modus zurück

    def _subscribe_adv(self, _: m.SubscribeBluetoothLEAdvertisementsRequest) -> None:
        self.wants_advertisements = True  # wir senden immer Rohdaten (Feature-Flag RAW)
        self.send_scanner_state()

    def _unsubscribe_adv(self, _: m.UnsubscribeBluetoothLEAdvertisementsRequest) -> None:
        self.wants_advertisements = False

    # Aktive Verbindungen ---------------------------------------------------------
    def _spawn(self, coro) -> None:
        """Langsame Bluetooth-Operationen laufen als Task, damit Pings beantwortbar bleiben."""
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)

    def _task_done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and (exc := task.exception()):
            log.error("%s: Fehler in Bluetooth-Operation: %r", self.peer, exc, exc_info=exc)

    def send_connections_free(self) -> None:
        if not self.wants_free:
            return
        srv = self.server
        self.send(m.BluetoothConnectionsFreeResponse(
            free=srv.free_slots, limit=srv.config.max_gatt_connections,
            allocated=sorted(a for a, link in srv.links.items() if link.conn)))

    def _subscribe_free(self, _: m.SubscribeBluetoothConnectionsFreeRequest) -> None:
        self.wants_free = True
        self.send_connections_free()

    def _conn(self, address: int) -> GattConnection | None:
        link = self.server.links.get(address)
        return link.conn if link and link.session is self else None

    def _device_request(self, req: m.BluetoothDeviceRequest) -> None:
        t = req.request_type
        if self.server.gatt is None:
            return
        if t in (m.REQ_CONNECT, m.REQ_CONNECT_WITH_CACHE, m.REQ_CONNECT_WITHOUT_CACHE):
            link = self.server.links.get(req.address)
            if link is None and self.server.free_slots > 0:
                link = self.server.links[req.address] = Link(self)  # Slot sofort belegen
                self.server.broadcast_free()
                link.task = asyncio.get_running_loop().create_task(self._connect(req, link))
                self._tasks.add(link.task)
                link.task.add_done_callback(self._task_done)
            elif link is not None and link.conn and link.session is self:
                self._send_connected(req.address, link.conn)
            else:
                self.send(m.BluetoothDeviceConnectionResponse(
                    address=req.address, connected=False, error=g.ERR_GENERIC))
        elif t == m.REQ_DISCONNECT:
            self._spawn(self._disconnect_device(req.address))
        elif t == m.REQ_PAIR:
            self._spawn(self._pair(req.address))
        elif t in (m.REQ_UNPAIR, m.REQ_CLEAR_CACHE):
            self._spawn(self._unpair(req.address, clear_cache=t == m.REQ_CLEAR_CACHE))

    def _send_connected(self, address: int, conn: GattConnection) -> None:
        self.send(m.BluetoothDeviceConnectionResponse(address=address, connected=True, mtu=conn.mtu))

    async def _connect(self, req: m.BluetoothDeviceRequest, link: Link) -> None:
        srv = self.server
        started = time.monotonic()
        mac = format_mac(req.address)
        srv.stats.connects += 1
        log.info("%s: verbinde mit %s (Typ %d, %d von %d Slots frei)", self.peer, mac, req.request_type,
                 srv.free_slots, srv.config.max_gatt_connections)
        try:
            conn = await srv.gatt.connect(req.address, req.address_type,
                                          lambda: srv.link_lost(req.address))
        except asyncio.CancelledError:
            log.info("%s: Verbindungsaufbau zu %s abgebrochen nach %.1f s (Trennung durch HA oder Client weg)",
                     self.peer, mac, time.monotonic() - started)
            srv.links.pop(req.address, None)
            srv.broadcast_free()
            raise
        except GattError as exc:
            log.info("%s: Verbindung zu %s fehlgeschlagen nach %.1f s: %s", self.peer, mac,
                     time.monotonic() - started, exc)
            srv.stats.connect_failures += 1
            srv.links.pop(req.address, None)
            srv.broadcast_free()
            self.send(m.BluetoothDeviceConnectionResponse(address=req.address, connected=False, error=exc.code))
            return
        if srv.links.get(req.address) is not link or self._closing:  # währenddessen getrennt
            await conn.disconnect()
            return
        link.conn = conn
        log.info("%s: verbunden mit %s (MTU %d) nach %.1f s", self.peer, mac, conn.mtu, time.monotonic() - started)
        self._send_connected(req.address, conn)
        srv.broadcast_free()

    async def _disconnect_device(self, address: int) -> None:
        log.info("%s: HA trennt %s", self.peer, format_mac(address))
        link = self.server.links.get(address)
        if link is None or link.session is not self:
            self.send(m.BluetoothDeviceConnectionResponse(address=address, connected=False))
            return
        self.server.links.pop(address)
        if link.conn:
            await link.conn.disconnect()
        elif link.task:
            link.task.cancel()
        self.send(m.BluetoothDeviceConnectionResponse(address=address, connected=False))
        self.server.broadcast_free()

    async def _pair(self, address: int) -> None:
        try:
            await self.server.gatt.pair(address)
            self.send(m.BluetoothDevicePairingResponse(address=address, paired=True))
        except (GattError, asyncio.TimeoutError) as exc:
            code = exc.code if isinstance(exc, GattError) else g.ERR_TIMEOUT
            self.send(m.BluetoothDevicePairingResponse(address=address, paired=False, error=code))

    async def _unpair(self, address: int, *, clear_cache: bool) -> None:
        cls = m.BluetoothDeviceClearCacheResponse if clear_cache else m.BluetoothDeviceUnpairingResponse
        try:
            await self.server.gatt.unpair(address)
            self.send(cls(address=address, success=True))
        except GattError as exc:
            self.send(cls(address=address, success=False, error=exc.code))

    async def _gatt(self, address: int, handle: int, op) -> None:
        """Führt ``op(conn)`` aus und meldet Fehler als BluetoothGATTErrorResponse."""
        conn = self._conn(address)
        try:
            if conn is None:
                raise GattError("nicht verbunden", g.ERR_NOT_FOUND)
            await op(conn)
        except GattError as exc:
            log.info("%s: GATT-Fehler %s/%d: %s", self.peer, format_mac(address), handle, exc)
            self.send(m.BluetoothGATTErrorResponse(address=address, handle=handle, error=exc.code))
        except asyncio.TimeoutError:
            self.send(m.BluetoothGATTErrorResponse(address=address, handle=handle, error=g.ERR_TIMEOUT))

    async def _get_services(self, req: m.BluetoothGATTGetServicesRequest) -> None:
        async def op(conn):
            for svc in await conn.services():
                self.send(m.BluetoothGATTGetServicesResponse(address=req.address, services=[
                    m.BluetoothGATTService(
                        uuid=g.uuid_to_words(svc.uuid), handle=svc.handle,
                        characteristics=[m.BluetoothGATTCharacteristic(
                            uuid=g.uuid_to_words(c.uuid), handle=c.handle, properties=c.properties,
                            descriptors=[m.BluetoothGATTDescriptor(uuid=g.uuid_to_words(d.uuid), handle=d.handle)
                                         for d in c.descriptors]) for c in svc.characteristics])]))
            self.send(m.BluetoothGATTGetServicesDoneResponse(address=req.address))
        await self._gatt(req.address, 0, op)

    async def _read(self, req, descriptor: bool) -> None:
        async def op(conn):
            data = await (conn.read_descriptor if descriptor else conn.read)(req.handle)
            self.send(m.BluetoothGATTReadResponse(address=req.address, handle=req.handle, data=data))
        await self._gatt(req.address, req.handle, op)

    async def _write(self, req: m.BluetoothGATTWriteRequest) -> None:
        async def op(conn):
            await conn.write(req.handle, req.data, req.response)
            if req.response:
                self.send(m.BluetoothGATTWriteResponse(address=req.address, handle=req.handle))
        await self._gatt(req.address, req.handle, op)

    async def _write_descriptor(self, req: m.BluetoothGATTWriteDescriptorRequest) -> None:
        async def op(conn):
            await conn.write_descriptor(req.handle, req.data)
            self.send(m.BluetoothGATTWriteResponse(address=req.address, handle=req.handle))
        await self._gatt(req.address, req.handle, op)

    async def _notify(self, req: m.BluetoothGATTNotifyRequest) -> None:
        async def op(conn):
            if req.enable:
                def on_data(data: bytes) -> None:
                    self.server.stats.notifications += 1
                    log.debug("%s: Notification %s/%d: %d Bytes", self.peer, format_mac(req.address),
                              req.handle, len(data))
                    self.send(m.BluetoothGATTNotifyDataResponse(address=req.address, handle=req.handle, data=data))

                await conn.start_notify(req.handle, on_data)
                log.info("%s: Notifications für %s/%d aktiv", self.peer, format_mac(req.address), req.handle)
            else:
                await conn.stop_notify(req.handle)
            self.send(m.BluetoothGATTNotifyResponse(address=req.address, handle=req.handle))
        await self._gatt(req.address, req.handle, op)

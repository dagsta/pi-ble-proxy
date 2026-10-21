"""Ende-zu-Ende: derselbe Client (aioesphomeapi), den Home Assistant benutzt."""

import asyncio
import base64
import os

import pytest

from pi_ble_proxy.config import Config
from pi_ble_proxy.scanner import Advertisement
from pi_ble_proxy.server import ApiServer

aioesphomeapi = pytest.importorskip("aioesphomeapi")

KEY = os.urandom(32)


class FakeScanner:
    adapter_address = "DC:A6:32:01:02:03"

    async def start(self, cb, discover=True):
        self.emit, self.discover = cb, discover

    async def stop(self):
        pass

    async def check(self):
        return None


def make_config(**kw):
    base = dict(
        name="pi-ble-proxy", friendly_name="Pi Proxy", mac="B8:27:EB:00:00:01",
        host="127.0.0.1", port=0, encryption_key=KEY, adapter_index=0,
        max_connections=4, max_gatt_connections=3, pause_scan_on_connect=True, log_level="DEBUG",
    )
    return Config(**{**base, **kw})


async def with_server(test, **cfg):
    scanner = FakeScanner()
    server = ApiServer(make_config(**cfg), scanner)
    await server.start()
    try:
        await test(server, scanner)
    finally:
        await server.stop()


def client_for(server, key=KEY, **kw):
    return aioesphomeapi.APIClient(
        "127.0.0.1", server.port, None, noise_psk=base64.b64encode(key).decode(), **kw
    )


def test_connect_and_device_info():
    async def test(server, scanner):
        client = client_for(server)
        await client.connect(login=True)
        info = await client.device_info()
        assert info.name == "pi-ble-proxy"
        assert info.mac_address == "B8:27:EB:00:00:01"
        assert info.bluetooth_mac_address == "DC:A6:32:01:02:03"
        assert info.bluetooth_proxy_feature_flags == 33
        entities, services = await client.list_entities_services()
        assert [e.object_id for e in entities] == ["cpu_temperature", "advertisements_received", "active_connections"]
        assert services == []
        await client.disconnect()

    asyncio.run(with_server(test))


def test_wrong_key_is_rejected_with_invalid_key_error():
    async def test(server, scanner):
        client = client_for(server, key=os.urandom(32))
        with pytest.raises(aioesphomeapi.InvalidEncryptionKeyAPIError):
            await client.connect(login=True)

    asyncio.run(with_server(test))


def test_advertisements_are_batched_and_delivered():
    async def test(server, scanner):
        client = client_for(server)
        await client.connect(login=True)
        received = []
        client.subscribe_bluetooth_le_raw_advertisements(lambda r: received.extend(r.advertisements))
        await asyncio.sleep(0.1)  # Subscribe ist beim Server angekommen
        eir = b"\x02\x01\x06\x03\x03\x0f\x18"
        for i in range(5):
            scanner.emit(Advertisement(0xAABBCCDDEE00 + i, i % 2, -50 - i, eir))
        await asyncio.sleep(0.4)
        assert [a.address for a in received] == [0xAABBCCDDEE00 + i for i in range(5)]
        assert [a.rssi for a in received] == [-50 - i for i in range(5)]
        assert [a.address_type for a in received] == [0, 1, 0, 1, 0]
        assert {bytes(a.data) for a in received} == {eir}
        await client.disconnect()

    asyncio.run(with_server(test))


def test_no_traffic_without_subscription_and_after_disconnect():
    async def test(server, scanner):
        scanner.emit(Advertisement(1, 0, -50, b""))
        assert server._pending == []  # niemand abonniert: nichts puffern
        client = client_for(server)
        await client.connect(login=True)
        await client.device_info()
        await client.disconnect()
        await asyncio.sleep(0.1)
        scanner.emit(Advertisement(1, 0, -50, b""))
        assert server._pending == [] and not server._sessions

    asyncio.run(with_server(test))


def test_connection_limit():
    async def test(server, scanner):
        first = client_for(server)
        await first.connect(login=True)
        second = client_for(server)
        with pytest.raises(aioesphomeapi.APIConnectionError):
            await second.connect(login=True)
        await first.disconnect()

    asyncio.run(with_server(test, max_connections=1))


# --- Keepalive -----------------------------------------------------------------------------

def test_server_pings_itself_so_a_busy_connection_survives(monkeypatch):
    """Der HA-Client pingt nicht, solange er Daten empfängt. Der Proxy darf ihn deshalb nicht trennen."""
    from pi_ble_proxy import server as srv

    monkeypatch.setattr(srv, "KEEPALIVE_INTERVAL", 0.2)
    monkeypatch.setattr(srv, "DISCONNECT_AFTER", 0.6)

    async def test(server, scanner):
        client = client_for(server)
        await client.connect(login=True)
        pings = []
        client._get_connection().add_message_callback(lambda msg: pings.append(msg),
                                                       (aioesphomeapi.core.PingRequest,))
        client.subscribe_bluetooth_le_raw_advertisements(lambda r: None)
        for _ in range(15):  # 1,5 s steter Datenverkehr in Richtung HA, von HA kommt nichts
            scanner.emit(Advertisement(1, 0, -50, b""))
            await asyncio.sleep(0.1)
        assert pings, "Server hat nie ein PingRequest geschickt"
        assert (await client.device_info()).name == "pi-ble-proxy"  # Verbindung lebt noch
        await client.disconnect()

    asyncio.run(with_server(test))


def test_silent_dead_client_is_dropped_after_pings_go_unanswered(monkeypatch):
    from pi_ble_proxy import server as srv
    from pi_ble_proxy.noise import Handshake

    monkeypatch.setattr(srv, "KEEPALIVE_INTERVAL", 0.2)
    monkeypatch.setattr(srv, "DISCONNECT_AFTER", 0.7)

    async def test(server, scanner):
        reader, writer = await asyncio.open_connection("127.0.0.1", server.port)

        async def frame():
            header = await reader.readexactly(3)
            return await reader.readexactly(int.from_bytes(header[1:], "big"))

        hs = Handshake(KEY, initiator=True)
        writer.write(b"\x01\x00\x00")  # Hello
        await frame()  # ServerHello
        msg1 = b"\x00" + hs.write_message1()
        writer.write(b"\x01" + len(msg1).to_bytes(2, "big") + msg1)
        reply = await frame()
        hs.read_message2(reply[1:])
        _, recv = hs.split()

        ping = recv.decrypt(b"", await asyncio.wait_for(frame(), 2))
        assert int.from_bytes(ping[:2], "big") == 7  # PingRequest
        start = asyncio.get_running_loop().time()
        with pytest.raises(asyncio.IncompleteReadError):  # wir antworten nie: Server schließt
            while True:
                await asyncio.wait_for(frame(), 3)
        assert 0.5 < asyncio.get_running_loop().time() - start < 2.5
        writer.close()

    asyncio.run(with_server(test))
